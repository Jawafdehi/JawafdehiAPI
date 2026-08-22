# SPDX-License-Identifier: Hippocratic-3.0
"""Write the site-wide Open Graph banner to a file.

The author cards are served live from ``/api/authors/<slug>/og-card.jpg``, but the
site banner is a committed static asset in the frontend repo
(``public/assets/social-preview.png``). It has to be: the Worker falls back to it
when this service cannot answer, and a fallback that depends on this service
being up is not a fallback.

So it is generated here — where the dot field, the logo treatment and the
descriptor already live, so the two card types cannot drift apart — and the bytes
are committed over there.

    uv run python manage.py render_og_site_card \\
        ../Jawafdehi/public/assets/social-preview.png

Only needed when the branding changes. Nothing calls it automatically.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from PIL import features

from cases.og_cards import render_site_card


class Command(BaseCommand):
    help = "Render the site-wide Open Graph banner (1200x630 PNG) to a path."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            type=Path,
            help="Where to write the PNG, e.g. ../Jawafdehi/public/assets/social-preview.png",
        )

    def handle(self, *args, **options):
        # The banner's wordmark is Devanagari, so an environment without shaping
        # would write a card with जवाफदेही come apart — and unlike the endpoint,
        # nothing downstream would notice, because the output is committed.
        if not features.check("raqm"):
            raise CommandError(
                "Pillow has no Raqm here, so जवाफदेही would render unshaped. "
                "Install libfribidi (Debian/Ubuntu: apt install libfribidi0) "
                "and try again."
            )

        path: Path = options["path"]
        if not path.parent.is_dir():
            raise CommandError(f"No such directory: {path.parent}")

        payload = render_site_card()
        path.write_bytes(payload)
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {path} ({len(payload):,} bytes, 1200x630)")
        )
