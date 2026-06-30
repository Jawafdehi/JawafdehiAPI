"""WSGI entrypoint for the consolidated platform (monolith)."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "monolith.config.settings")

application = get_wsgi_application()
