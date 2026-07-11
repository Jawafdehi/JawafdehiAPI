"""Newsletter routes, mounted by config/urls.py at /api/.

Only subscribe is served: unsubscribe is handled by SendPulse's own hosted link
in campaign emails, so the backend owns no unsubscribe route. Path matches the
frontend contract in ``jawafdehi-frontend`` ``src/services/jds-api.ts``.
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
]
