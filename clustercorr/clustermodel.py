import os
import sys
import warnings
import numpy as np
import pandas as pd
from .pyper import R
import tempfile
r = R(max_len=5e7, return_err=False)
r('source("%s/mods.R")' % os.path.dirname(__file__))

def rcall(covs, model, X=None, kwargs=None):
    """
    internal function to call R and return the result
    """
    if kwargs is None: kwargs = {}

    # faster to use csv than to use pyper's conversion
    if not isinstance(covs, str):
        fh = tempfile.NamedTemporaryFile()
        covs.to_csv(fh, index=False)
        fh.flush()
        covs = fh.name

    assert os.path.exists(covs), covs
    r['combined_df'] = covs
    if X is None:
        kwargs_str = ", ".join("%s='%s'" % (k, v)
                                for k, v in kwargs.iteritems())
        r("a <- c('nan', 'nan', 'nan'); a <- fclust.lm(combined_df, '%s', %s)"
                % (model, kwargs_str))
        vals = r['a']
        ret = dict(covariate=vals[0], p=float(vals[1]), model=model)
        ret['coef'] = float(vals[2])
        ret.update(kwargs)
        return ret
    else:
        if False:
            "fclust.lm.X('%s', '%s', '%s', %s)" % (covs, model, X, kwargs_str)
            import shutil ;shutil.copyfile(covs, "/tmp/ttt.csv")
        import multiprocessing
        mc_cores = multiprocessing.cpu_count()
        kwargs['mc.cores'] = mc_cores - 1
        kwargs_str = ", ".join("%s='%s'" % (k, v)
                                for k, v in kwargs.iteritems())
        r("a <- NA; a <- fclust.lm.X(combined_df, '%s', '%s', %s)"
                % (model, X, kwargs_str))
        df = r['a']
        return df

def clustered_model(cov_df, cluster_df, model, X=None, gee_args=(), liptak=False,
        bumping=False, skat=False, outlier_sds=None):
    """
    Given a cluster of (presumably) correlated CpG's. There are a number of
    methods one could employ to determine the association of the methylation
    of those CpG's to a covariate (often disease status).
    Here we implement:

    1. GEE with autoregressive, independent, or exchangeable correlation
       structure.
    2. random interecpt for each CpG and or each individual
    3. Liptak correction of correlated p-values (1 p-value from each probe and
       use observed correlation for correction
    4. "bumping" algorithm that simulates data by shuffling the residuals of
       the reduced model and compares the observed coefficient estimates to the
       simulated estimates from the shuffled data. Uses a lowess smooothing of
       the data. Different from "bumphunting" because "bumphunting" must do this
       genome-wide, whereas here, it is on a per-cluster basis
    5. SKAT. we can use the methylation values to send to SKAT to compare to the
       null model that does not consider methylation.


    Arguments:

        cov_df - a pandas.DataFrame that must have an index of sample ids and
                 all the covariates defined in model except "id" and "CpG"
                 which will be set automatically. pandas DataFrame

        cluster_df - a pandas.DataFrame that is a cluster of probes. Must have
                     and index indicating the CpG (name or site) and columns
                     of sample ids. This function will use samples from the
                     intersection of cluster_df.columns and cov_df.index

        model - model in R syntax with "methylation ~" as the RHS. Other
                allowed covariates are any that appear in cov_df as well as
                "CpG" and "id" which will be set by this function.
                If not using liptak or bumping or gee_args this model should
                likely have a random effect. The obvious choices would be:
                (1|CpG) and/or (1|id) to add a random-intercept by CpG site
                and/or by sample.
                The p-value returned will always be for the first covariate
                in the model. See module docstring for examples.

        X - a file with the same samples as cov_df and rows of expression
            data. If present, each DMR will be tested against each row in
            this file--this is computationally intensive!!

        gee_args - a 2-tuple of arguments to R's geepack::geeglm().
                   1) the corstr (one of "ex", "in", "ar")
                   2) the cluster variable. This will likely be "id" if
                      corstr="ar" otherwise it will likely be "CpG"
                   sorting is handled internally so that these options will
                   work as expected.
                   So common invocations would be ('ex', 'CpG') or ('ar', 'id')

        liptak - if set to True, use liptak correction on the p-values from
                 modelling each CpG independently.

        bumping - if set to True, use a modified bump-hunting algorithm to test
                  the sum of the observed coefficients (1 for each CpG) against
                  the sums of coefficients derived by fitting the model to data
                  generated by repeatedly shuffling the residuals of the reduced
                  model

        skat - if set to True, use skat to test if modelling the CpG
               methylation better describes the dependent variable.
    """

    combined_df = cov_cluster_setup(cov_df, cluster_df, outlier_sds)
    #combined_df.to_csv('clustercorr/tests/example-wide.csv')
    return clustered_model_frame(combined_df, model, X, gee_args, liptak,
                                 bumping, skat)

def set_outlier_nan(cluster_df, n_sds):
    """
    take cluster dataframe and set to nan
    any values where that are > n_sds standard-deviations away
    from the mean for that probe
    """
    #imean, isd = cluster_df.mean(axis=1), cluster_df.std(axis=1,
    #        skipna=True)

    for probe in cluster_df.index:
        row = cluster_df.ix[probe, :]
        rown = row[~np.isnan(row)]
        m, s = rown.mean(), rown.std()
        rng = (m - (n_sds * s)), (m + (n_sds * s))
        row[((row < rng[0]) | (row > rng[1]))] = np.nan

def cov_cluster_setup(cov_df, cluster_df, outlier_sds=None):
    """
    turn two dataframes, one for methylation and 1 for covariates into a
    single, long dataframe.
    (some) index from cov_df must match columns from cluster_df
    returns a file-handle of the merged dataframe.
    """
    if outlier_sds:
        set_outlier_nan(cluster_df, n_sds=outlier_sds)

    methylation = cluster_df.T
    if not set(cov_df.index).intersection(methylation.index):
        raise Exception("must share cov_df.index, cluster_df.columns")

    n_probes = cluster_df.shape[1]
    combined_df = cov_df.copy()
    combined_df['id'] = np.arange(cov_df.shape[0]).astype(int)
    for c in methylation.columns:
        combined_df['CpG__' + str(c)] = methylation[c]

    return combined_df

def clustered_model_frame(combined_df, model, X=None, gee_args=(), liptak=False,
        bumping=False, skat=False):
    """
    the arguments to this function are identical to clustered_model()
    except that fname_df is the file-name of a dataframe.to_csv()
    this allows calling a number of methods without writing to a new
    file each time
    """

    if "|" in model:
        assert not any((skat, liptak, bumping, gee_args))
        return rcall(combined_df, model, X)

    if skat:
        return rcall(combined_df, model, X, dict(skat=True))
    elif liptak:
        return rcall(combined_df, model, X, dict(liptak=True))
    elif bumping:
        return rcall(combined_df, model, X, dict(bumping=True))
    elif gee_args:
        corr, cov = gee_args
        assert corr[:2] in ('ex', 'ar', 'in', 'un')
        return rcall(combined_df, model, X, {"gee.corstr": corr, "gee.clustervar": cov})
    else:
        raise Exception('must specify one of skat/liptak/bumping/gee_args'
                        ' or specify a mixed-effect model in lme4 syntax')
