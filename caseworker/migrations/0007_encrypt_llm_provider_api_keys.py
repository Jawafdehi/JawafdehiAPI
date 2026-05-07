from django.db import migrations, models


def encrypt_existing_provider_keys(apps, schema_editor):
    from caseworker.crypto import encrypt_secret, is_encrypted_secret

    LLMProvider = apps.get_model("caseworker", "LLMProvider")
    for provider in LLMProvider.objects.exclude(api_key__isnull=True).exclude(
        api_key=""
    ):
        if is_encrypted_secret(provider.api_key):
            continue
        provider.api_key = encrypt_secret(provider.api_key)
        provider.save(update_fields=["api_key"])


class Migration(migrations.Migration):

    dependencies = [
        ("caseworker", "0006_alter_llmprovider_options_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="llmprovider",
            name="api_key",
            field=models.CharField(blank=True, max_length=2048, null=True),
        ),
        migrations.RunPython(encrypt_existing_provider_keys, migrations.RunPython.noop),
    ]
