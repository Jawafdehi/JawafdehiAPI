"""Packaging contract for the embedded MCP component."""

from importlib.metadata import version
from pathlib import Path

import jawafdehi_mcp
from jawafdehi_mcp.server import app


def test_mcp_version_comes_from_monolith_package_metadata():
    assert jawafdehi_mcp.__version__ == version("jawafdehi")


def test_mcp_server_info_reports_monolith_package_version():
    options = app.create_initialization_options()

    assert options.server_name == "jawafdehi-mcp"
    assert options.server_version == jawafdehi_mcp.__version__


def test_mcp_license_is_packaged_with_the_module():
    license_path = Path(jawafdehi_mcp.__file__).with_name("LICENSE")
    assert license_path.is_file()
    assert license_path.read_text(encoding="utf-8").startswith(
        "Hippocratic License Version 3.0"
    )
