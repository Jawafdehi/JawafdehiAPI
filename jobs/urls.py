"""Job-queue routes, mounted by config/urls.py at /api/jobs/."""

from django.urls import path

from . import views

app_name = "jobs"

urlpatterns = [
    path("", views.jobs_collection, name="jobs"),
    path("claim/", views.claim, name="job-claim"),
    path("<int:pk>/stage/", views.stage, name="job-stage"),
    path("<int:pk>/result/", views.result, name="job-result"),
]
