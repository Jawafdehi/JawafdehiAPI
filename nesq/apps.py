from django.apps import AppConfig


class NesqConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "nesq"
    verbose_name = "NES Queue System"

    # NOTE: NESQueueItem is intentionally NOT registered with auditlog.
    # The batch processor calls item.save() once per queued item in a loop
    # (serializing the payload/result JSON each time) and runs outside any
    # request, so per-save LogEntry rows would amplify writes and carry no
    # actor anyway. Who-did-what is already captured on the model's own fields:
    # the submission endpoint logs action+user and sets submitted_by, and the
    # admin approve/reject actions stamp reviewed_by + reviewed_at on each row.
