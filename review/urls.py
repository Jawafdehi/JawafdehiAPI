"""URL configuration for the Casework Review System (review app).

Mounted by config/urls.py under /api/casework/. Authentication is the shared
jawafdehi-api JWT (clients get a token from /api/caseworker/auth/token/), and
every endpoint requires at least the Contributor role (see permissions.py).
"""

from django.urls import path

from . import views

urlpatterns = [
    path("auth/me/", views.me_view),
    path("reviews/", views.ReviewListView.as_view()),
    path("reviews/submit/", views.submit_review),
    path("reviews/regrade-all/", views.regrade_all),
    path("reviews/<int:pk>/", views.ReviewDetailView.as_view()),
    path("rules/", views.rules_list),
    path("rules/<int:pk>/", views.rule_detail),
    path("config/", views.config_view),
]
