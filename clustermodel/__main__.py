import sys
import tempfile
import gzip
import re
from itertools import groupby, izip_longest
from collections import OrderedDict
import numpy as np
import pandas as pd
from aclust import aclust
from .plotting import plot_dmr, plot_hbar, plot_continuous
from . import feature_gen, cluster_to_dataframe, clustered_model, CPUS
from .clustermodel import r

xopen = lambda f: gzip.open(f) if f.endswith('.gz') else open(f)

def is_numeric(pd_series):
    if np.issubdtype(pd_series.dtype, int) or \
        np.issubdtype(pd_series.dtype, float):
        return len(pd_series.unique()) > 2
    return False

def run_model(clusters, covs, model, X, outlier_sds, liptak, bumping, gee_args,
        skat):
    # we turn the cluster list into a pandas dataframe with columns
    # of samples and rows of probes. these must match our covariates
    cluster_dfs = [cluster_to_dataframe(cluster, columns=covs.index)
            for cluster in clusters]
        # now we want to test a model on our clustered dataset.
    res = clustered_model(covs, cluster_dfs, model, X=X, gee_args=gee_args,
                liptak=liptak, bumping=bumping, skat=skat,
                outlier_sds=outlier_sds)
    res['chrom'], res['start'], res['end'], res['n_probes'] = ("CHR", 1, 1, 0)
    if "cluster_id" in res.columns:
        # start at 1 because we using 1:nclusters in R
        for i, c in enumerate(clusters, start=1):
            res.ix[res.cluster_id == i, 'chrom'] = c[0].group
            res.ix[res.cluster_id == i, 'start'] = c[0].start
            res.ix[res.cluster_id == i, 'end'] = c[-1].end
            res.ix[res.cluster_id == i, 'n_probes'] = len(c)
    else:
        assert len(clusters) == 1
        res['chrom'] = clusters[0][0].group
        res['start'] = clusters[0][0].start
        res['end'] = clusters[-1][-1].end
        res['n_probes'] = len(clusters[0])
    return res

def distX(dmr, expr):
    strand = "-" if expr['strand'] == "-" else "+"
    dmr['distance'] = 0
    if dmr['end'] < expr['start']:
        dmr['distance'] = expr['start'] - dmr['end']
        # dmr is left of gene. that means it is upstream if strand is +
        # we use "-" for upstream
        if strand == "+":
            dmr['distance'] *= -1

    elif dmr['start'] > expr['end']:
        dmr['distance'] = dmr['start'] - expr['end']
        # dmr is right of gene. that is upstream if strand is -
        # use - for upstream
        if strand == "-":
            dmr['distance'] *= -1
    dmr['Xstart'], dmr['Xend'], dmr['Xstrand'] = expr['start'], expr['end'], expr['strand']
    dmr['Xname'] = expr.get('name', expr.get('gene', dmr.get('X', 'NA')))


def clustermodel(fcovs, fmeth, model,
                 # clustering args
                 max_dist=500, linkage='complete', rho_min=0.3,
                 min_clust_size=2, multi_member=False,

                 sep="\t",
                 X=None, X_locs=None, X_dist=None,
                 outlier_sds=None,
                 liptak=False, bumping=False, gee_args=(), skat=False,
                 png_path=None):
    # an iterable of feature objects
    feature_iter = feature_gen(fmeth, rho_min=rho_min)
    assert min_clust_size >= 1

    cluster_gen = (c for c in aclust(feature_iter, max_dist=max_dist,
                                     max_skip=2, linkage=linkage,
                                     multi_member=multi_member)
                    if len(c) >= min_clust_size)
    for res in clustermodelgen(fcovs, cluster_gen, model, sep=sep,
            X=X, X_locs=X_locs, X_dist=X_dist, outlier_sds=outlier_sds,
            liptak=liptak, bumping=bumping, gee_args=gee_args,
            skat=skat, png_path=None):
        yield res


def fix_name(name, patt=re.compile("-|:| ")):
    """
    >>> fix_name('asd f')
    'asd.f'
    >>> fix_name('asd-f')
    'asd.f'
    >>> fix_name('a:s:d-f')
    'a.s.d.f'
    """
    return re.sub(patt, ".", name)


def groups_of(n, iterable):
    args = [iter(iterable)] * n
    for x in izip_longest(*args):
        yield [v for v in x if v is not None]


