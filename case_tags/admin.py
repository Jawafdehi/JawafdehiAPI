"""Direct vocabulary editing, separate from the review queue.

The queue (``/admin/tags`` in the SPA, T26) is where automation-proposed changes get a
tick. This is the escape hatch for the things a queue is the wrong shape for: correcting
a label typo, deprecating a term, wiring up a merge. Registered read-mostly for
``TagProposal`` because deciding a proposal must go through the API — the view holds the
row lock and the apply step, and a status flipped here would change nothing in the
vocabulary while looking like it had.
"""

from django.contrib import admin

from case_tags.models import Tag, TagAlias, TagAxis, TagProposal


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


@admin.register(TagProposal)
class TagProposalAdmin(admin.ModelAdmin):
    list_display = ("kind", "dedup_key", "status", "confidence", "detected_by",
                    "reviewer", "reviewed_at")
    list_filter = ("kind", "status")
    search_fields = ("dedup_key", "detected_by")
    # Deciding happens through the API, which locks the row and runs the apply step.
    readonly_fields = ("status", "reviewer", "reviewed_at")
