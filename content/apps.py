from django.apps import AppConfig
from django.db.models.signals import post_migrate


class ContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "content"
    verbose_name = "Content (CMS)"

    def ready(self):
        from .permissions import sync_cms_group_permissions

        post_migrate.connect(sync_cms_group_permissions, sender=self)
