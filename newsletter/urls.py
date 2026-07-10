"""Newsletter routes, mounted by config/urls.py at /api/.

Paths match the frontend contract in ``jawafdehi-frontend``
``src/services/jds-api.ts`` exactly:
``/api/newsletter/subscriptions/`` and ``/api/newsletter/unsubscribe/<token>/``.
"""

from django.urls import path

from . import views

app_name = "newsletter"

urlpatterns = [
    path(
        "newsletter/subscriptions/",
        views.NewsletterSubscriptionView.as_view(),
        name="subscribe",
    ),
    # Token is opaque (URL-safe base64 from django.core.signing); match greedily
    # up to the trailing slash so '.'/'-'/'_'/':' in the signature aren't split.
    path(
        "newsletter/unsubscribe/<str:token>/",
        views.NewsletterUnsubscribeView.as_view(),
        name="unsubscribe",
    ),
]
