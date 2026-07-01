"""
Tests for the landing page (index view).
"""

from django.test import TestCase
from django.urls import reverse


class LandingPageTest(TestCase):
    """Test the landing page content and structure."""

    def test_landing_page_renders(self):
        """Test that the landing page renders successfully with correct content."""
        response = self.client.get(reverse("index"))

        # Test rendering
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "index.html")

        # Test page title
        self.assertContains(response, "Jawafdehi Contributor Portal")
        self.assertContains(response, "<title>Jawafdehi Contributor Portal</title>")

        # Test main website link
        self.assertContains(response, "https://jawafdehi.org")
        self.assertContains(response, "The actual Jawafdehi website is located at")
