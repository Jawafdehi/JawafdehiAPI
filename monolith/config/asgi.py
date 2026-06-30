"""ASGI entrypoint for the consolidated platform (monolith)."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "monolith.config.settings")

application = get_asgi_application()
