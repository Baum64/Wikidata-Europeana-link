"""Europeana-Orte (Entity API, Typ „place“) für den Abgleich von Städten mit Wikidata.

Statt alle ~170.000 Orte zu laden, wird jede Wikidata-Stadt einzeln über
`/entity/resolve?uri=<Wikidata-URI>` aufgelöst. Europeana antwortet mit
  301 + Location           → genau ein Ort,
  300 + Liste von Orten    → mehrere Orte mit demselben sameAs (Dubletten in Europeana),
  404                      → kein Ort.
"""

from __future__ import annotations

import logging

from .. import config
from ..http import CachedClient
from ..models import SourceRecord
from ..normalize import normalize_p7704, qid_from_uri
from .europeana import _require_key

log = logging.getLogger(__name__)
E = config.EUROPEANA_ENTITY


def _place_num(uri: str) -> str | None:
    v = normalize_p7704(uri or "")
    return v.rsplit("/", 1)[-1] if v and v.startswith("place/") else None


def resolve_qid(client: CachedClient, qid: str) -> list[str]:
    """Nummern der Europeana-Orte, deren sameAs auf die QID zeigt (leer = keiner)."""
    r = client.resolve(f"{E}/resolve", params={"uri": f"http://www.wikidata.org/entity/{qid}", "wskey": _require_key()})
    if r["status"] == 404:
        return []
    uris = r["body"] if isinstance(r["body"], list) else []
    if not uris and r["location"]:
        uris = [r["location"]]
    nums = [n for n in (_place_num(u) for u in uris) if n]
    if len(nums) != len(uris):
        log.debug("%s: Nicht-Orte in der Auflösung ignoriert: %s", qid, uris)
    return sorted(set(nums), key=int)


def fetch_place(client: CachedClient, num: str) -> dict | None:
    return client.get_json(f"{E}/place/{num}.json", params={"wskey": _require_key()}, allow_404=True)


def place_record(num: str, place: dict | None) -> SourceRecord:
    place = place or {}
    pl = place.get("prefLabel") or {}
    same = place.get("sameAs") or []
    return SourceRecord(
        source="europeana",
        source_id=num,
        label=pl.get("de") or pl.get("en") or next(iter(pl.values()), ""),
        url=f"https://www.europeana.eu/de/collections/place/{num}",
        qids=sorted({q for q in (qid_from_uri(s) for s in same if "wikidata.org" in s) if q}),
        # aktuelle Europeana-Form place/<n> (place/base/<n> wird nur umgeleitet), s. normalize.WITH_BASE
        ids={"P7704": [normalize_p7704(f"place/{num}")]},
    )


def record_count(place: dict | None) -> int:
    """Anzahl der Europeana-Objekte, die mit dem Ort verknüpft sind (isAggregatedBy.recordCount)."""
    return int(((place or {}).get("isAggregatedBy") or {}).get("recordCount") or 0)
