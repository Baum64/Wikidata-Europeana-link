"""Wikidata-SPARQL: Einrichtungen je Land/Typ und Identifikator-Aussagen.

Leitlinien (Timeouts vermeiden):
- pro Land und Typ getrennt abfragen,
- kein Label-Service; Labels separat in Batches,
- keine wdt:P279*-Pfade, außer mit subclasses=True,
- Detaildaten über VALUES-Batches.
"""

from __future__ import annotations

import logging
import re
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
#   Q28657655 = ISIL Datei und Sigelverzeichnis (so in 3.888 bestehenden P791-Referenzen)
#   Für „Museen in Österreich“ (P7917) gibt es kein Item → Referenz über S854 (URL des Eintrags).
SOURCE_ITEMS = {"europeana": "Q234110", "ddb": "Q621630", "isil": "Q28657655"}


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

    def big_cities(self, country: str, city_class: str = "Q1549591", min_population: int = 100_000,
                   subclasses: bool = True) -> list[str]:
        """Großstädte: P31 = Großstadt (Q1549591) ODER Stadt (Q515) mit ≥ min_population Einwohnern (P1082).

        Die Klasse allein reicht nicht – z. B. fehlt Berlin (P31 Stadtstaat/Metropole).
        """
        path = "wdt:P31/wdt:P279*" if subclasses else "wdt:P31"
        rows = self.query(f"""
SELECT DISTINCT ?item WHERE {{
  ?item wdt:P17 wd:{country} .
  {{ ?item {path} wd:{city_class} }}
  UNION {{ ?item {path} wd:Q515 ; wdt:P1082 ?pop . FILTER(?pop >= {int(min_population)}) }}
  FILTER NOT EXISTS {{ ?item wdt:P576 ?aufgeloest }}   # keine aufgelösten Städte (z. B. Altona)
}}""")
        return sorted({_strip(r["item"]) for r in rows}, key=lambda q: int(q[1:]))

    # Städte in den Niederlanden und Dänemark tragen P17 = Landesteil, nicht das Königreich
    EUROPE_EXTRA = {"Q55": "Niederlande", "Q35": "Dänemark"}

    def european_countries(self) -> dict[str, str]:
        """Souveräne Staaten (Q3624078) mit Kontinent Europa (P30 Q46), ohne aufgelöste → {QID: Name}."""
        rows = self.query("""
SELECT ?c ?l WHERE {
  ?c wdt:P31 wd:Q3624078 ; wdt:P30 wd:Q46 . FILTER NOT EXISTS { ?c wdt:P576 ?e }
  OPTIONAL { ?c rdfs:label ?l FILTER(LANG(?l) = "de") }
}""")
        out = {_strip(r["c"]): r.get("l", _strip(r["c"])) for r in rows}
        out.update(self.EUROPE_EXTRA)
        return dict(sorted(out.items(), key=lambda kv: kv[1]))

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

    def p227_statements(self, values: Iterable[str], batch: int = 250) -> list[dict[str, str]]:
        """Alle P227-Aussagen (jeder Rang) mit diesen Werten: [{qid, value, rank, refs, quals}]."""
        out = []
        for chunk in _chunks(sorted(set(values)), batch):
            rows = self.query(f"""
SELECT ?item ?value ?rank (COUNT(DISTINCT ?ref) AS ?refs) (COUNT(DISTINCT ?qp) AS ?quals) WHERE {{
  VALUES ?value {{ {" ".join(_lit(v) for v in chunk)} }}
  ?item p:P227 ?st . ?st ps:P227 ?value ; wikibase:rank ?rank .
  OPTIONAL {{ ?st prov:wasDerivedFrom ?ref }}
  OPTIONAL {{ ?st ?qp ?qv . FILTER(STRSTARTS(STR(?qp), "http://www.wikidata.org/prop/qualifier/P")) }}
}} GROUP BY ?item ?value ?rank""")
            out += [{"qid": _strip(r["item"]), "value": r["value"], "rank": r["rank"],
                     "refs": r.get("refs", "0"), "quals": r.get("quals", "0")} for r in rows]
        return out

    def p227_qualifiers(self, pairs: Iterable[tuple[str, str]], batch: int = 250) -> dict[tuple[str, str], list[dict]]:
        """(QID, GND) → P227-Aussagen mit diesem Wert: [{rank, has_p580, has_p582}] (jeder Qualifikatorwert zählt)."""
        out: dict[tuple[str, str], list[dict]] = {}
        for chunk in _chunks(sorted(set(pairs)), batch):
            rows = self.query(f"""
SELECT ?item ?value ?st ?rank ?s ?e WHERE {{
  VALUES (?item ?value) {{ {" ".join(f"(wd:{q} {_lit(v)})" for q, v in chunk)} }}
  ?item p:P227 ?st . ?st ps:P227 ?value ; wikibase:rank ?rank .
  BIND(EXISTS {{ ?st pq:P580 [] }} AS ?s) BIND(EXISTS {{ ?st pq:P582 [] }} AS ?e)
}}""")
            for r in rows:
                out.setdefault((_strip(r["item"]), r["value"]), []).append(
                    {"rank": r["rank"], "has_p580": r.get("s") in ("true", "1"), "has_p582": r.get("e") in ("true", "1")})
        return out

    def years(self, qids: Iterable[str], batch: int = 300) -> dict[str, tuple[set[int], set[int], bool, bool]]:
        """QID → (Jahre P571, Jahre P576, P571 vorhanden, P576 vorhanden).

        „Vorhanden“ zählt JEDE Aussage, gleich welcher Rang (auch deprecated) und welcher Wert (auch
        „unbekannter Wert“), damit Datumsvorschläge nie eine zweite Aussage erzeugen.
        """
        out: dict[str, tuple[set[int], set[int], bool, bool]] = {}
        for chunk in _chunks(sorted(set(qids)), batch):
            rows = self.query(f"""
SELECT ?item ?p ?v WHERE {{
  VALUES ?item {{ {" ".join("wd:" + q for q in chunk)} }}
  OPTIONAL {{ VALUES (?p ?ps) {{ (p:P571 ps:P571) (p:P576 ps:P576) }} ?item ?p ?st . ?st ?ps ?v }}
}}""")
            acc: dict[str, list] = {}
            for r in rows:
                t = acc.setdefault(_strip(r["item"]), [set(), set(), False, False])
                if not r.get("p"):
                    continue
                i = 0 if r["p"].endswith("P571") else 1
                t[2 + i] = True
                m = re.match(r"^([+-]?\d{1,4})-", r.get("v", ""))
                if m:
                    t[i].add(int(m.group(1)))
            for q, t in acc.items():
                out[q] = (t[0], t[1], t[2], t[3])
        return out

    def gnd_terms(self, values: Iterable[str], batch: int = 300) -> dict[str, dict]:
        """GND-Werte → Items mit P227 (nicht deprecated) und deren deutsche Bezeichnungen.

        QID → {gnd: set, human: bool (P31 = Q5), glam: bool (P31 Museum/Bibliothek/Archiv, mit
        Unterklassen), lde, lmul, dde: str, aliases: set (de + mul)}.
        """
        out: dict[str, dict] = {}
        for chunk in _chunks(sorted(set(values)), batch):
            rows = self.query(f"""
SELECT ?item ?g ?human ?glam ?lde ?lmul ?dde (GROUP_CONCAT(DISTINCT ?a; separator="\\t") AS ?aliases) WHERE {{
  VALUES ?g {{ {" ".join(_lit(v) for v in chunk)} }}
  ?item p:P227 ?st . ?st ps:P227 ?g ; wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
  OPTIONAL {{ ?item rdfs:label ?lde FILTER(LANG(?lde) = "de") }}
  OPTIONAL {{ ?item rdfs:label ?lmul FILTER(LANG(?lmul) = "mul") }}
  OPTIONAL {{ ?item schema:description ?dde FILTER(LANG(?dde) = "de") }}
  OPTIONAL {{ ?item skos:altLabel ?a FILTER(LANG(?a) IN ("de", "mul")) }}
  BIND(EXISTS {{ ?item wdt:P31 wd:Q5 }} AS ?human)
  BIND(EXISTS {{ VALUES ?c {{ wd:Q33506 wd:Q7075 wd:Q166118 }} ?item wdt:P31/wdt:P279* ?c }} AS ?glam)
}} GROUP BY ?item ?g ?human ?glam ?lde ?lmul ?dde""")
            for r in rows:
                t = out.setdefault(_strip(r["item"]), {"gnd": set(), "human": False, "glam": False, "lde": "", "lmul": "",
                                                        "dde": "", "aliases": set()})
                t["gnd"].add(r["g"])
                t["human"] = t["human"] or r.get("human") in ("true", "1")
                t["glam"] = t["glam"] or r.get("glam") in ("true", "1")
                for k in ("lde", "lmul", "dde"):
                    t[k] = t[k] or r.get(k, "")
                t["aliases"] |= {a for a in (r.get("aliases") or "").split("\t") if a}
        return out

    def fetch_extras(self, qids: Iterable[str], batch: int = 200):
        """Websites (P856), Aliasse (de/en) und Koordinaten (P625) für den Namens-/Website-Abgleich."""
        from ..registry import Extras  # spätes Import, um Zirkelbezug zu vermeiden

        qids = sorted(set(qids))
        out = {q: Extras() for q in qids}
        for chunk in _chunks(qids, batch):
            values = " ".join(f"wd:{q}" for q in chunk)
            rows = self.query(f"""
SELECT ?item ?w ?a ?c WHERE {{
  VALUES ?item {{ {values} }}
  {{ ?item wdt:P856 ?w }} UNION {{ ?item skos:altLabel ?a FILTER(LANG(?a) IN ("de", "en")) }}
  UNION {{ ?item wdt:P625 ?c }}
}}""")
            for r in rows:
                ex = out[_strip(r["item"])]
                if "w" in r:
                    ex.websites.append(r["w"])
                elif "a" in r:
                    ex.aliases.append(r["a"])
                elif "c" in r:
                    m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", r["c"])
                    if m:
                        ex.coords.append((float(m.group(2)), float(m.group(1))))
        return out

    def locations(self, qids: Iterable[str], batch: int = 200) -> dict[str, list[tuple[str, str]]]:
        """Orte der Items für den Ortsabgleich → {QID: [(Orts-QID, Label)]} (Labels de/fr/it/rm/en,
        je Sprache eine Zeile – Schweizer Orte heißen im Register oft „Genève“, „Lugano“ …).

        P131 eine und zwei Stufen (Bezirk → Stadt), P276 (Standort) und dessen P131, P159 (Sitz).
        """
        qids = sorted(set(qids))
        out: dict[str, list[tuple[str, str]]] = {q: [] for q in qids}
        for chunk in _chunks(qids, batch):
            rows = self.query(f"""
SELECT DISTINCT ?item ?p ?l WHERE {{
  VALUES ?item {{ {" ".join(f"wd:{q}" for q in chunk)} }}
  {{ ?item wdt:P131 ?p }} UNION {{ ?item wdt:P131/wdt:P131 ?p }} UNION {{ ?item wdt:P276 ?p }}
  UNION {{ ?item wdt:P276/wdt:P131 ?p }} UNION {{ ?item wdt:P159 ?p }}
  OPTIONAL {{ ?p rdfs:label ?l FILTER(LANG(?l) IN ("de", "fr", "it", "rm", "en")) }}
}}""")
            for r in rows:
                out[_strip(r["item"])].append((_strip(r["p"]), r.get("l", "")))
        return out

    def all_values(self, prop: str, prefix: str | None = None) -> list[tuple[str, str]]:
        """Alle (QID, Wert) einer Property (ohne deprecated), optional nur Werte mit Präfix."""
        return [(s["qid"], s["value"]) for s in self.all_statements(prop, prefix)]

    def all_statements(self, prop: str, prefix: str | None = None) -> list[dict]:
        """Alle Aussagen einer Property (ohne deprecated) mit Angabe, ob Quellen/Qualifier vorhanden sind."""
        filt = f'FILTER(STRSTARTS(UCASE(?v), "{prefix.upper()}"))' if prefix else ""
        rows = self.query(f"""
SELECT ?item ?v ?hasRef ?hasQual WHERE {{
  ?item p:{prop} ?st . ?st ps:{prop} ?v ; wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank) {filt}
  BIND(EXISTS {{ ?st prov:wasDerivedFrom ?ref }} AS ?hasRef)
  BIND(EXISTS {{ ?st ?pq ?qv . FILTER(STRSTARTS(STR(?pq), "http://www.wikidata.org/prop/qualifier/P")) }} AS ?hasQual)
}}""")
        return [{"qid": _strip(r["item"]), "value": r["v"], "has_refs": r["hasRef"] == "true",
                 "has_qualifiers": r["hasQual"] == "true"} for r in rows]

    def agent_query(self, body: str) -> list[dict[str, str]]:
        """Abfrage über alle agent/base/<n>- und agent/<n>-Werte, zerlegt nach erster Ziffer (vermeidet Timeouts).

        `body` enthält den Platzhalter {prefix} für FILTER(STRSTARTS(?v, "{prefix}")).
        """
        rows: list[dict[str, str]] = []
        for form in ("agent/base/", "agent/"):
            for d in "123456789":
                rows += self.query(body.replace("{prefix}", f"{form}{d}"))
        return rows

    def p7704_non_agent(self) -> list[dict[str, str]]:
        """Alle P7704-Aussagen außer agent/… (Organisationen, Orte, Begriffe, Fehlformen)."""
        rows = self.query("""
SELECT ?item ?value ?rank WHERE {
  ?item p:P7704 ?st . ?st ps:P7704 ?value ; wikibase:rank ?rank .
  FILTER(!STRSTARTS(?value, "agent/"))
}""")
        return [{"qid": _strip(r["item"]), "value": r["value"], "rank": _strip(r["rank"])} for r in rows]

    def entity_claims(self, qids: Iterable[str], props: Iterable[str], batch: int = 50) -> dict[str, dict]:
        """Über die Wikidata-API (nicht den Query-Service, der schnell drosselt), immer aktuell:
        QID → {labels: {sprache: label}, claims: {P: [{value, rank, refs, quals, id}]}}.
        Werte: Item-ID, Zeichenkette oder Zeitstring; deprecated-Aussagen sind enthalten."""
        props = list(props)
        out: dict[str, dict] = {}
        for chunk in _chunks(sorted(set(qids)), batch):
            d = self.client.get_json(config.WIKIDATA_API, params={
                "action": "wbgetentities", "ids": "|".join(chunk), "props": "claims|labels", "format": "json"})
            for q, e in (d or {}).get("entities", {}).items():
                if "missing" in e:
                    continue
                claims = {}
                for prop in props:
                    sts = []
                    for st in e.get("claims", {}).get(prop, []):
                        v = st["mainsnak"].get("datavalue", {}).get("value")
                        if isinstance(v, dict):
                            v = v.get("id") or v.get("time") or v
                        sts.append({"value": v, "rank": st.get("rank"), "id": st.get("id"),
                                    "refs": len(st.get("references", [])), "quals": len(st.get("qualifiers", {}))})
                    claims[prop] = sts
                out[q] = {"labels": {k: x["value"] for k, x in e.get("labels", {}).items()}, "claims": claims}
        return out

    def settlement_labels(self, places: Iterable[str], batch: int = 10) -> dict[str, set[str]]:
        """Ort-QID → Labels (de/mul) der Siedlungen mit P131 = Ort (Ortsteile, Stadtteile, Dörfer).

        Zwei leichte Abfragen statt P31/P279* über alle Kinder (zu schwer für große Städte):
        Kinder mit Klasse, dann welche dieser Klassen Siedlungen (Q486972) sind."""
        kids: list[tuple[str, str, str]] = []
        for chunk in _chunks(sorted(set(places)), batch):
            rows = self.query(f"""
SELECT ?place ?l ?t WHERE {{
  VALUES ?place {{ {" ".join("wd:" + q for q in chunk)} }}
  ?c wdt:P131 ?place ; wdt:P31 ?t ; rdfs:label ?l .
  FILTER(LANG(?l) IN ("de", "mul"))
}}""")
            kids += [(_strip(r["place"]), r["l"], _strip(r["t"])) for r in rows]
        settle: set[str] = set()
        for chunk in _chunks(sorted({t for _, _, t in kids}), 200):
            rows = self.query(f"""
SELECT ?t WHERE {{ VALUES ?t {{ {" ".join("wd:" + t for t in chunk)} }} ?t wdt:P279* wd:Q486972 . }}""")
            settle |= {_strip(r["t"]) for r in rows}
        out: dict[str, set[str]] = defaultdict(set)
        for place, label, t in kids:
            if t in settle:
                out[place].add(label)
        return dict(out)

    def class_by_label(self, labels: Iterable[str], roots: Iterable[str], batch: int = 60) -> dict[str, list[tuple[str, str]]]:
        """Deutsches Label → [(QID, Wurzel)] der Klassen, die (transitiv) Unterklasse einer Wurzel sind.
        Sucht auch die Form mit kleinem Anfangsbuchstaben („archäologisches Museum“)."""
        out: dict[str, list[tuple[str, str]]] = defaultdict(list)
        roots = " ".join("wd:" + r for r in roots)
        forms = {f: t for t in set(labels) for f in (t, t[:1].lower() + t[1:])}
        for chunk in _chunks(sorted(forms), batch):
            rows = self.query(f"""
SELECT DISTINCT ?l ?c ?root WHERE {{
  VALUES ?l {{ {" ".join(_lit(x) + "@de" for x in chunk)} }}
  VALUES ?root {{ {roots} }}
  ?c rdfs:label ?l ; wdt:P279+ ?root .
}}""")
            for r in rows:
                out[forms[r["l"]]].append((_strip(r["c"]), _strip(r["root"])))
        return dict(out)

    def resolve_redirects(self, qids: Iterable[str], batch: int = 200) -> dict[str, str]:
        """QID → Ziel-QID für Weiterleitungen."""
        qids = sorted(set(qids))
        out: dict[str, str] = {}
        for chunk in _chunks(qids, batch):
            rows = self.query(f"""
SELECT ?item ?target WHERE {{ VALUES ?item {{ {" ".join(f"wd:{q}" for q in chunk)} }} ?item owl:sameAs ?target . }}""")
            out.update({_strip(r["item"]): _strip(r["target"]) for r in rows})
        return out
