"""One-time seed of the legacy static Updates into the Wagtail CMS.

Idempotent: skips any article whose slug already exists. Inline images and PDFs
keep their existing ``/assets/...`` paths (served by the frontend), embedded as
rich text rather than Wagtail Image/Document objects.

Usage: python manage.py seed_updates
"""

import datetime
import re
from urllib.parse import quote

import markdown as md
from django.core.management.base import BaseCommand

from content.models import ArticleCategory, ArticleIndexPage, ArticlePage

# Transcribed from the frontend's former src/data/updates.ts (one-time import).
UPDATES = [
    {
        "id": "2026-04-14-caseworker-intern-flyer",
        "title": "Caseworker Intern Opening",
        "content": """
We're looking for a detail-oriented law student to join Jawafdehi as a Caseworker Intern.

This remote, part-time internship focuses on researching corruption cases, structuring evidence, and helping build Nepal's institutional memory against corruption.

See the full role description and share it with anyone who may be a strong fit.
""",
        "pdfs": [
            {
                "name": "Caseworker Intern Flyer (PDF)",
                "path": "/assets/updates/2026-04-14-job-postings/caseworker-intern-flyer.pdf",
            }
        ],
    },
    {
        "id": "2026-01-04-second-national-strategy-feedback",
        "title": "Jawafdehi Submits Feedback on Nepal's Second National Anti-Corruption Strategy",
        "content": """
We received an invitation from the Office of the Prime Minister and Council of Ministers to participate in a stakeholder consultation meeting for finalizing Nepal's Second National Strategy and Action Plan Against Corruption, 2082 (भ्रष्टाचार विरुद्धको दोस्रो राष्ट्रिय रणनीति तथा कार्ययोजना, २०८२).

We reviewed the draft strategy document and submitted our feedback at the meeting held in Singha Durbar, coordinated by the Chief Secretary.

In summary, we called for:

- Independent expert groups (legal experts, academics, journalists, civil society) in coordination mechanisms
- Clear rationale and measurable outcomes for proposed legal amendments
- Incorporation of the GenZ movement's demands, including a high-level asset investigation commission
- Digital infrastructure upgrades — high-bandwidth government websites, document digitization, and online court records
- Government funding for civic accountability initiatives to reduce foreign dependency

![Jawafdehi at the consultation meeting](/assets/updates/2026-01-04-meeting/2026-01-04-second-national-strategy-feedback.jpeg)
*Rohan representing Jawafdehi at the stakeholder consultation in Singha Durbar*

Moving forward, we remain committed to supporting the implementation of this strategy and will continue engaging with the government on anti-corruption efforts.
""",
        "thumbnail": "/assets/updates/2026-01-04-meeting/2026-01-04-second-national-strategy-feedback.jpeg",
        "pdfs": [
            {
                "name": "Our Feedback Letter",
                "path": "/assets/updates/2026-01-04-meeting/our-feedback.pdf",
            },
            {
                "name": "Government Strategy Document (भ्रष्टाचार विरुद्धको दोस्रो राष्ट्रिय रणनीति तथा कार्ययोजना, २०८२)",
                "path": "/assets/updates/2026-01-04-meeting/भ्रष्टाचार विरुद्धको दोस्रो राष्ट्रिय रणनीति तथा कार्ययोजना, २०८२.pdf",
            },
        ],
    },
]


def _excerpt(content: str, length: int = 200) -> str:
    text = re.sub(r"!\[.*?\]\(.*?\)", "", content)  # drop images
    text = re.sub(r"[#*_>`\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= length:
        return text
    return text[:length].rstrip() + "…"


def _documents_html(pdfs) -> str:
    if not pdfs:
        return ""
    items = "".join(
        f'<li><a href="{quote(pdf["path"], safe="/")}">{pdf["name"]}</a></li>'
        for pdf in pdfs
    )
    return f"<h2>Documents and resources</h2><ul>{items}</ul>"


class Command(BaseCommand):
    help = "Seed legacy static Updates into the Wagtail CMS (idempotent)."

    def handle(self, *args, **options):
        index = ArticleIndexPage.objects.first()
        if index is None:
            self.stderr.write(
                "No ArticleIndexPage found. Run migrations first (content.0002)."
            )
            return

        created = 0
        for update in UPDATES:
            slug = update["id"]
            if ArticlePage.objects.filter(slug=slug).exists():
                self.stdout.write(f"skip (exists): {slug}")
                continue

            body_html = md.markdown(update["content"], extensions=["extra"])
            body_html += _documents_html(update.get("pdfs"))

            page = ArticlePage(
                title=update["title"],
                slug=slug,
                category=ArticleCategory.UPDATE,
                date=datetime.date.fromisoformat(slug[:10]),
                excerpt=_excerpt(update["content"]),
                body=[("paragraph", body_html)],
            )
            index.add_child(instance=page)
            page.save_revision().publish()
            created += 1
            self.stdout.write(f"created: {slug}")

        self.stdout.write(self.style.SUCCESS(f"Done. {created} article(s) created."))
