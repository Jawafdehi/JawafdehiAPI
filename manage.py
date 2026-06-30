#!/usr/bin/env python
"""Management entrypoint for the consolidated Jawafdehi platform (monolith).

ONE manage.py for the whole project. All three former services (NES / NGM /
Jawafdehi) load as Django apps under the single ``monolith.config.settings``
module. Per-database migrations are driven via ``--database=<alias>`` together
with the DB router's ``allow_migrate`` (which restricts each app's tables to its
own DB):

    python manage.py migrate --database=default   # Jawafdehi + django.contrib.*
    python manage.py migrate --database=nes        # nes_service.entities
    python manage.py migrate --database=ngm        # ngm_service.{courts,materials}
"""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "monolith.config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
