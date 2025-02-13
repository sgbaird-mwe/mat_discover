#!/usr/bin/env python3
# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
from warnings import warn
from os.path import dirname, abspath, join
import sys
import sphinx_rtd_theme  # noqa

try:
    # mat_discover is installed
    import mat_discover
except ImportError:
    # mat_discover is run from its source checkout
    current_dir = dirname(__file__)
    mypath = abspath(join(current_dir, "..", "..", "mat_discover"))
    mypath2 = abspath(join(current_dir, "..", ".."))
    sys.path.insert(0, mypath)
    sys.path.insert(0, mypath2)
    print("path added: ", mypath)
    print("path added: ", mypath2)
    import mat_discover

    # try:
    #     import mat_discover
    # except ImportError:
    #     warn("You might need to run `conda install flit; flit install --pth-file`")


# -- Project information -----------------------------------------------------

project = "mat_discover"
copyright = "2021, Sterling G. Baird"
author = "Sterling G. Baird"

# The full version, including alpha/beta/rc tags
version = ".".join(mat_discover.__version__.split(".")[:2])
release = mat_discover.__version__


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    "myst_parser",
    "sphinx.ext.napoleon",
    "sphinx.ext.autodoc",
    "sphinx.ext.githubpages",
    "sphinx_rtd_theme",
]

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = []

# https://sublime-and-sphinx-guide.readthedocs.io/en/latest/code_blocks.html
pygments_style = "sphinx"

# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = "sphinx_rtd_theme"

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = []  # throws a WARNING, removed "_static"

html_extra_path = ["googlea441f484329a8f75.html"]

autodoc_mock_imports = ["numba", "torch", "pymatgen", "hdbscan"]

# https://github.com/sphinx-doc/sphinx/issues/7000#issuecomment-677916705
source_suffix = [".rst", ".md"]
