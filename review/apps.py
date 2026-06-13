from django.apps import AppConfig


class ReviewConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "review"

    def ready(self):
        # Audit the casework review lifecycle (submit/claim/stage/result) and
        # the global review configuration (threshold/sample changes).
        from auditlog.registry import auditlog

        from review.models import CaseReview
        from review.models import ReviewConfig as ReviewConfigModel

        auditlog.register(CaseReview)
        auditlog.register(ReviewConfigModel)
