from django.apps import AppConfig


class DiscoveryConfig(AppConfig):
    """The public crawl/harvest discovery surfaces (Sitemaps + ResourceSync).

    Owns no models of its own — it ENUMERATES the public corpus by querying the
    three service apps' models in-process (router-correct: ``entities`` → ``nes``,
    ``courts``/``materials`` → ``ngm``, ``cases`` → ``default``). So it routes to
    ``default`` by default, but never reads/writes a ``discovery`` table.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "discovery"
    label = "discovery"
