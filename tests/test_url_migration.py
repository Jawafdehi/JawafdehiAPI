"""
Tests for URL field migration from URLField to JSONField.

These tests verify both the migration process and post-migration behavior:
- Tests the actual migration logic that converts string URLs to lists
- Confirms the field accepts and stores lists of URLs after migration
- Validates serialization works correctly with the new JSONField type
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.test import TransactionTestCase

from cases.models import DocumentSource


class TestURLMigrationProcess(TransactionTestCase):
    """
    Test the actual migration process from URLField to JSONField.

    Uses TransactionTestCase to allow migration testing with database schema changes.
    """

    @staticmethod
    def get_historical_model(connection, migration_tuple, app_label, model_name):
        """
        Helper to get historical model at a specific migration state.

        Args:
            connection: Database connection
            migration_tuple: Tuple of (app_label, migration_name)
            app_label: App label for the model
            model_name: Model name

        Returns:
            Historical model class at the specified migration state
        """
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        project_state = executor.loader.project_state(migration_tuple)
        return project_state.apps.get_model(app_label, model_name)

    def setUp(self):
        """Set up test by migrating to the state before our migration."""
        from django.utils import timezone

        # Migrate to the state before our URL migration
        try:
            call_command("migrate", "cases", "0009_merge_20260112_0309", verbosity=0)
        except IrreversibleError:
            self.skipTest(
                "Cannot migrate back to 0009 because entity relationship migration is intentionally irreversible."
            )

        # Get the historical model at migration 0009
        DocumentSource = self.get_historical_model(
            connection, ("cases", "0009_merge_20260112_0309"), "cases", "DocumentSource"
        )

        # Create test data with old URLField format (single string) using ORM
        now = timezone.now()
        DocumentSource.objects.bulk_create(
            [
                DocumentSource(
                    source_id="source:test:001",
                    title="Test Source 1",
                    description="Description 1",
                    url="https://example.com/doc1.pdf",
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentSource(
                    source_id="source:test:002",
                    title="Test Source 2",
                    description="Description 2",
                    url="https://example.com/doc2.pdf",
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentSource(
                    source_id="source:test:003",
                    title="Empty URL Source",
                    description="Description 3",
                    url="",
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentSource(
                    source_id="source:test:004",
                    title="Null URL Source",
                    description="Description 4",
                    url=None,
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )

    def test_migration_converts_string_urls_to_lists(self):
        """Test that migration converts single URL strings to JSON arrays."""
        # Run the migration
        call_command("migrate", "cases", "0010_change_url_to_jsonfield", verbosity=0)

        # Get the historical model at migration 0010 state
        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0010_change_url_to_jsonfield"),
            "cases",
            "DocumentSource",
        )

        # Verify the data was converted correctly
        source1 = DocumentSource.objects.get(source_id="source:test:001")
        assert isinstance(source1.url, list), "URL should be converted to list"
        assert source1.url == [
            "https://example.com/doc1.pdf"
        ], "URL should be wrapped in list"

        source2 = DocumentSource.objects.get(source_id="source:test:002")
        assert source2.url == ["https://example.com/doc2.pdf"]

    def test_migration_handles_empty_urls(self):
        """Test that migration converts empty strings to empty lists."""
        call_command("migrate", "cases", "0010_change_url_to_jsonfield", verbosity=0)

        # Get the historical model at migration 0010 state
        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0010_change_url_to_jsonfield"),
            "cases",
            "DocumentSource",
        )

        source = DocumentSource.objects.get(source_id="source:test:003")
        assert source.url == [], "Empty string should become empty list"

    def test_migration_handles_null_urls(self):
        """Test that migration converts NULL values to empty lists."""
        call_command("migrate", "cases", "0010_change_url_to_jsonfield", verbosity=0)

        # Get the historical model at migration 0010 state
        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0010_change_url_to_jsonfield"),
            "cases",
            "DocumentSource",
        )

        source = DocumentSource.objects.get(source_id="source:test:004")
        assert source.url == [], "NULL should become empty list"

    def test_reverse_migration_converts_lists_back_to_strings(self):
        """Test that reverse migration converts lists back to single URL strings."""
        # First migrate forward
        call_command("migrate", "cases", "0010_change_url_to_jsonfield", verbosity=0)

        # Get the historical model at migration 0010 state
        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0010_change_url_to_jsonfield"),
            "cases",
            "DocumentSource",
        )

        # Verify forward migration worked
        source = DocumentSource.objects.get(source_id="source:test:001")
        assert isinstance(source.url, list)

        # Now migrate backward
        call_command("migrate", "cases", "0009_merge_20260112_0309", verbosity=0)

        # Verify reverse migration worked (takes first URL from list)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT url FROM cases_documentsource WHERE source_id = %s",
                ["source:test:001"],
            )
            url_value = cursor.fetchone()[0]
            assert (
                url_value == "https://example.com/doc1.pdf"
            ), "Should revert to first URL string"

    def tearDown(self):
        """Restore DB schema to latest migrations after each test before flush."""
        call_command("migrate", verbosity=0)
        super().tearDown()

    @classmethod
    def tearDownClass(cls):
        """Restore DB schema to latest migrations after migration tests."""
        # Re-apply all migrations to restore schema for subsequent tests
        call_command("migrate", verbosity=0)
        super().tearDownClass()


@pytest.mark.django_db
class TestURLFieldPostMigration:
    """Test suite for URL field behavior after migration to JSONField."""

    def test_url_field_stores_dicts(self):
        """Legacy string url is normalized to a {link, role: RAW} dict on save."""
        source = DocumentSource.objects.create(
            title="Test Source", url=["https://example.com"]
        )

        assert isinstance(source.url, list)
        assert source.url == [{"link": "https://example.com", "role": "RAW"}]

    def test_multiple_urls_storage(self):
        """Verify multiple URLs can be stored; legacy strings default to RAW."""
        urls = [
            "https://example.com",
            {"link": "https://backup.example.com", "role": "RAW"},
        ]
        source = DocumentSource.objects.create(title="Test Source", url=urls)

        source.refresh_from_db()
        assert source.url == [
            {"link": "https://example.com", "role": "RAW"},
            {"link": "https://backup.example.com", "role": "RAW"},
        ]

    def test_url_serialization(self):
        """Verify URLs serialize correctly in API responses."""
        from cases.serializers import DocumentSourceSerializer

        source = DocumentSource.objects.create(
            title="Test Source", url=["https://example.com", "https://backup.com"]
        )

        serializer = DocumentSourceSerializer(source)
        # Backward-compat url field returns strings
        assert serializer.data["url"] == [
            "https://example.com",
            "https://backup.com",
        ]
        # New urls field returns dicts
        assert serializer.data["urls"] == [
            {"link": "https://example.com", "role": "RAW"},
            {"link": "https://backup.com", "role": "RAW"},
        ]


@pytest.mark.django_db
class TestSourceLinkDictFormat:
    """Tests for the source link dict format support."""

    def test_validate_url_list_accepts_dict_items(self):
        """validate_url_list should accept dict items with link+role."""
        from cases.models import validate_url_list

        # Should not raise
        validate_url_list(
            [
                {"link": "https://example.com/doc1", "role": "RAW"},
                {"link": "https://example.com/doc2.md", "role": "MARKDOWN"},
                {"link": "https://example.com/permalink", "role": "PERMALINK"},
            ]
        )

    def test_validate_url_list_rejects_plain_strings(self):
        """validate_url_list should reject plain string items (dicts only)."""
        from django.core.exceptions import ValidationError

        from cases.models import validate_url_list

        with pytest.raises(ValidationError):
            validate_url_list(
                [
                    "https://example.com/plain",
                    {"link": "https://example.com/dict", "role": "RAW"},
                ]
            )

    def test_validate_url_list_rejects_dict_without_role(self):
        """role is now mandatory — a dict missing it is rejected."""
        from django.core.exceptions import ValidationError

        from cases.models import validate_url_list

        with pytest.raises(ValidationError):
            validate_url_list([{"link": "https://example.com/doc"}])

    def test_validate_url_list_rejects_dict_with_none_role(self):
        """An explicit None role is also rejected by validate_url_list."""
        from django.core.exceptions import ValidationError

        from cases.models import validate_url_list

        with pytest.raises(ValidationError):
            validate_url_list([{"link": "https://example.com/doc", "role": None}])

    def test_validate_url_list_rejects_invalid_role(self):
        """validate_url_list should reject dict with invalid role."""
        from django.core.exceptions import ValidationError

        from cases.models import validate_url_list

        with pytest.raises(ValidationError):
            validate_url_list([{"link": "https://example.com/doc", "role": "INVALID"}])

    def test_validate_url_list_rejects_dict_missing_link(self):
        """validate_url_list should reject dict missing link key."""
        from django.core.exceptions import ValidationError

        from cases.models import validate_url_list

        with pytest.raises(ValidationError):
            validate_url_list([{"role": "RAW"}])

    def test_create_serializer_accepts_dict_urls(self):
        """DocumentSourceCreateSerializer should accept dict format URLs."""
        from cases.serializers import DocumentSourceCreateSerializer

        data = {
            "title": "Dict URL Test",
            "url": [
                {"link": "https://example.com/plain", "role": "RAW"},
                {"link": "https://example.com/with-role", "role": "MARKDOWN"},
            ],
        }
        serializer = DocumentSourceCreateSerializer(data=data)
        assert serializer.is_valid(), f"Errors: {serializer.errors}"
        assert serializer.validated_data["url"] == [
            {"link": "https://example.com/plain", "role": "RAW"},
            {"link": "https://example.com/with-role", "role": "MARKDOWN"},
        ]

    def test_create_serializer_rejects_plain_strings(self):
        """Plain string URLs are no longer accepted by the create serializer."""
        from cases.serializers import DocumentSourceCreateSerializer

        data = {
            "title": "Plain URL Test",
            "url": ["https://example.com/doc"],
        }
        serializer = DocumentSourceCreateSerializer(data=data)
        assert not serializer.is_valid()
        assert "url" in serializer.errors

    def test_create_serializer_rejects_dict_without_role(self):
        """A dict without an explicit role is rejected (role mandatory)."""
        from cases.serializers import DocumentSourceCreateSerializer

        data = {
            "title": "No Role Test",
            "url": [{"link": "https://example.com/doc"}],
        }
        serializer = DocumentSourceCreateSerializer(data=data)
        assert not serializer.is_valid()
        assert "url" in serializer.errors

    def test_serializer_outputs_dict_format(self):
        """DocumentSourceSerializer should output {link, role} dicts."""
        from cases.serializers import DocumentSourceSerializer

        source = DocumentSource.objects.create(
            title="Dict Output Test",
            url=[
                "https://example.com/plain",
                {"link": "https://example.com/markdown", "role": "MARKDOWN"},
            ],
        )
        serializer = DocumentSourceSerializer(source)
        # Legacy string entry is normalized to RAW on save.
        assert serializer.data["url"] == [
            "https://example.com/plain",
            "https://example.com/markdown",
        ]
        assert serializer.data["urls"] == [
            {"link": "https://example.com/plain", "role": "RAW"},
            {"link": "https://example.com/markdown", "role": "MARKDOWN"},
        ]

    def test_source_link_role_enum_values(self):
        """SourceLinkRole enum should have expected members."""
        from cases.models import SourceLinkRole

        assert SourceLinkRole.RAW.value == "RAW"
        assert SourceLinkRole.MARKDOWN.value == "MARKDOWN"
        assert SourceLinkRole.PERMALINK.value == "PERMALINK"
        assert len(list(SourceLinkRole)) == 3

    def test_create_serializer_rejects_invalid_url(self):
        """SourceLinkField should reject invalid URLs."""
        from cases.serializers import DocumentSourceCreateSerializer

        data = {
            "title": "Invalid URL Test",
            "url": ["not-a-url"],
        }
        serializer = DocumentSourceCreateSerializer(data=data)
        assert not serializer.is_valid()

    def test_create_serializer_rejects_dict_invalid_url(self):
        """SourceLinkField should reject dict with invalid URL."""
        from cases.serializers import DocumentSourceCreateSerializer

        data = {
            "title": "Invalid Dict URL Test",
            "url": [{"link": "not-a-url", "role": "RAW"}],
        }
        serializer = DocumentSourceCreateSerializer(data=data)
        assert not serializer.is_valid()

    def test_create_serializer_strips_whitespace(self):
        """SourceLinkField should strip whitespace from the link in a dict."""
        from cases.serializers import DocumentSourceCreateSerializer

        data = {
            "title": "Whitespace Test",
            "url": [{"link": "  https://example.com/doc  ", "role": "RAW"}],
        }
        serializer = DocumentSourceCreateSerializer(data=data)
        assert serializer.is_valid(), f"Errors: {serializer.errors}"
        assert serializer.validated_data["url"] == [
            {"link": "https://example.com/doc", "role": "RAW"}
        ]

    def test_create_serializer_sanitizes_extra_dict_keys(self):
        """SourceLinkField should strip extra keys from dict input."""
        from cases.serializers import DocumentSourceCreateSerializer

        data = {
            "title": "Extra Keys Test",
            "url": [
                {
                    "link": "https://example.com/doc",
                    "role": "RAW",
                    "malicious": "payload",
                }
            ],
        }
        serializer = DocumentSourceCreateSerializer(data=data)
        assert serializer.is_valid(), f"Errors: {serializer.errors}"
        assert serializer.validated_data["url"] == [
            {"link": "https://example.com/doc", "role": "RAW"}
        ]

    def test_none_role_normalized_to_raw_on_save(self):
        """A None role passed to the model is normalized to RAW before save."""
        from cases.serializers import DocumentSourceSerializer

        source = DocumentSource.objects.create(
            title="None Role Test",
            url=[{"link": "https://example.com/doc", "role": None}],
        )
        # Stored value has a concrete RAW role, not None.
        assert source.url == [{"link": "https://example.com/doc", "role": "RAW"}]
        serializer = DocumentSourceSerializer(source)
        assert serializer.data["url"] == ["https://example.com/doc"]
        assert serializer.data["urls"] == [
            {"link": "https://example.com/doc", "role": "RAW"}
        ]

    def test_model_rejects_invalid_role(self):
        """An unknown role is rejected by full_clean on save."""
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            DocumentSource.objects.create(
                title="Bad Role",
                url=[{"link": "https://example.com/doc", "role": "BOGUS"}],
            )

    def test_model_preserves_explicit_non_raw_role(self):
        """A valid non-RAW role survives save unchanged."""
        source = DocumentSource.objects.create(
            title="Markdown Role",
            url=[{"link": "https://example.com/md", "role": "MARKDOWN"}],
        )
        assert source.url == [{"link": "https://example.com/md", "role": "MARKDOWN"}]

    def test_get_url_dedupes_same_link_across_roles(self):
        """Deprecated url field collapses one link shared by two roles."""
        from cases.serializers import DocumentSourceSerializer

        source = DocumentSource.objects.create(
            title="Dup Link Test",
            url=[
                {"link": "https://example.com/doc", "role": "RAW"},
                {"link": "https://example.com/doc", "role": "MARKDOWN"},
            ],
        )
        serializer = DocumentSourceSerializer(source)
        # url (strings) is deduped; urls (dicts) keeps both role variants.
        assert serializer.data["url"] == ["https://example.com/doc"]
        assert serializer.data["urls"] == [
            {"link": "https://example.com/doc", "role": "RAW"},
            {"link": "https://example.com/doc", "role": "MARKDOWN"},
        ]


class TestDictFormatMigration(TransactionTestCase):
    """Test the data migration that converts str entries to dict format."""

    @staticmethod
    def get_historical_model(connection, migration_tuple, app_label, model_name):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        project_state = executor.loader.project_state(migration_tuple)
        return project_state.apps.get_model(app_label, model_name)

    def setUp(self):
        """Create test data with mixed str/dict URL entries before the migration."""
        from django.utils import timezone

        call_command("migrate", "cases", "0024_alter_chat_user_identity", verbosity=0)

        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0024_alter_chat_user_identity"),
            "cases",
            "DocumentSource",
        )

        now = timezone.now()
        DocumentSource.objects.bulk_create(
            [
                DocumentSource(
                    source_id="source:dict:migrate:001",
                    title="Plain strings",
                    description="All str entries",
                    url=["https://example.com/1", "https://example.com/2"],
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentSource(
                    source_id="source:dict:migrate:002",
                    title="Already dicts",
                    description="Already in dict format",
                    url=[
                        {"link": "https://example.com/3", "role": "RAW"},
                    ],
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentSource(
                    source_id="source:dict:migrate:003",
                    title="Mixed entries",
                    description="Mix of str and dict",
                    url=[
                        "https://example.com/4",
                        {"link": "https://example.com/5", "role": "MARKDOWN"},
                    ],
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentSource(
                    source_id="source:dict:migrate:004",
                    title="Empty list",
                    description="Empty URL list",
                    url=[],
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )

    def test_forward_converts_strs_to_dicts(self):
        """Migration should convert str entries to {link, role: None} dicts."""
        call_command("migrate", "cases", "0025_convert_url_list_to_dict", verbosity=0)

        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0025_convert_url_list_to_dict"),
            "cases",
            "DocumentSource",
        )

        s1 = DocumentSource.objects.get(source_id="source:dict:migrate:001")
        for entry in s1.url:
            assert isinstance(entry, dict), f"Expected dict, got {entry!r}"
            assert "link" in entry
            assert "role" in entry
        assert s1.url[0] == {"link": "https://example.com/1", "role": None}
        assert s1.url[1] == {"link": "https://example.com/2", "role": None}

    def test_forward_leaves_existing_dicts(self):
        """Migration should not modify existing dict entries."""
        call_command("migrate", "cases", "0025_convert_url_list_to_dict", verbosity=0)

        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0025_convert_url_list_to_dict"),
            "cases",
            "DocumentSource",
        )

        s2 = DocumentSource.objects.get(source_id="source:dict:migrate:002")
        assert s2.url == [{"link": "https://example.com/3", "role": "RAW"}]

    def test_forward_converts_mixed_list(self):
        """Migration should handle mixed str/dict lists."""
        call_command("migrate", "cases", "0025_convert_url_list_to_dict", verbosity=0)

        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0025_convert_url_list_to_dict"),
            "cases",
            "DocumentSource",
        )

        s3 = DocumentSource.objects.get(source_id="source:dict:migrate:003")
        assert s3.url[0] == {"link": "https://example.com/4", "role": None}
        assert s3.url[1] == {"link": "https://example.com/5", "role": "MARKDOWN"}

    def test_forward_handles_empty_list(self):
        """Migration should not break empty lists."""
        call_command("migrate", "cases", "0025_convert_url_list_to_dict", verbosity=0)

        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0025_convert_url_list_to_dict"),
            "cases",
            "DocumentSource",
        )

        s4 = DocumentSource.objects.get(source_id="source:dict:migrate:004")
        assert s4.url == []

    def test_reverse_converts_dicts_to_strings(self):
        """Reverse migration should convert dict entries back to strings."""
        call_command("migrate", "cases", "0025_convert_url_list_to_dict", verbosity=0)
        call_command("migrate", "cases", "0024_alter_chat_user_identity", verbosity=0)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT url FROM cases_documentsource WHERE source_id = %s",
                ["source:dict:migrate:001"],
            )
            url_value = cursor.fetchone()[0]

        import json as _json

        parsed = _json.loads(url_value) if isinstance(url_value, str) else url_value
        assert parsed == ["https://example.com/1", "https://example.com/2"]

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    @classmethod
    def tearDownClass(cls):
        call_command("migrate", verbosity=0)
        super().tearDownClass()


class TestRoleBackfillMigration(TransactionTestCase):
    """Test migration 0027, which backfills None/missing roles to RAW."""

    @staticmethod
    def get_historical_model(connection, migration_tuple, app_label, model_name):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        project_state = executor.loader.project_state(migration_tuple)
        return project_state.apps.get_model(app_label, model_name)

    def setUp(self):
        """Create data with None-role entries at the state just before 0032."""
        from django.utils import timezone

        call_command("migrate", "cases", "0031_make_source_type_required", verbosity=0)

        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0031_make_source_type_required"),
            "cases",
            "DocumentSource",
        )

        now = timezone.now()
        DocumentSource.objects.bulk_create(
            [
                DocumentSource(
                    source_id="source:role:backfill:001",
                    title="None roles",
                    url=[
                        {"link": "https://example.com/a", "role": None},
                        {"link": "https://example.com/b", "role": None},
                    ],
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentSource(
                    source_id="source:role:backfill:002",
                    title="Mixed roles",
                    url=[
                        {"link": "https://example.com/c", "role": "MARKDOWN"},
                        {"link": "https://example.com/d", "role": None},
                    ],
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )

    def test_forward_backfills_none_roles_to_raw(self):
        """None roles become RAW; explicit non-RAW roles are preserved."""
        call_command(
            "migrate", "cases", "0032_backfill_source_link_role_raw", verbosity=0
        )

        DocumentSource = self.get_historical_model(
            connection,
            ("cases", "0032_backfill_source_link_role_raw"),
            "cases",
            "DocumentSource",
        )

        s1 = DocumentSource.objects.get(source_id="source:role:backfill:001")
        assert s1.url == [
            {"link": "https://example.com/a", "role": "RAW"},
            {"link": "https://example.com/b", "role": "RAW"},
        ]

        s2 = DocumentSource.objects.get(source_id="source:role:backfill:002")
        assert s2.url == [
            {"link": "https://example.com/c", "role": "MARKDOWN"},
            {"link": "https://example.com/d", "role": "RAW"},
        ]

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    @classmethod
    def tearDownClass(cls):
        call_command("migrate", verbosity=0)
        super().tearDownClass()
