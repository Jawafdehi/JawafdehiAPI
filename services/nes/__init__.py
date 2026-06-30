"""Test-collection package marker (not shipped).

With ``services/__init__.py`` present, this makes pytest name the NES test
package uniquely as ``services.nes.tests`` under the default prepend import
mode, so NESs ``tests/test_api.py`` does not collide with NGMs on the bare
module name ``tests.test_api`` in a single ``pytest services shared`` run. The
shipped wheel is ``nes_service`` (this ``services.nes`` package is test-only).
See the repo-root pyproject ``[tool.pytest.ini_options]`` for the full rationale.
"""
