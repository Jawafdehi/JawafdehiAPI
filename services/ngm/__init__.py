"""Test-collection package marker (not shipped).

With ``services/__init__.py`` present, this makes pytest name the NGM test
package uniquely as ``services.ngm.tests`` under the default prepend import
mode, so NGMs ``tests/test_api.py`` does not collide with NESs on the bare
module name ``tests.test_api``. A two-segment ``ngm.tests`` name is NOT used
because the Jawafdehi ``ngm`` proxy app already owns the top-level ``ngm``
package. The shipped wheel is ``ngm_service`` (this ``services.ngm`` package is
test-only). See the repo-root pyproject ``[tool.pytest.ini_options]``.
"""
