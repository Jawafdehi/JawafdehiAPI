"""/metrics must be exempt from the HTTPS redirect so vmagent can scrape the pod
over plain HTTP (no Traefik → no X-Forwarded-Proto). Everything else still
redirects. SECURE_SSL_REDIRECT is off under TESTING, so force it on here.
"""

from django.test import Client, override_settings


@override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^metrics$"])
def test_metrics_is_exempt_from_ssl_redirect():
    # Plain-HTTP GET (secure=False) is NOT redirected — the scrape reaches /metrics.
    assert Client().get("/metrics", secure=False).status_code == 200


@override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^metrics$"])
def test_non_metrics_paths_still_redirect_to_https():
    resp = Client().get("/api/cases/", secure=False)
    assert resp.status_code == 301
    assert resp["Location"].startswith("https://")
