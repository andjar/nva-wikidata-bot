"""NVA → Wikidata bot (NVAImportBot).

Adds missing, referenced statements to Wikidata items using metadata from
Nasjonalt vitenarkiv (NVA, https://api.nva.unit.no), and can optionally
create items for NVA publications that are not on Wikidata yet. It never
overwrites values; the only removal is converting an author name string
(P2093) into an author item (P50) for the same person.

Copyright (C) 2026 Ajarmund. Licensed under the GNU GPL v3 or later.
"""

__version__ = "0.2.0"
