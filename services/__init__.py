"""Test-only package marker. Makes pytest name the per-service test packages
uniquely (``services.nes.tests`` / ``services.ngm.tests``) under the default
prepend import mode, avoiding the ``tests.test_api`` collision between NES and
NGM in a single ``pytest services shared`` run. Not part of any shipped wheel
(the deployable packages are ``nes_service`` / ``ngm_service`` / the Jawafdehi
apps, not ``services``). See repo-root pyproject pytest config.
"""
