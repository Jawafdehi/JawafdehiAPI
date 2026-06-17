"""Token-authenticated HTTP client for the casework "upstream" API.

This is the shared write-path plumbing the ``review_poller`` once owned
privately: it authenticates to the casework API with a long-lived DRF token
(``Authorization: Token <key>``) and posts back to it. Extracted so both the
poller and the ``reprocess_source_markdown`` management command talk to upstream
through one place.

Auth: the token is a DRF auth token for a dedicated service account holding the
write permission (``CanManageDocumentSources``, i.e. ReviewAssistant+). Create
one with ``manage.py drf_create_token <service-account-username>`` and supply it
via ``CASEWORK_POLLER_TOKEN``. The API base is ``CASEWORK_API_BASE``.
"""

import requests
from django.conf import settings


class UpstreamError(Exception):
    pass


class UpstreamClient:
    """Thin token-auth client for the casework API.

    ``on_log`` / ``on_err`` are optional callables (e.g. a command's
    ``self.stdout.write`` / ``self.stderr.write``) used for human-facing
    progress; they default to no-ops so the client is usable outside a command.
    """

    def __init__(self, base=None, token=None, on_log=None, on_err=None):
        self.base = (base or settings.CASEWORK_API_BASE).rstrip("/")
        self.token = token if token is not None else settings.CASEWORK_POLLER_TOKEN
        if not self.token:
            raise UpstreamError(
                "CASEWORK_POLLER_TOKEN is not set. Create a DRF token for the "
                "service account (manage.py drf_create_token <user>) and set it "
                "in the environment."
            )
        self._log = on_log or (lambda _msg: None)
        self._err = on_err or (lambda _msg: None)

    def _headers(self):
        return {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }

    def get(self, path, timeout=30):
        url = f"{self.base}{path}"
        return requests.get(url, headers=self._headers(), timeout=timeout)

    def post(self, path, payload, timeout=60):
        url = f"{self.base}{path}"
        return requests.post(
            url, json=payload, headers=self._headers(), timeout=timeout
        )

    def attach_markdown(self, items, *, overwrite=False):
        """Attach locally-converted markdown back to sources upstream.

        ``items`` is an iterable of ``{"source_id", "markdown"}`` dicts (the
        candidate shape produced by ``converter.convert_case_to_attach_candidates``).
        Each is POSTed to ``/sources/{id}/markdown/``; the server stores it as a
        MARKDOWN-role url, idempotently (it skips a source that already has one
        unless ``overwrite`` is set).

        Returns a summary dict: ``{attached, skipped, failed}``.
        """
        summary = {"attached": 0, "skipped": 0, "failed": 0}
        for item in items or []:
            sid = item.get("source_id")
            try:
                r = self.post(
                    f"/sources/{sid}/markdown/",
                    {"markdown": item.get("markdown", ""), "overwrite": overwrite},
                    timeout=60,
                )
                if r.status_code == 200:
                    body = r.json()
                    if body.get("created"):
                        summary["attached"] += 1
                        self._log(f"    attached MARKDOWN url to source {sid}")
                    else:
                        summary["skipped"] += 1
                else:
                    summary["failed"] += 1
                    self._err(
                        f"    markdown attach failed for {sid}: "
                        f"HTTP {r.status_code} {r.text[:150]}"
                    )
            except Exception as e:  # noqa: BLE001 - attach is best-effort per source
                summary["failed"] += 1
                self._err(f"    markdown attach error for {sid}: {e}")
        return summary
