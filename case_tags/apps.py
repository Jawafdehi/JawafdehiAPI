from django.apps import AppConfig


class CaseTagsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "case_tags"
    verbose_name = "Case tags"

    def ready(self) -> None:
        # Import for the side effect of registering the tagger prompt. Mirrors how
        # case_proposals registers its own — the registry is populated at app-ready so
        # `llm.prompts.known()` is complete for any caller, including the CLI.
        from case_tags import prompts  # noqa: F401, PLC0415

        # Same, for the job kind's server-side hooks. Registering at ready() rather than
        # at import time is what keeps `jobs.registry` complete before the first claim.
        from case_tags import job_kind  # noqa: F401, PLC0415
