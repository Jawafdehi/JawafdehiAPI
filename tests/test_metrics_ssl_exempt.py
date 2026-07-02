"""/metrics must be exempt from the HTTPS redirect so vmagent can scrape the pod
over plain HTTP (no Traefik → no X-Forwarded-Proto). Everything else still
redirects. SECURE_SSL_REDIRECT is off under TESTING, so force it on here.
"""

from django.test import Client, override_settings

# Only force SECURE_SSL_REDIRECT on (it's disabled under TESTING); the exempt list
# under test is the REAL SECURE_REDIRECT_EXEMPT from config.settings, not an
# override — so removing it there would fail these tests.


@override_settings(SECURE_SSL_REDIRECT=True)
def test_metrics_is_exempt_from_ssl_redirect():
    # Plain-HTTP GET (secure=False) is NOT redirected — the scrape reaches /metrics.
    # Trailing slash is also exempt (r"^metrics/?$").
    assert Client().get("/metrics", secure=False).status_code == 200
    assert Client().get("/metrics/", secure=False).status_code in (200, 404)


@override_settings(SECURE_SSL_REDIRECT=True)
def test_non_metrics_paths_still_redirect_to_https():
    resp = Client().get("/api/cases/", secure=False)
    assert resp.status_code == 301
    assert resp["Location"].startswith("https://")
