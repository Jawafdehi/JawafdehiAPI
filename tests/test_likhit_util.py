from types import SimpleNamespace

from django.test import override_settings

from cases.services.likhit_util import convert_bytes_to_markdown, idempotency_key


class FakeConverter:
    def __init__(self):
        self.calls = 0

    def convert_uri(self, uri):
        self.calls += 1
        return SimpleNamespace(markdown=f"converted:{uri}")


@override_settings(
    CACHES={
        "doc_conv": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "test-doc-conv-cache",
        }
    }
)
def test_likhit_conversion_uses_doc_conv_cache():
    converter = FakeConverter()

    first = convert_bytes_to_markdown(
        b"source bytes",
        filename="source.pdf",
        converter=converter,
    )
    second = convert_bytes_to_markdown(
        b"source bytes",
        filename="source.pdf",
        converter=converter,
    )

    assert first.markdown.startswith("converted:file://")
    assert first.content_hash == second.content_hash
    assert first.cache_key == second.cache_key
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert converter.calls == 1


def test_idempotency_key_uses_case_and_content_hash():
    key = idempotency_key("JAWA-CASE-1", "abc123")
    assert key == "case:JAWA-CASE-1:evidence:abc123"
