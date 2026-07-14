from django.apps import AppConfig


class ReviewConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "review"

    def ready(self):
        # Audit review-workflow records. ``CaseReview`` is driven by the jobs
        # poller via ``QuerySet.update()`` (jobs/consumers.py), so the audited
        # manager is what captures those transitions; ``ReviewConfig`` is the
        # admin-edited grading config.
        from jawafdehi_shared.db.audited import register_audited

        from review.models import CaseReview
        from review.models import ReviewConfig as ReviewConfigModel

        register_audited(CaseReview)
        register_audited(ReviewConfigModel)
