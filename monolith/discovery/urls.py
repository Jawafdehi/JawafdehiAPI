"""Public discovery URLs — mounted at the project ROOT (not under /api/).

These are public-corpus crawl/harvest surfaces addressed at canonical top-level
paths so crawlers and harvesters find them where the standards expect:

    /sitemap.xml                      -> sitemap INDEX (per-type child sitemaps)
    /sitemap-<section>.xml            -> a child sitemap for one record type
    /robots.txt                       -> points at the sitemap + resourcesync
    /.well-known/resourcesync         -> ResourceSync Source Description
    /resourcesync/capabilitylist.xml  -> ResourceSync Capability List
    /resourcesync/resourcelist.xml    -> ResourceSync Resource List

Sitemaps use Django's ``django.contrib.sitemaps`` ``index`` + ``sitemap`` views.
The index emits one ``<sitemap>`` per section (record type); for a large type the
section paginates (``?p=N``) into 50k-URL child sitemaps. The index links the
children via the named ``discovery-sitemap-section`` route.
"""

from __future__ import annotations

from django.urls import path

from . import views

# The index view links each child sitemap by reversing this URL name, passing the
# ``section`` (record-type token). The index + section views are thin wrappers
# over ``django.contrib.sitemaps`` (see views.py) that pin the child-link host to
# the canonical IRI authority and add a server-side cache.
urlpatterns = [
    path(
        "sitemap.xml",
        views.sitemap_index,
        name="discovery-sitemap-index",
    ),
    path(
        "sitemap-<section>.xml",
        views.sitemap_section,
        name="discovery-sitemap-section",
    ),
    path("robots.txt", views.robots_txt, name="discovery-robots"),
    path(
        ".well-known/resourcesync",
        views.resourcesync_source_description,
        name="discovery-resourcesync",
    ),
    path(
        "resourcesync/capabilitylist.xml",
        views.resourcesync_capability_list,
        name="discovery-resourcesync-capabilitylist",
    ),
    path(
        "resourcesync/resourcelist.xml",
        views.resourcesync_resource_list,
        name="discovery-resourcesync-resourcelist",
    ),
]