def clustermodelgen(fcovs, cluster_gen, model, sep="\t",
                    X=None, X_locs=None, X_dist=None,
                    outlier_sds=None,
                    liptak=False, bumping=False, gee_args=(), skat=False,
                    png_path=None):

    covs = pd.read_table(fcovs, index_col=0, sep=sep)
    covariate = model.split("~")[1].split("+")[0].strip()
    Xvar = X
    if X is not None:
        # read in once in R, then subset by probes
        r('Xfull = readX("%s")' % X)
        Xvar = 'Xfull'

    # read expression into memory and pull out subsets as needed.
    if not X_locs is None:
        # change names so R formulas are OK
        X_locs = pd.read_table(xopen(X_locs), index_col="probe")
        X_locs.index = [fix_name(xi) for xi in X_locs.index]

        # just reading in the first column to make sure we're using probes that
        # exist in the X matrix
        Xi = pd.read_table(xopen(X), index_col=0, usecols=[0]).index
        X_probes = set([fix_name(xi) for xi in Xi])

    for clusters in groups_of(100 * CPUS if X is None else
                              10 * CPUS if X_locs is not None
                              else CPUS, cluster_gen):

        if not X_locs is None:
            probes = []
            # here, we take any X probe that's associated with any single
            # cluster and test it against all clusters. This tends to work out
            # because the clusters are sorted by location and it helps
            # parallelization. 
            for cluster in clusters:
                chrom = cluster[0].group
                start, end = cluster[0].start, cluster[-1].end
                probe_locs = X_locs[((X_locs.ix[:, 0] == chrom) &
                             (X_locs.ix[:, 1] < (end + X_dist)) &
                             (X_locs.ix[:, 2] > (start - X_dist)))]
                probes.extend([p for p in probe_locs.index if p in X_probes])
            if len(probes) == 0: continue
            probes = OrderedDict.fromkeys(probes).keys()

            # we send do the extraction directly in R so the only data
            # sent is the name of the probes. Then we take the subset
            # inside R
            r['XXprobes'] = probes
            Xvar = 'Xfull[XXprobes,,drop=FALSE]'

        res = run_model(clusters, covs, model, Xvar, outlier_sds, liptak,
                        bumping, gee_args, skat)

        for i, row in res.iterrows():
            row = dict(row)
            if X_locs is not None:
                distX(row, dict(X_locs.ix[row['X'], :]))
            yield row
            if row['p'] < 1e-4 and png_path:
                cluster_df = cluster_to_dataframe(clusters[i], columns=covs.index)
                plot_res(row, png_path, covs, covariate, cluster_df)



def plot_res(res, png_path, covs, covariate, cluster_df):
    from matplotlib import pyplot as plt
    from mpltools import style
    style.use('ggplot')

    region = "{chrom}_{start}_{end}".format(**res)
    if png_path.endswith('show'):
        png = None
    elif png_path.endswith(('.png', '.pdf')):
        png = "%s.%s%s" % (png_path[:-4], region, png_path[-4:])
    elif png_path:
        png = "%s.%s.png" % (png_path.rstrip("."), region)

    if is_numeric(getattr(covs, covariate)):
        f = plot_continuous(covs, cluster_df, covariate, res['chrom'], res, png)
    else:
        f = plt.figure(figsize=(11, 4))
        ax = f.add_subplot(1, 1, 1)
        if 'spaghetti' in png_path:
            plot_dmr(covs, cluster_df, covariate, res['chrom'], res, png)
        else:
            plot_hbar(covs, cluster_df, covariate, res['chrom'], res, png)
        plt.title('p-value: %.3g %s: %.3f' % (res['p'], covariate, res['coef']))
    f.set_tight_layout(True)
    if png:
        plt.savefig(png)
    else:
        plt.show()


def main_example():
    fcovs = "clustermodel/tests/example-covariates.txt"
    fmeth = "clustermodel/tests/example-methylation.txt.gz"
    model = "methylation ~ disease + gender"

    for cluster_p in clustermodel(fcovs, fmeth, model):
        if cluster_p['p'] < 1e-5:
            print cluster_p

def add_modelling_args(p):
    mp = p.add_argument_group('modeling choices (choose one or specify a '
            'mixed-model using lme4 syntax)')
    group = mp.add_mutually_exclusive_group()

    group.add_argument('--skat', action='store_true')
    group.add_argument('--gee-args',
                       help='comma-delimited correlation-structure, variable')
    group.add_argument('--liptak', action="store_true")
    group.add_argument('--bumping', action="store_true")

    p.add_argument('model',
                   help="model in R syntax, e.g. 'methylation ~ disease'")
    p.add_argument('covs', help="tab-delimited file of covariates: shape is "
                   "n_samples * n_covariates")
    p.add_argument('methylation', help="tab-delimited file of methylation"
                   " rows of this file must match the columns of `covs`"
                   " shape is n_probes * n_samples")

def add_expression_args(p):
    ep = p.add_argument_group('optional expression parameters')
    ep.add_argument('--X', help='file with same sample columns as methylation, '
            'rows of probes and values of some measurement (likely expression)'
            ' this will perform a methyl-eQTL--for each DMR, it will test '
            'againts all rows in this methylation array. As such, it is best'
            ' to run this on subsets of data, e.g. only looking for cis '
            'relationships')
    ep.add_argument('--X-locs', help="BED file with locations of probes from"
            " the first column in --X. Should have a 'probe' column header")
    ep.add_argument('--X-dist', type=int, help="only look at cis interactions"
            " between X and methylation sites with this as the maximum",
            default=100000)

