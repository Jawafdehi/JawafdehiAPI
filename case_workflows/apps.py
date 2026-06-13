from django.apps import AppConfig


class CaseWorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "case_workflows"
    verbose_name = "Case Workflows"

    def ready(self):
        # Auto-discover workflow templates on startup
        from case_workflows.registry import autodiscover

        autodiscover()

        # NOTE: CaseWorkflowRun is intentionally NOT registered with auditlog.
        # Its run.save() is called per workflow step and per retry attempt, each
        # serializing the full (growing) case_data JSON; a per-save LogEntry
        # would amplify writes on the hot execution path and bloat the audit
        # table. Lifecycle state lives in the model's own fields
        # (has_failed, error_message, completed_at, ...).
