"""Django settings for running casework SCRIPTS without a database.

The casework sourcing scripts (see ``casework/``) use the Django modules (``llm``,
``sourcing``, serializers) as a library but talk to the casework API over HTTP and
never touch the ORM. This settings module makes ``django.setup()`` succeed with no
``DATABASE_URL``: it points DATABASES at a throwaway in-memory sqlite that is never
connected (Django opens the DB lazily, only on the first query — which never happens
in these scripts).

Run scripts with ``DJANGO_SETTINGS_MODULE=config.settings_scripts`` plus the real
LLM/API env vars (REVIEW_LLM_PROVIDER_*, CASEWORK_API_BASE, tokens, …). No DB infra
or DATABASE_URL required.
"""

import os

# Safe defaults so importing config.settings doesn't require web env. Only set
# when absent, so a real value (if supplied) still wins.
os.environ.setdefault("SECRET_KEY", "casework-scripts-not-for-serving")
os.environ.setdefault("ALLOWED_HOSTS", "localhost")

from config.settings import *  # noqa: F401,F403,E402

# Force a throwaway in-memory DB and disable routing; scripts never query the ORM.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
DATABASE_ROUTERS = []
