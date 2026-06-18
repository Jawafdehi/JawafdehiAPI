"""Casework sourcing/enrichment scripts.

DB-free standalone scripts (run as `python casework/<name>.py …`, not via
manage.py) that use the Django modules (llm, sourcing) as a library but talk to
the Jawafdehi API over HTTP and never touch the ORM. See casework/common.py.
"""
