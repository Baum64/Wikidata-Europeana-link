"""Europeana Entity API: Organisationen abrufen und sameAs-Links auswerten."""

from __future__ import annotations

import logging
import re

from .. import config
from ..http import CachedClient
from ..models import SourceRecord
from ..normalize import (
    LEGACY_ORG_ID_MIN_LEN,
    normalize_gnd,
    normalize_isni,
    normalize_p7704,
    normalize_viaf,
    qid_from_uri,
)

log = logging.getLogger(__name__)
E = config.EUROPEANA_ENTITY


def _require_key() -> str:
    if not config.EUROPEANA_API_KEY:
        raise SystemExit("EUROPEANA_API_KEY ist nicht gesetzt (siehe .env.example).")
    return config.EUROPEANA_API_KEY


def fetch_all_raw(client: CachedClient, page_size: int = 100) -> list[dict]:
    """Alle Organisationen über die Suchfunktion (paginiert)."""
    key = _require_key()
    items: list[dict] = []
    page = 1
    while True:
        d = client.get_json(
            f"{E}/search",
            params={"query": "*", "type": "organization", "page": page, "pageSize": page_size,
                    "sort": "id asc", "wskey": key},
        )
        batch = d.get("items") or []
        items.extend(batch)
        if not batch or not d.get("next"):
            break
        page += 1
    log.info("Europeana: %d Organisationen geladen", len(items))
    return items


def _uniq(values) -> list[str]:
    return sorted({v for v in values if v})


def to_record(o: dict) -> SourceRecord:
    num = o["id"].rsplit("/", 1)[-1]
    same = o.get("sameAs") or []
    pl = o.get("prefLabel") or {}
    country = ((o.get("country") or {}).get("prefLabel") or {}).get("en")

    legacy = []
    for s in same:
        if "data.europeana.eu/organization/" in s:
            v = normalize_p7704(s)
            if v and v != f"organisation/{num}" and len(v.rsplit("/", 1)[-1]) >= LEGACY_ORG_ID_MIN_LEN:
                legacy.append(v)

    return SourceRecord(
        source="europeana",
        source_id=num,
        label=pl.get("de") or pl.get("en") or next(iter(pl.values()), ""),
        url=f"https://www.europeana.eu/de/collections/organisation/{num}",
        country=country,
        qids=_uniq(qid_from_uri(s) for s in same if "wikidata.org" in s),
        ids={
            "P7704": [f"organisation/{num}"],
            "P227": _uniq(normalize_gnd(s) for s in same if "d-nb.info/gnd/" in s),
            "P214": _uniq(normalize_viaf(s) for s in same if "viaf.org/viaf/" in s),
            "P213": _uniq(normalize_isni(s) for s in same if re.search(r"isni(\.oclc)?\.org", s)),
        },
        legacy_p7704=_uniq(legacy),
    )


def fetch(client: CachedClient, country_label_en: str | None = None) -> list[SourceRecord]:
    """Alle Organisationen als SourceRecords, optional gefiltert nach Land (englisches Label)."""
    records = [to_record(o) for o in fetch_all_raw(client)]
    if country_label_en:
        records = [r for r in records if r.country == country_label_en]
    return records
