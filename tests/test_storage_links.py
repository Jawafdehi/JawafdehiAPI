"""Tests for cases.services.storage_links host normalization.

Links are frozen into a source's ``url`` JSON at write time, so a missing
``AWS_S3_CUSTOM_DOMAIN`` must never let a raw Cloudflare R2 *endpoint* host
(``<account>.r2.cloudflarestorage.com/<bucket>/<key>``) be persisted. The
helpers rewrite any such URL to the public ``JAWAFDEHI_S3_BASE`` host.
"""

import pytest
from django.test import override_settings

from cases.services.storage_links import (
    absolute_media_url,
    normalize_storage_host,
)

R2 = (
    "https://4c96557d73194d4f245ba23bd6063ad5.r2.cloudflarestorage.com"
    "/jawafdehi/case_uploads/abc123.pdf"
)
S3 = "https://s3.jawafdehi.org/case_uploads/abc123.pdf"


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
def test_r2_endpoint_host_rewritten_to_public_host():
    assert normalize_storage_host(R2) == S3


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
def test_absolute_media_url_normalizes_r2_endpoint():
    # Already-absolute R2 URL: passes the scheme check, then gets normalized.
    assert absolute_media_url(R2) == S3


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
def test_bucket_segment_is_dropped_but_nested_key_preserved():
    src = "https://acct.r2.cloudflarestorage.com/mybucket/case_uploads/sub/dir/file.md"
    assert (
        normalize_storage_host(src)
        == "https://s3.jawafdehi.org/case_uploads/sub/dir/file.md"
    )


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org/")
def test_trailing_slash_on_base_does_not_double_up():
    assert normalize_storage_host(R2) == S3


@pytest.mark.parametrize(
    "url",
    [
        "https://s3.jawafdehi.org/case_uploads/abc.pdf",  # already public
        "https://web.archive.org/web/x/https://example.com",  # external
        "https://ciaa.gov.np/report.pdf",
        "",
        None,
    ],
)
@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
def test_non_r2_urls_unchanged(url):
    assert normalize_storage_host(url) == url


@override_settings(JAWAFDEHI_S3_BASE="")
def test_no_public_base_leaves_url_untouched():
    # Without a configured base we cannot rewrite; return as-is rather than
    # producing a broken host.
    assert normalize_storage_host(R2) == R2


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
def test_bare_r2_host_without_key_unchanged():
    # No ``/<bucket>/<key>`` to rewrite — leave it alone rather than emit a base
    # that points nowhere useful.
    bare = "https://acct.r2.cloudflarestorage.com/onlybucket"
    assert normalize_storage_host(bare) == bare
