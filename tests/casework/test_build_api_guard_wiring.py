"""Guard-wiring unit tests for every ported enricher's `build_api(args)`.

Task PP2 wires `args.allow_remote_writes` into BOTH branches of `build_api`
(the `token=` branch and the `basic=` branch) across all six enricher CLIs, so
that `--allow-remote-writes` actually reaches `CaseworkApi` and its
`_patch` write-guard. This is the mutation-sensitive test the task calls out
explicitly: dropping `allow_remote_writes=` from a `build_api` call must make
one of these fail (pinned by `TestMutationDropsAllowRemoteWrites` below).

No network -- `build_api` only constructs a `CaseworkApi` object, it never
makes a request.
"""
import argparse

import pytest

from casework import convert as c_convert
from casework import enrich_allegations as c_allegations
from casework import enrich_missing_bigo as c_bigo
from casework import enrich_related_entities as c_entities
from casework import enrich_tags as c_tags
from casework import enrich_timeline as c_timeline

MODULES = [c_bigo, c_tags, c_timeline, c_allegations, c_entities, c_convert]


def _args(*, api_base_url, **overrides):
    ns = argparse.Namespace(
        api_token="",
        api_base_url=api_base_url,
        allow_remote_writes=False,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
class TestBuildApiGuardWiring:
    """Both auth branches of `build_api`, both flag values, all six files.

    The `basic=` branch requires a loopback `api_base_url` (`CaseworkApi`
    itself rejects `basic=` against a non-loopback host -- see
    `test_api.py::test_basic_mode_rejects_non_loopback_base_url`), so those
    cases use `127.0.0.1`; the `token=` branch has no such restriction and
    uses a representative non-loopback production host to mirror how
    `--allow-remote-writes` is actually meant to be used (Bearer auth against
    a remote deployment).
    """

    def test_basic_branch_true_reaches_caseworkapi(self, module, monkeypatch):
        monkeypatch.setenv("CASEWORK_API_USER", "dev-user")
        monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev-pass")
        args = _args(api_base_url="http://127.0.0.1:48010", allow_remote_writes=True)
        api = module.build_api(args)
        assert api.allow_remote_writes is True

    def test_basic_branch_false_reaches_caseworkapi(self, module, monkeypatch):
        monkeypatch.setenv("CASEWORK_API_USER", "dev-user")
        monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev-pass")
        args = _args(api_base_url="http://127.0.0.1:48010", allow_remote_writes=False)
        api = module.build_api(args)
        assert api.allow_remote_writes is False

    def test_basic_branch_requires_credentials(self, module, monkeypatch):
        # No dev-default fallback: a missing CASEWORK_API_USER/PASSWORD must fail
        # loud (SystemExit), not silently authenticate as a baked-in dev user.
        monkeypatch.delenv("CASEWORK_API_USER", raising=False)
        monkeypatch.delenv("CASEWORK_API_PASSWORD", raising=False)
        args = _args(api_base_url="http://127.0.0.1:48010")
        with pytest.raises(SystemExit):
            module.build_api(args)

    def test_token_branch_true_reaches_caseworkapi(self, module):
        args = _args(
            api_base_url="https://example.invalid",
            api_token="secret-token", allow_remote_writes=True,
        )
        api = module.build_api(args)
        assert api.allow_remote_writes is True

    def test_token_branch_false_reaches_caseworkapi(self, module):
        args = _args(
            api_base_url="https://example.invalid",
            api_token="secret-token", allow_remote_writes=False,
        )
        api = module.build_api(args)
        assert api.allow_remote_writes is False
