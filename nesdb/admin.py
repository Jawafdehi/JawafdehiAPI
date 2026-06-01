from django.contrib import admin

from .models import NesEntity, NesEntityName, NesRelationship, NesSyncState


@admin.register(NesSyncState)
class NesSyncStateAdmin(admin.ModelAdmin):
    list_display = ("last_commit_hash", "last_sync_at")


class NesEntityNameInline(admin.TabularInline):
    model = NesEntityName
    extra = 0


@admin.register(NesEntity)
class NesEntityAdmin(admin.ModelAdmin):
    list_display = ("entity_id", "entity_prefix", "slug", "created_at")
    list_filter = ("entity_prefix",)
    search_fields = ("entity_id", "slug", "entity_prefix")
    inlines = [NesEntityNameInline]


@admin.register(NesRelationship)
class NesRelationshipAdmin(admin.ModelAdmin):
    list_display = ("relationship_id", "type", "source_entity_id", "target_entity_id")
    list_filter = ("type",)
    search_fields = ("relationship_id", "source_entity_id", "target_entity_id")
