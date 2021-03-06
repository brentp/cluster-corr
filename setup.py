from ez_setup import use_setuptools
use_setuptools()

from setuptools import setup, find_packages

import clustermodel
# Run 2to3 builder if we're on Python 3.x, from
#   http://wiki.python.org/moin/PortingPythonToPy3k
try:
    from distutils.command.build_py import build_py_2to3 as build_py
except ImportError:
    # 2.x
    from distutils.command.build_py import build_py
command_classes = {'build_py': build_py}

setup(name='clustermodel',
      version=clustermodel.__version__,
      description="statistical models on clusters of correlated genomic data",
      packages=find_packages(),
      url="https://github.com/brentp/cluster-corr/",
      long_description=open('README.md').read(),
      platforms='any',
      classifiers=[
        'Topic :: Scientific/Engineering :: Bio-Informatics',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.2',
        'Programming Language :: Python :: 3.3',
          ],
      keywords='bioinformatics methylation correlation',
      author='brentp',
      author_email='bpederse@gmail.com',
      license='BSD 3-clause',
      include_package_data=True,
      #package_data = {"": ['*.R']}
      tests_require=['nose'],
      test_suite='nose.collector',
      zip_safe=False,
      install_requires=['numpy', 'pandas', 'aclust', 'toolshed'],
      #scripts=[],
      entry_points={
      },
      cmdclass=command_classes,
  )
