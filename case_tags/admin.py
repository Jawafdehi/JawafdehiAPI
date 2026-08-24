"""Direct vocabulary editing.

The tagger writes the vocabulary as it goes, so this is not the primary path — it is
where a human corrects what the tagger got wrong: fixing a label, deprecating a term
nobody wants, wiring up a merge so a bad slug keeps resolving. Those are exactly the
operations a generated pipeline is the wrong shape for.
"""

from django.contrib import admin

from case_tags.models import Tag, TagAlias, TagAxis


@admin.register(TagAxis)
class TagAxisAdmin(admin.ModelAdmin):
    list_display = ("id", "label_ne", "label_en", "min_per_case", "max_per_case",
                    "highlighted", "members", "sort_order")
    list_filter = ("highlighted", "members")
    ordering = ("sort_order", "id")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("id", "axis", "label_ne", "label_en", "status", "merged_into")
    list_filter = ("axis", "status")
    search_fields = ("id", "label_ne", "label_en")
    autocomplete_fields = ()
    ordering = ("axis", "id")


@admin.register(TagAlias)
class TagAliasAdmin(admin.ModelAdmin):
    list_display = ("value", "tag", "source", "approved_by", "approved_at")
    list_filter = ("source",)
    search_fields = ("value", "tag__id")
    ordering = ("value",)
