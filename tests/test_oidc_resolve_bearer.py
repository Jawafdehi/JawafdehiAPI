"""resolve_service_bearer: how the scraper cron commands get their write bearer.

Priority: explicit/static token → sa-ingestion OIDC (INGESTION_OIDC_*) → the shared
casework service account (CASEWORK_OIDC_* settings) → None. No network: the
client-credentials grant is mocked at the provider / convenience-function seam.
"""

import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from review import oidc_client_credentials as O


class ResolveServiceBearerTests(SimpleTestCase):
    def test_explicit_token_wins_over_everything(self):
        with mock.patch.dict(os.environ, {"INGESTION_API_TOKEN": "envtok"}, clear=True):
            self.assertEqual(O.resolve_service_bearer("explicit"), "explicit")

    def test_static_env_token(self):
        with mock.patch.dict(os.environ, {"INGESTION_API_TOKEN": "envtok"}, clear=True):
            self.assertEqual(O.resolve_service_bearer(), "envtok")

    @override_settings(OIDC_ISSUER="https://auth.example", CASEWORK_OIDC_SCOPE="sc", CASEWORK_OIDC_AUDIENCE="aud")
    def test_ingestion_oidc_client_credentials_mint(self):
        env = {"INGESTION_OIDC_CLIENT_ID": "cid", "INGESTION_OIDC_CLIENT_SECRET": "sec"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            O.ClientCredentialsTokenProvider, "get_token", return_value="minted"
        ) as get_token:
            self.assertEqual(O.resolve_service_bearer(), "minted")
            get_token.assert_called_once()

    @override_settings(CASEWORK_OIDC_CLIENT_ID="ccid", CASEWORK_OIDC_CLIENT_SECRET="csec")
    def test_casework_service_account_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            O, "get_access_token", return_value="casework-token"
        ) as get_access_token:
            self.assertEqual(O.resolve_service_bearer(), "casework-token")
            get_access_token.assert_called_once()

    @override_settings(CASEWORK_OIDC_CLIENT_ID="", CASEWORK_OIDC_CLIENT_SECRET="")
    def test_nothing_configured_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(O.resolve_service_bearer())

    @override_settings(OIDC_ISSUER="https://auth.example", CASEWORK_OIDC_SCOPE="sc")
    def test_configured_grant_failure_propagates(self):
        # A configured-but-failing grant surfaces the OIDC error rather than
        # silently returning None (which would look like "no token").
        env = {"INGESTION_OIDC_CLIENT_ID": "cid", "INGESTION_OIDC_CLIENT_SECRET": "sec"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            O.ClientCredentialsTokenProvider, "get_token",
            side_effect=O.OIDCTokenError("boom"),
        ):
            with self.assertRaises(O.OIDCTokenError):
                O.resolve_service_bearer()
