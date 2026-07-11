"""Auth-mode tests for the SendPulse client.

Complements ``test_api.py`` (which mocks the client at the view layer) by
exercising :mod:`newsletter.sendpulse` itself: the config-gating in
:func:`get_client` and, critically, that a static ``SENDPULSE_API_KEY`` is sent
as a Bearer token *without* the OAuth ``/oauth/access_token`` round-trip.
"""

from unittest import mock

import pytest

from newsletter.sendpulse import SendPulseClient, SendPulseError, get_client

pytestmark = pytest.mark.django_db  # get_client reads cache in the OAuth path


def _ok(status=201):
    resp = mock.Mock()
    resp.status_code = status
    resp.text = ""
    return resp


# -- get_client config-gating ------------------------------------------------


def test_get_client_none_when_unconfigured(settings):
    settings.SENDPULSE_API_KEY = ""
    settings.SENDPULSE_CLIENT_ID = ""
    settings.SENDPULSE_CLIENT_SECRET = ""
    settings.SENDPULSE_ADDRESSBOOK_ID = ""
    assert get_client() is None


def test_get_client_prefers_static_api_key(settings):
    settings.SENDPULSE_API_KEY = "sp_apikey_test"
    settings.SENDPULSE_CLIENT_ID = ""
    settings.SENDPULSE_CLIENT_SECRET = ""
    settings.SENDPULSE_ADDRESSBOOK_ID = "719648"
    c = get_client()
    assert isinstance(c, SendPulseClient)
    assert c._api_key == "sp_apikey_test"


def test_get_client_falls_back_to_oauth_pair(settings):
    settings.SENDPULSE_API_KEY = ""
    settings.SENDPULSE_CLIENT_ID = "id"
    settings.SENDPULSE_CLIENT_SECRET = "secret"
    settings.SENDPULSE_ADDRESSBOOK_ID = "719648"
    c = get_client()
    assert isinstance(c, SendPulseClient)
    assert c._api_key == ""
    assert c._client_id == "id"


def test_get_client_requires_addressbook(settings):
    """Auth alone isn't enough — without an address book there's nowhere to add."""
    settings.SENDPULSE_API_KEY = "sp_apikey_test"
    settings.SENDPULSE_ADDRESSBOOK_ID = ""
    assert get_client() is None


def test_get_client_requires_full_oauth_pair(settings):
    """A half-configured OAuth pair (id without secret) is not usable."""
    settings.SENDPULSE_API_KEY = ""
    settings.SENDPULSE_CLIENT_ID = "id"
    settings.SENDPULSE_CLIENT_SECRET = ""
    settings.SENDPULSE_ADDRESSBOOK_ID = "719648"
    assert get_client() is None


# -- static-key request path -------------------------------------------------


@mock.patch("newsletter.sendpulse.requests.post")
@mock.patch("newsletter.sendpulse.requests.request")
def test_static_key_sends_bearer_and_skips_oauth(mock_request, mock_post):
    """The API key goes out as a Bearer header; the token endpoint is never hit."""
    mock_request.return_value = _ok(201)
    client = SendPulseClient("719648", api_key="sp_apikey_test")

    client.add_subscriber("reader@example.org", variables={"consent_source": "x"})

    mock_post.assert_not_called()  # no /oauth/access_token exchange
    mock_request.assert_called_once()
    _, kwargs = mock_request.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer sp_apikey_test"
    args, _ = mock_request.call_args
    assert args[0] == "POST"
    assert args[1].endswith("/addressbooks/719648/emails")


@mock.patch("newsletter.sendpulse.requests.post")
@mock.patch("newsletter.sendpulse.requests.request")
def test_static_key_does_not_retry_on_401(mock_request, mock_post):
    """A 401 refresh only helps OAuth; a static key is re-sent as-is, so no retry."""
    mock_request.return_value = _ok(401)
    client = SendPulseClient("719648", api_key="sp_apikey_test")

    with pytest.raises(SendPulseError) as exc:
        client.add_subscriber("reader@example.org")

    assert exc.value.status == 401
    mock_request.assert_called_once()  # not retried
    mock_post.assert_not_called()


@mock.patch("newsletter.sendpulse.requests.post")
@mock.patch("newsletter.sendpulse.requests.request")
def test_oauth_mode_fetches_and_uses_token(mock_request, mock_post, settings):
    """Without an API key, the client exchanges id/secret for a Bearer token."""
    from django.core.cache import cache

    cache.clear()
    token_resp = mock.Mock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "tok_abc", "expires_in": 3600}
    mock_post.return_value = token_resp
    mock_request.return_value = _ok(201)

    client = SendPulseClient("719648", client_id="id", client_secret="secret")
    client.add_subscriber("reader@example.org")

    mock_post.assert_called_once()  # token exchange happened
    _, kwargs = mock_request.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tok_abc"
    cache.clear()


