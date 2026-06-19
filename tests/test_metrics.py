"""Tests for the Prometheus /metrics exposition (django-prometheus wiring)."""

from django.test import TestCase
from django.urls import reverse


class MetricsEndpointTest(TestCase):
    def test_metrics_endpoint_exposes_per_view_red_metrics(self):
        # Generate one observation so the per-view series exist.
        self.client.get(reverse("index"))

        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        # Per-endpoint RED families are registered and exported.
        self.assertIn("django_http_requests_latency_seconds_by_view_method", body)
        self.assertIn("django_http_responses_total_by_status_view", body)
        self.assertIn("django_http_requests_total_by_view_transport_method", body)

        # The view label is present, proving per-endpoint attribution works.
        self.assertIn('view="index"', body)
