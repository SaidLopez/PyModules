"""Namespace marker for PyModules contrib packages.

Each subpackage (``pymodules.contrib.api``, ``pymodules.contrib.db``,
``pymodules.contrib.discovery``, ``pymodules.contrib.messaging``,
``pymodules.contrib.health``) is independently import-gated behind a PyPI
extra. This file deliberately re-exports nothing - importing
``pymodules.contrib`` must not pull in any optional dependency.
"""
