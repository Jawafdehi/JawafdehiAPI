from django.db import models


class SourceURLRole(models.TextChoices):
    RAW = "RAW", "Raw URL"
    MARKDOWN = "MARKDOWN", "Markdown URL"
    PERMALINK = "PERMALINK", "Permalink URL"
