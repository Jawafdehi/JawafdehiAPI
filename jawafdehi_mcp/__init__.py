"""Jawafdehi MCP Server - Tools for querying Nepal's judicial data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jawafdehi")
except PackageNotFoundError:  # source tree before installation
    __version__ = "0+unknown"