@mock.patch("newsletter.sendpulse.requests.post")
@mock.patch("newsletter.sendpulse.requests.request")
def test_oauth_mode_retries_once_on_401(mock_request, mock_post):
    """A cached-but-rejected token: the 401 forces one refresh and one retry."""
    from django.core.cache import cache

    from newsletter.sendpulse import _TOKEN_CACHE_KEY

    # Seed a stale token so the first request uses it (no fetch); the 401 is what
    # drives the single refresh — the branch this test guards.
    cache.set(_TOKEN_CACHE_KEY, "tok_stale", 3600)
    token_resp = mock.Mock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "tok_fresh", "expires_in": 3600}
    mock_post.return_value = token_resp
    mock_request.side_effect = [_ok(401), _ok(201)]

    client = SendPulseClient("719648", client_id="id", client_secret="secret")
    client.add_subscriber("reader@example.org")

    assert mock_request.call_count == 2  # initial 401 + retry
    mock_post.assert_called_once()  # exactly one token refresh (no initial fetch)
    first_headers = mock_request.call_args_list[0].kwargs["headers"]
    retry_headers = mock_request.call_args_list[1].kwargs["headers"]
    assert first_headers["Authorization"] == "Bearer tok_stale"
    assert retry_headers["Authorization"] == "Bearer tok_fresh"
    cache.clear()


def test_get_client_strips_whitespace(settings):
    """A key with stray whitespace (copy-paste / env round-trip) is trimmed."""
    settings.SENDPULSE_API_KEY = "  sp_apikey_test\n"
    settings.SENDPULSE_CLIENT_ID = ""
    settings.SENDPULSE_CLIENT_SECRET = ""
    settings.SENDPULSE_ADDRESSBOOK_ID = " 719648 "
    c = get_client()
    assert c._api_key == "sp_apikey_test"
    assert c._addressbook_id == "719648"


# -- double opt-in -----------------------------------------------------------


@mock.patch("newsletter.sendpulse.requests.request")
def test_single_opt_in_omits_confirmation_params(mock_request):
    """Default (no confirmation config) sends no DOI params — backward compatible."""
    mock_request.return_value = _ok(201)
    client = SendPulseClient("719648", api_key="k")

    client.add_subscriber("reader@example.org")

    _, kwargs = mock_request.call_args
    body = kwargs["json"]
    assert "confirmation" not in body
    assert "sender_email" not in body


@mock.patch("newsletter.sendpulse.requests.request")
def test_double_opt_in_sends_confirmation_params(mock_request):
    """DOI attaches confirmation/sender_email/message_lang/template_id top-level."""
    mock_request.return_value = _ok(200)
    client = SendPulseClient(
        "719648",
        api_key="k",
        confirmation=True,
        sender_email="inquiry@jawafdehi.org",
        confirmation_template_id="tmpl-123",
        message_lang="en",
    )

    client.add_subscriber("reader@example.org", name="Sita")

    body = mock_request.call_args.kwargs["json"]
    assert body["confirmation"] == "force"
    assert body["sender_email"] == "inquiry@jawafdehi.org"
    assert body["message_lang"] == "en"
    assert body["template_id"] == "tmpl-123"
    # The contact + its variables still ride along in "emails".
    assert body["emails"][0]["email"] == "reader@example.org"


@mock.patch("newsletter.sendpulse.requests.request")
def test_double_opt_in_requires_sender_email(mock_request):
    """confirmation=True without a sender is inert — SendPulse would reject it."""
    mock_request.return_value = _ok(201)
    client = SendPulseClient("719648", api_key="k", confirmation=True, sender_email="")

    client.add_subscriber("reader@example.org")

    assert "confirmation" not in mock_request.call_args.kwargs["json"]


@mock.patch("newsletter.sendpulse.requests.request")
def test_unsupported_message_lang_falls_back_to_en(mock_request):
    """Nepali isn't a supported message_lang, so it degrades to English."""
    mock_request.return_value = _ok(200)
    client = SendPulseClient(
        "719648", api_key="k", confirmation=True,
        sender_email="inquiry@jawafdehi.org", message_lang="ne",
    )

    client.add_subscriber("reader@example.org")

    assert mock_request.call_args.kwargs["json"]["message_lang"] == "en"


@mock.patch("newsletter.sendpulse.requests.request")
def test_double_opt_in_omits_template_id_when_unset(mock_request):
    """No template_id → SendPulse uses its default confirmation email."""
    mock_request.return_value = _ok(200)
    client = SendPulseClient(
        "719648", api_key="k", confirmation=True,
        sender_email="inquiry@jawafdehi.org",
    )

    client.add_subscriber("reader@example.org")

    body = mock_request.call_args.kwargs["json"]
    assert body["confirmation"] == "force"
    assert "template_id" not in body


def test_get_client_wires_double_opt_in_settings(settings):
    """get_client threads the DOI settings onto the client."""
    settings.SENDPULSE_API_KEY = "k"
    settings.SENDPULSE_ADDRESSBOOK_ID = "719648"
    settings.SENDPULSE_CONFIRMATION = True
    settings.SENDPULSE_SENDER_EMAIL = "  inquiry@jawafdehi.org "
    settings.SENDPULSE_CONFIRMATION_TEMPLATE_ID = " tmpl-123 "
    settings.SENDPULSE_MESSAGE_LANG = "en"
    c = get_client()
    assert c._doi is True
    assert c._sender_email == "inquiry@jawafdehi.org"
    assert c._confirmation_template_id == "tmpl-123"
