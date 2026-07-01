"""Seed the NGM rate-limit tier auth Groups.

Relocated from the retired ``ngm`` proxy app's
``0001_create_rate_tier_groups`` migration when that app was deleted. These
Groups are still load-bearing: the in-process NGM plane gates on them
(``courts.permissions.NGM_ROLE_GROUPS``) and the shared OIDC
authenticator only ATTACHES existing Groups during role sync — it never creates
them (``shared/jawafdehi_shared/auth/oidc.py``). So the rows must be seeded by a
migration. They live in the ``default`` DB alongside ``auth`` (same DB the proxy
migration targeted), so the relocation is a straight move with no DB change.
"""

from django.db import migrations

_TIER_GROUPS = ("NGM_SilverTier", "NGM_GoldTier", "NGM_PlatinumTier")


def create_ngm_rate_tier_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for group_name in _TIER_GROUPS:
        Group.objects.get_or_create(name=group_name)


def remove_ngm_rate_tier_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=_TIER_GROUPS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0038_remove_case_case_id"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(
            create_ngm_rate_tier_groups,
            reverse_code=remove_ngm_rate_tier_groups,
        ),
    ]
