"""Court-portal crawlers, ported from the retired NGM Scrapy service.

Each court is a parser (portal HTML → structured rows) plus a fetch config; the
``scrape_courtcases`` management command drives them date-by-date and writes the
``courts`` app models via the ORM, normalising fields at write time. Kept as pure
parse functions (no I/O) so the porting logic is unit-tested against HTML
fixtures without touching the network or a prod database.
"""
