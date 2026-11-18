"""Europeana-Agents (Personen) über die Entity API, Typ „agent“.

Die Suche liefert rund 170.000 Agents in Seiten à 100. Über `fl` werden nur
sameAs und die Labels (de/en) angefordert; der Cache speichert pro Seite eine
kompakte Form (id, Labels, QIDs, GND), damit er klein bleibt.
"""

from __future__ import annotations

import logging

from .. import config
from ..http import CachedClient
from ..models import SourceRecord
from ..normalize import normalize_gnd, qid_from_uri
from .europeana import _require_key

log = logging.getLogger(__name__)
E = config.EUROPEANA_ENTITY
FIELDS = "owl_sameAs,skos_prefLabel.de,skos_prefLabel.en"


def _compact(page: dict) -> dict:
    out = []
    for a in page.get("items") or []:
        same = a.get("sameAs") or []
        out.append({
            "id": a["id"].rsplit("/", 1)[-1],
            "label": (a.get("prefLabel") or {}).get("de") or (a.get("prefLabel") or {}).get("en") or "",
            "qids": sorted({q for q in (qid_from_uri(s) for s in same if "wikidata.org" in s) if q}),
            "gnd": sorted({g for g in (normalize_gnd(s) for s in same if "d-nb.info/gnd/" in s) if g}),
        })
    return {"items": out, "next": bool(page.get("next")), "total": (page.get("partOf") or {}).get("total")}


def fetch_all(client: CachedClient) -> list[dict]:
    """Alle Agents in kompakter Form."""
    key = _require_key()
    agents: list[dict] = []
    page = 1
    while True:
        d = client.get_json(f"{E}/search", params={"query": "*", "type": "agent", "page": page, "pageSize": 100,
                                                   "fl": FIELDS, "wskey": key},
                            transform=_compact, cache_tag=":compact")
        agents.extend(d["items"])
        if page % 100 == 0:
            log.info("Europeana-Agents: Seite %d, %d geladen (von %s)", page, len(agents), d.get("total"))
        if not d["items"] or not d["next"]:
            break
        page += 1
    log.info("Europeana: %d Agents geladen", len(agents))
    return agents


def to_record(a: dict) -> SourceRecord:
    return SourceRecord(
        source="europeana", source_id=a["id"], label=a["label"],
        url=f"https://www.europeana.eu/de/collections/person/{a['id']}",
        qids=a["qids"], ids={"P7704": [f"agent/base/{a['id']}"], "P227": a["gnd"]},
    )


def fetch_agent(client: CachedClient, num: str) -> dict | None:
    """Einzelner Agent (für recordCount bei Dubletten und zur Prüfung nicht gelisteter IDs)."""
    return client.get_json(f"{E}/agent/{num}.json", params={"wskey": _require_key()}, allow_404=True)
