"""Wikidata-SPARQL: Einrichtungen je Land/Typ und Identifikator-Aussagen.

Leitlinien (Timeouts vermeiden):
- pro Land und Typ getrennt abfragen,
- kein Label-Service; Labels separat in Batches,
- keine wdt:P279*-Pfade, außer mit subclasses=True,
- Detaildaten über VALUES-Batches.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable

from .. import config
from ..http import CachedClient
from ..models import Statement, WikidataItem
from ..normalize import PROPERTIES

log = logging.getLogger(__name__)

TYPES = {"museum": "Q33506", "library": "Q7075", "archive": "Q166118"}
ENTITY = "http://www.wikidata.org/entity/"

# Fundstellen-Items (per SPARQL geprüft am 15.11.2026):
#   Q234110 = Europeana (so auch in 228 bestehenden P7704-Referenzen verwendet)
#   Q621630 = Deutsche Digitale Bibliothek
#   Achtung: Q6955 ist „19. Jahrhundert“, NICHT Europeana.
SOURCE_ITEMS = {"europeana": "Q234110", "ddb": "Q621630"}


def _strip(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _lit(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Wikidata:
    def __init__(self, client: CachedClient) -> None:
        self.client = client
        if not config.CONTACT:
            log.warning("GLAMSYNC_CONTACT ist leer – Wikimedia verlangt eine Kontaktangabe im User-Agent.")

    def query(self, sparql: str) -> list[dict[str, str]]:
        d = self.client.post_json(
            config.WIKIDATA_SPARQL,
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
            timeout=120,
        )
        return [{k: v["value"] for k, v in b.items()} for b in d["results"]["bindings"]]

    # --- Grundmenge -----------------------------------------------------------
    def country_label_en(self, country: str) -> str | None:
        rows = self.query(f'SELECT ?l WHERE {{ wd:{country} rdfs:label ?l FILTER(LANG(?l)="en") }}')
        return rows[0]["l"] if rows else None

    def base_set(self, country: str, type_qid: str, subclasses: bool = False) -> list[str]:
        """QIDs mit P31 = Typ (direkt bzw. mit Unterklassen) und P17 = Land."""
        path = "wdt:P31/wdt:P279*" if subclasses else "wdt:P31"
        rows = self.query(f"SELECT DISTINCT ?item WHERE {{ ?item {path} wd:{type_qid} ; wdt:P17 wd:{country} . }}")
        return sorted({_strip(r["item"]) for r in rows}, key=lambda q: int(q[1:]))

    # --- Detaildaten ----------------------------------------------------------
    def fetch_items(self, qids: Iterable[str], batch: int = 150) -> dict[str, WikidataItem]:
        qids = sorted(set(qids))
        items = {q: WikidataItem(q) for q in qids}
        prop_values = " ".join(f'("{p}" p:{p} ps:{p})' for p in PROPERTIES)
        for chunk in _chunks(qids, batch):
            values = " ".join(f"wd:{q}" for q in chunk)
            rows = self.query(f"""
SELECT ?item ?prop ?value ?st ?rank ?hasRef ?hasQual WHERE {{
  VALUES ?item {{ {values} }}
  VALUES (?prop ?p ?ps) {{ {prop_values} }}
  ?item ?p ?st . ?st ?ps ?value ; wikibase:rank ?rank .
  BIND(EXISTS {{ ?st prov:wasDerivedFrom ?ref }} AS ?hasRef)
  BIND(EXISTS {{ ?st ?pq ?qv . FILTER(STRSTARTS(STR(?pq), "http://www.wikidata.org/prop/qualifier/P")) }} AS ?hasQual)
}}""")
            for r in rows:
                it = items[_strip(r["item"])]
                it.claims.setdefault(r["prop"], []).append(
                    Statement(
                        value=r["value"],
                        statement_id=_strip(r["st"]).replace("-", "$", 1),
                        rank=_strip(r["rank"]).replace("Rank", "").lower(),
                        has_refs=r["hasRef"] == "true",
                        has_qualifiers=r["hasQual"] == "true",
                    )
                )
            rows = self.query(f"""
SELECT ?item ?prop ?v WHERE {{
  VALUES ?item {{ {values} }}
  {{ ?item wdt:P31 ?v . BIND("P31" AS ?prop) }} UNION {{ ?item wdt:P17 ?v . BIND("P17" AS ?prop) }}
}}""")
            for r in rows:
                it = items[_strip(r["item"])]
                if r["prop"] == "P31":
                    it.p31.append(_strip(r["v"]))
                else:
                    it.p17.append(_strip(r["v"]))
            self._labels(chunk, items)
        return items

    def _labels(self, chunk: list[str], items: dict[str, WikidataItem]) -> None:
        values = " ".join(f"wd:{q}" for q in chunk)
        rows = self.query(f"""
SELECT ?item ?lang ?l WHERE {{
  VALUES ?item {{ {values} }}
  ?item rdfs:label ?l . BIND(LANG(?l) AS ?lang) FILTER(?lang IN ("de", "en"))
}}""")
        for r in rows:
            items[_strip(r["item"])].labels[r["lang"]] = r["l"]

    # --- Rückwärtssuche nach Identifikatoren ----------------------------------
    def lookup(self, prop: str, values: Iterable[str], batch: int = 200) -> dict[str, list[str]]:
        """Wert → QIDs (alle Ränge außer deprecated), über ganz Wikidata."""
        values = sorted(set(values))
        out: dict[str, set[str]] = defaultdict(set)
        for chunk in _chunks(values, batch):
            rows = self.query(f"""
SELECT ?item ?value WHERE {{
  VALUES ?value {{ {" ".join(_lit(v) for v in chunk)} }}
  ?item p:{prop} ?st . ?st ps:{prop} ?value ; wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
}}""")
            for r in rows:
                out[r["value"]].add(_strip(r["item"]))
        return {k: sorted(v) for k, v in out.items()}

    def p7704_non_agent(self) -> list[dict[str, str]]:
        """Alle P7704-Aussagen außer agent/… (Organisationen, Orte, Begriffe, Fehlformen)."""
        rows = self.query("""
SELECT ?item ?value ?rank WHERE {
  ?item p:P7704 ?st . ?st ps:P7704 ?value ; wikibase:rank ?rank .
  FILTER(!STRSTARTS(?value, "agent/"))
}""")
        return [{"qid": _strip(r["item"]), "value": r["value"], "rank": _strip(r["rank"])} for r in rows]

    def resolve_redirects(self, qids: Iterable[str], batch: int = 200) -> dict[str, str]:
        """QID → Ziel-QID für Weiterleitungen."""
        qids = sorted(set(qids))
        out: dict[str, str] = {}
        for chunk in _chunks(qids, batch):
            rows = self.query(f"""
SELECT ?item ?target WHERE {{ VALUES ?item {{ {" ".join(f"wd:{q}" for q in chunk)} }} ?item owl:sameAs ?target . }}""")
            out.update({_strip(r["item"]): _strip(r["target"]) for r in rows})
        return out
