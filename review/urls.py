"""URL configuration for the Casework Review System (review app).

Mounted by config/urls.py under /api/casework/. Authentication is the shared
jawafdehi-api JWT (clients get a token from /api/caseworker/auth/token/), and
every endpoint requires at least the Caseworker role (see permissions.py).
"""

from django.urls import path

from . import views

urlpatterns = [
    path("auth/me/", views.me_view),
    path("reviews/", views.ReviewListView.as_view()),
    path("reviews/submit/", views.submit_review),
    path("reviews/regrade-all/", views.regrade_all),
    # NOTE: the review-local job API (jobs/claim, jobs/<id>/stage, jobs/<id>/result)
    # was RETIRED. Reviews are now enqueued on the central queue and the poller
    # claims them at /api/jobs/* as kind=case_review. See jobs/ + docs/jobs-queue-design.md.
    path("sources/<str:source_id>/markdown/", views.attach_source_markdown),
    path("reviews/<int:pk>/", views.ReviewDetailView.as_view()),
    path("rules/", views.rules_list),
    path("rules/<int:pk>/", views.rule_detail),
    path("config/", views.config_view),
]