def add_clustering_args(p):
    cp = p.add_argument_group('clustering parameters')
    cp.add_argument('--rho-min', type=float, default=0.3,
                   help="minimum correlation to merge 2 probes")
    cp.add_argument('--min-cluster-size', type=int, default=2,
                    help="minimum cluster size on which to run model: "
                   "must be at least 1")
    cp.add_argument('--linkage', choices=['single', 'complete'],
                    default='complete', help="linkage method")
    cp.add_argument('--multi-member', default=False, action="store_true",
                    help="if True, a probe can be a member of multiple"
                    " clusters")

    cp.add_argument('--max-dist', default=500, type=int,
                    help="maximum distance beyond which a probe can not be"
                    " added to a cluster")

def add_misc_args(p):
    p.add_argument('--png-path',
                   help="path to save a png of regions with low p-values. If "
                   "this ends with 'show', each plot will be shown in a window"
                   " if this contains the string 'spaghetti', it will draw a "
                   "a spaghetti plot, otherwise, it's a histogram plot")
    p.add_argument('--outlier-sds', type=float, default=30,
            help="remove points that are more than this many standard "
                 "deviations away from the mean (only usable with GEEs"
                 " and mixed-models which allow missing data")

def get_method(a):
    if a.gee_args is not None:
        method = 'gee:' + a.gee_args
        a.gee_args = a.gee_args.split(",")
    else:
        if a.liptak: method = 'liptak'
        elif a.bumping: method = 'bumping'
        elif a.skat: method = 'skat'
        else:
            assert "|" in a.model
            method = "mixed-model"
    return method

def gen_clusters_from_regions(feature_iter, regions):
    header = xopen(regions).next().split("\t")
    has_header = not (header[1].isdigit() and header[2].isdigit())
    regions = pd.read_table(regions, header=0 if has_header else False)
    regions.columns = 'chrom start end'.split() + list(regions.columns[3:])

    regions['region'] = ['%s:%i-%i' % t for t in zip(regions['chrom'],
                                                     regions['start'],
                                                     regions['end'])]
    def by_region(feat):
        sub = regions[((regions['chrom'] == feat.group) &
                (feat.start <= regions['end']) &
                (feat.end >= regions['start']))]['region']
        sub = list(sub)
        if len(sub) == 0: return False
        assert len(sub) == 1, (feat, "overlaps multiple regions")
        return str(sub[0])

    # TODO: send the region back to the caller as well
    for region, cluster in groupby(feature_iter, by_region):
        if not region: continue
        yield list(cluster)


def regional_main(args=sys.argv[1:]):
    import argparse
    p = argparse.ArgumentParser(__doc__)
    add_modelling_args(p)
    add_misc_args(p)
    add_expression_args(p)

    p.add_argument('--regions', required=True, help="BED file of regions to "
            "test", metavar="BED")

    a = p.parse_args(args)
    method = get_method(a)

    feature_iter = feature_gen(a.methylation)
    cluster_gen = gen_clusters_from_regions(feature_iter, a.regions)

    fmt = "{chrom}\t{start}\t{end}\t{coef}\t{p}\t{n_probes}\t{model}\t{method}"
    if a.X_locs:
        fmt += "\t{Xname}\t{Xstart}\t{Xend}\t{Xstrand}\t{distance}"
    print "#" + fmt.replace("}", "").replace("{", "")


    for c in clustermodelgen(a.covs, cluster_gen, a.model,
                          X=a.X,
                          X_locs=a.X_locs,
                          X_dist=a.X_dist,
                          outlier_sds=a.outlier_sds,
                          liptak=a.liptak,
                          bumping=a.bumping,
                          gee_args=a.gee_args,
                          skat=a.skat,
                          png_path=a.png_path):
        c['method'] = method if c['n_probes'] > 1 else 'lm'
        print fmt.format(**c)


def main(args=sys.argv[1:]):
    import argparse
    p = argparse.ArgumentParser(__doc__)

    add_modelling_args(p)
    add_clustering_args(p)
    add_misc_args(p)
    add_expression_args(p)

    a = p.parse_args(args)
    method = get_method(a)

    fmt = "{chrom}\t{start}\t{end}\t{coef}\t{p}\t{n_probes}\t{model}\t{method}"
    if a.X_locs:
        fmt += "\t{Xname}\t{Xstart}\t{Xend}\t{Xstrand}\t{distance}"
    print "#" + fmt.replace("}", "").replace("{", "")
    for c in clustermodel(a.covs, a.methylation, a.model,
                          max_dist=a.max_dist,
                          linkage=a.linkage,
                          rho_min=a.rho_min,
                          min_clust_size=a.min_cluster_size,
                          multi_member=a.multi_member,
                          liptak=a.liptak,
                          bumping=a.bumping,
                          gee_args=a.gee_args,
                          skat=a.skat,
                          X=a.X,
                          X_locs=a.X_locs,
                          X_dist=a.X_dist,
                          outlier_sds=a.outlier_sds,
                          png_path=a.png_path):
        c['method'] = method if c['n_probes'] > 1 else 'lm'
        print fmt.format(**c)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "example":
        sys.exit(main_example())
    if len(sys.argv) > 1 and sys.argv[1] == "simulate":
        from . import simulate
        sys.exit(simulate.main(sys.argv[2:]))

    # want to specify existing regions, not use found ones.
    if len(sys.argv) > 1 and "--regions" in sys.argv[1:]:
        sys.exit(regional_main(sys.argv[1:]))

    main()
