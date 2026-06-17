"""URL configuration for the Casework Review System (review app).

Mounted by config/urls.py under /api/casework/. Authentication is a Zitadel
OIDC access token (validated by ZitadelJWTAuthentication), and every endpoint
requires at least the Contributor role (see permissions.py).
"""

from django.urls import path

from . import views

urlpatterns = [
    path("auth/me/", views.me_view),
    path("reviews/", views.ReviewListView.as_view()),
    path("reviews/submit/", views.submit_review),
    path("reviews/regrade-all/", views.regrade_all),
    # Job API for the DB-free poller: claim -> (stage) -> result.
    path("jobs/claim/", views.claim_job),
    path("jobs/<int:pk>/stage/", views.job_stage),
    path("jobs/<int:pk>/result/", views.submit_job_result),
    path("sources/<str:source_id>/markdown/", views.attach_source_markdown),
    path("reviews/<int:pk>/", views.ReviewDetailView.as_view()),
    path("rules/", views.rules_list),
    path("rules/<int:pk>/", views.rule_detail),
    path("config/", views.config_view),
]
