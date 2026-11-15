"""DDB-API: Institutionen mit GND (Solr) und ISIL (/items/{id}).

Ergebnis der Erkundung (siehe README):
- Solr-Collection 'organization', type:ddb-institution → id, Name, Sparte, GND in variant_id
- /items/{id} → provider-info.provider-isil (ZDB-URI) bzw. edm.RDF.Organization.sameAs
Ein API-Schlüssel ist für diese Endpunkte nicht nötig.
"""

from __future__ import annotations

import logging

from .. import config
from ..http import CachedClient
from ..models import SourceRecord
from ..normalize import normalize_gnd, normalize_isil

log = logging.getLogger(__name__)
B = config.DDB_API

# DDB-Sparten
SECTORS = {
    "sec_01": "Archiv",
    "sec_02": "Bibliothek",
    "sec_03": "Denkmalpflege",
    "sec_04": "Forschung",
    "sec_05": "Mediathek",
    "sec_06": "Museum",
    "sec_07": "Sonstige",
}
# Zuordnung CLI-Typ → DDB-Sparte
TYPE_TO_SECTOR = {"archive": "sec_01", "library": "sec_02", "museum": "sec_06"}


def _solr(client: CachedClient, **params) -> dict:
    return client.get_json(f"{B}/search/index/organization/select", params={"wt": "json", **params})


def fetch_institutions(client: CachedClient, page_size: int = 1000) -> list[dict]:
    docs: list[dict] = []
    start = 0
    while True:
        d = _solr(
            client,
            q="type:ddb-institution",
            rows=page_size,
            start=start,
            sort="id asc",
            fl="id,preferredName,variant_id,sector_fct,parent_id,state_de_fct,city_de_fct",
        )
        batch = d["response"]["docs"]
        docs.extend(batch)
        start += page_size
        if start >= d["response"]["numFound"] or not batch:
            break
    log.info("DDB: %d Institutionen geladen", len(docs))
    return docs


def fetch_isils(client: CachedClient, ddb_id: str) -> list[str]:
    """ISIL aus /items/{id} (provider-isil und Organization.sameAs)."""
    item = client.get_json(f"{B}/items/{ddb_id}", allow_404=True) or {}
    raw = [(item.get("provider-info") or {}).get("provider-isil") or ""]
    org = ((item.get("edm") or {}).get("RDF") or {}).get("Organization") or {}
    raw += [s.get("resource", "") for s in org.get("sameAs") or [] if isinstance(s, dict)]
    return sorted({v for v in (normalize_isil(r) for r in raw if r) if v})


def fetch(
    client: CachedClient, sectors: set[str] | None = None, limit: int | None = None
) -> list[SourceRecord]:
    """DDB-Institutionen als SourceRecords (optional nach Sparte gefiltert, begrenzt)."""
    docs = fetch_institutions(client)
    if sectors:
        docs = [d for d in docs if d.get("sector_fct") in sectors]
    if limit:
        docs = docs[:limit]
    records = []
    for i, d in enumerate(docs, 1):
        if i % 250 == 0:
            log.info("DDB: ISIL für %d/%d Institutionen abgerufen", i, len(docs))
        gnd = sorted({v for v in (normalize_gnd(x) for x in d.get("variant_id") or []) if v})
        records.append(
            SourceRecord(
                source="ddb",
                source_id=d["id"],
                label=d.get("preferredName", ""),
                url=f"https://www.deutsche-digitale-bibliothek.de/organization/{d['id']}",
                country="Germany",
                sector=d.get("sector_fct"),
                ids={"P791": fetch_isils(client, d["id"]), "P227": gnd},
            )
        )
    return records
