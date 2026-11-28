"""Ablaufsteuerung für fetch / compare / export-qs."""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from . import config, export
from .http import CachedClient, SkippedRequest
from .match import (
    RecordMatch,
    build_lookups,
    classify_record,
    decide_qid,
    hold_duplicate_fixes,
    name_candidates,
    p7704_duplicates,
    resolve_collisions,
    wikidata_format_findings,
)
from .models import FEHLT, FORMATFEHLER, IDENTISCH, KEIN_TREFFER, KONFLIKT, MEHRDEUTIG, Finding, SourceRecord, Statement, WikidataItem
from .normalize import PROPERTIES, compare_key, normalize
from .sources import ddb, europeana
from .sources.wikidata import TYPES, Wikidata

log = logging.getLogger(__name__)
STATUSES = [IDENTISCH, FEHLT, FORMATFEHLER, KONFLIKT, MEHRDEUTIG]


# --- Persistenz ----------------------------------------------------------------
def _data_path(name: str) -> Path:
    return config.DATA_DIR / f"{name}.json"


def _save(name: str, obj) -> Path:
    p = _data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _load(name: str):
    """Lädt data/<name>.json; Wikidata-Grundmengen nur, solange jünger als GLAMSYNC_WIKIDATA_CACHE_HOURS."""
    import time

    p = _data_path(name)
    if not p.is_file():
        return None
    if name.startswith("wikidata_") and time.time() - p.stat().st_mtime > config.WIKIDATA_CACHE_HOURS * 3600:
        log.info("%s ist älter als %g h – wird neu von Wikidata geladen", p.name, config.WIKIDATA_CACHE_HOURS)
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _items_from_json(d: dict) -> dict[str, WikidataItem]:
    out = {}
    for q, it in d.items():
        claims = {p: [Statement(**s) for s in sts] for p, sts in it["claims"].items()}
        out[q] = WikidataItem(q, it["labels"], it["p31"], it["p17"], claims)
    return out


def parse_types(types: str) -> list[str]:
    ts = [t.strip() for t in types.split(",") if t.strip()]
    unknown = [t for t in ts if t not in TYPES]
    if unknown:
        raise SystemExit(f"Unbekannte Typen: {unknown} (erlaubt: {', '.join(TYPES)})")
    return ts


# --- fetch -----------------------------------------------------------------------
def fetch_europeana(client: CachedClient) -> list[SourceRecord]:
    records = europeana.fetch(client)  # alle Länder; gefiltert wird in compare
    _save("europeana", [r.to_dict() for r in records])
    return records


def fetch_ddb(client: CachedClient, types: list[str] | None, limit: int | None) -> list[SourceRecord]:
    sectors = {ddb.TYPE_TO_SECTOR[t] for t in types} if types else None
    records = ddb.fetch(client, sectors=sectors, limit=limit)
    if not limit:
        _save("ddb_" + ("-".join(sorted(types)) if types else "alle"), [r.to_dict() for r in records])
    return records


def fetch_wikidata(client: CachedClient, country: str, types: list[str], subclasses: bool) -> dict[str, dict]:
    wd = Wikidata(client)
    result = {}
    for t in types:
        qids = wd.base_set(country, TYPES[t], subclasses)
        log.info("Wikidata %s/%s: %d Items", country, t, len(qids))
        items = wd.fetch_items(qids)
        result[t] = {q: asdict(it) for q, it in items.items()}
        _save(f"wikidata_{country}_{t}{'_sub' if subclasses else ''}", result[t])
    return result


# --- compare ---------------------------------------------------------------------
def compare(client: CachedClient, country: str, types: list[str], limit: int | None = None,
            subclasses: bool = True, sources: tuple[str, ...] = ("europeana", "ddb")) -> dict:
    wd = Wikidata(client)
    date = export.today()
    notes: list[str] = []

    # 1. Quellen laden – immer frisch aus dem HTTP-Cache aufgebaut, damit Änderungen
    #    an der Normalisierung sofort wirken (data/*.json ist nur Ausgabe).
    country_en = wd.country_label_en(country)
    all_eu = fetch_europeana(client)
    europeana_org_ids = {r.source_id for r in all_eu}
    records: list[SourceRecord] = []
    if "europeana" in sources:
        eu = [r for r in all_eu if r.country == country_en]
        records += eu[:limit] if limit else eu
    if "ddb" in sources:
        if country != "Q183":
            notes.append("DDB nur für Deutschland (Q183) berücksichtigt.")
        else:
            records += fetch_ddb(client, types, limit)
    log.info("Quell-Einrichtungen: %s", dict(Counter(r.source for r in records)))

    # 2. Grundmenge (nur ohne --limit, sonst Trockenlauf ohne Gesamtstatistik)
    base: dict[str, dict[str, WikidataItem]] = {}
    if not limit:
        for t in types:
            name = f"wikidata_{country}_{t}{'_sub' if subclasses else ''}"
            d = _load(name) or fetch_wikidata(client, country, [t], subclasses)[t]
            base[t] = _items_from_json(d)
    else:
        notes.append("Trockenlauf: Wikidata-Grundmenge nicht geladen, Formatprüfung nur für zugeordnete Items.")

    # 3. Rückwärtssuche der Identifikatoren in Wikidata
    raw_lookups: dict[str, dict[str, list[str]]] = {}
    for prop in ("P791", "P227", "P214", "P213"):
        vals = {v for r in records for v in r.ids.get(prop, [])}
        if prop == "P791":  # Groß-/Kleinschreibung: beide Varianten suchen
            vals |= {v.upper() for v in vals}
        if prop == "P213":  # falls mit Leerzeichen eingetragen
            vals |= {" ".join(v[i:i + 4] for i in range(0, 16, 4)) for v in vals}
        raw_lookups[prop] = wd.lookup(prop, vals) if vals else {}
    p7704_scan = wd.p7704_non_agent()
    raw_lookups["P7704"] = {}
    for r in p7704_scan:
        if "deprecated" not in r["rank"].lower():
            raw_lookups["P7704"].setdefault(r["value"], []).append(r["qid"])
    lookups = build_lookups(raw_lookups)

    # 4. Zuordnung
    redirects = wd.resolve_redirects({q for r in records for q in r.qids})
    matches: list[RecordMatch] = [decide_qid(r, lookups, redirects) for r in records]

    # 5. Detaildaten für alle beteiligten Items
    org_scan_qids = {r["qid"] for r in p7704_scan
                     if (normalize("P7704", r["value"]) or "").startswith("organisation/")
                     or not normalize("P7704", r["value"])}
    needed = {m.qid for m in matches if m.qid} | org_scan_qids
    items: dict[str, WikidataItem] = {}
    for t in base.values():
        items.update(t)
    items.update(wd.fetch_items(needed - set(items)))

    for m in matches:
        if m.qid and not (items[m.qid].labels or items[m.qid].p31 or items[m.qid].claims):
            m.note = (m.note + "; " if m.note else "") + "QID existiert nicht (gelöscht?)"
            m.qid, m.status = None, "kein_treffer"
        classify_record(m, items, lookups)

    findings = [f for m in matches for f in m.findings]
    already = {(f.qid, f.prop, f.old_value) for f in findings if f.old_value}
    wd_scope = {q: items[q] for q in (set(items) if base else needed)}
    wd_findings = wikidata_format_findings(wd_scope, europeana_org_ids, already)
    wd_findings += p7704_duplicates(p7704_scan)
    hold_duplicate_fixes(findings + wd_findings)
    resolve_collisions(findings + wd_findings)

    # 6. Ausgabe
    out = config.OUT_DIR
    match_rows = []
    for m in matches:
        r = m.record
        match_rows.append({
            "source": r.source, "source_id": r.source_id, "label": r.label, "url": r.url,
            "qid": m.qid or "", "wikidata_label": items[m.qid].label if m.qid else "",
            "match_via": m.via if m.qid else "", "match_status": m.status or "zugeordnet",
            "note": m.note, "findings": [f.to_dict() for f in m.findings],
        })
    export.write_report(match_rows, wd_findings, export.out_path(date, "report", ".csv"))

    unmatched = [m.record for m in matches if not m.qid]
    pool = {q: it for q, it in items.items()}
    cands = name_candidates(unmatched, pool)
    export.write_candidates(cands, export.out_path(date, "kandidaten_namen", ".csv"))

    results = {
        "date": date, "country": country, "types": types, "limit": limit, "subclasses": subclasses,
        "matches": match_rows, "wikidata_findings": [f.to_dict() for f in wd_findings],
    }
    results_path = export.out_path(date, "results", ".json")
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = build_summary(results, base, notes)
    summary["candidates"] = len(cands)
    export.write_summary(summary, export.out_path(date, "summary", ".md"))
    return summary


def build_summary(results: dict, base: dict[str, dict[str, WikidataItem]], notes: list[str]) -> dict:
    all_findings = [Finding(**f) for m in results["matches"] for f in m["findings"]]
    all_findings += [Finding(**f) for f in results["wikidata_findings"]]
    new = {(f.qid, f.prop) for f in all_findings if f.action == "new"}

    base_rows = {}
    for t, items in base.items():
        def has(it: WikidataItem, p: str) -> bool:
            return bool(it.values(p))
        before = Counter()
        after = Counter()
        for q, it in items.items():
            for key, props in (("isil", ["P791"]), ("gnd", ["P227"]), ("either", ["P791", "P227"]),
                               ("europeana", ["P7704"])):
                b = any(has(it, p) for p in props)
                before[key] += b
                after[key] += b or any((q, p) in new for p in props)
        base_rows[t] = {"total": len(items), "before": before, "after": after}

    matching: dict[str, Counter] = {}
    for m in results["matches"]:
        c = matching.setdefault(m["source"], Counter())
        c["total"] += 1
        c[m["match_via"] if m["qid"] else m["match_status"]] += 1

    qs = []
    for prop in PROPERTIES:
        for action in ("new", "fix", "delete"):
            groups = export.qs_commands(all_findings, prop, action, results["date"])
            if groups:
                qs.append({"prop": prop, "action": action, "commands": len(groups),
                           "files": len(export.split_parts(groups))})

    return {
        "date": results["date"], "country": results["country"], "types": results["types"],
        "limit": results["limit"], "subclasses": results.get("subclasses"), "base": base_rows, "matching": matching,
        "classification": export.count_classification(results["matches"], [Finding(**f) for f in results["wikidata_findings"]]),
        "statuses": STATUSES, "qs": qs, "notes": notes,
    }


# --- export-qs -------------------------------------------------------------------
def export_qs(prop: str, action: str, date: str | None = None) -> list[Path]:
    date = date or export.today()
    path = export.out_path(date, "results", ".json")
    if not path.is_file():
        candidates = sorted(config.OUT_DIR.glob("????-??-??_results.json"))
        if not candidates:
            raise SystemExit("Keine Ergebnisse gefunden – zuerst `compare` ausführen.")
        path = candidates[-1]
    results = json.loads(path.read_text(encoding="utf-8"))
    findings = [Finding(**f) for m in results["matches"] for f in m["findings"]]
    findings += [Finding(**f) for f in results["wikidata_findings"]]
    props = PROPERTIES if prop == "all" else [prop]
    actions = ["new", "fix", "delete"] if action == "all" else [action]
    written = []
    for p in props:
        for a in actions:
            written += export.write_qs(findings, p, a, results["date"])
    return written


# --- Register ohne gemeinsamen Schlüssel (ISIL-Verzeichnis, Museen in Österreich) -----
REGISTRY_SOURCES = {
    # Quelle: (Property, Standard-Typen)
    "isil": ("P791", ["museum", "library", "archive"]),
    "mure": ("P7917", ["museum"]),
}
# Länderpräfix der ISIL je Land
ISIL_PREFIX = {"Q183": "DE", "Q40": "AT", "Q39": "CH"}


def registry_compare(client: CachedClient, source: str, country: str, types: list[str] | None = None,
                     subclasses: bool = True) -> dict:
    from . import registry as reg
    from .sources import registry as reg_src

    wd = Wikidata(client)
    date = export.today()
    prop, default_types = REGISTRY_SOURCES[source]
    types = types or default_types

    # 1. Register laden
    if source == "isil":
        prefix = ISIL_PREFIX.get(country)
        if not prefix:
            raise SystemExit(f"Kein ISIL-Präfix für {country} hinterlegt ({ISIL_PREFIX}).")
        # Schweiz: vollständiges Verzeichnis der Nationalbibliothek statt der wenigen CH-ISIL in Berlin
        records = reg_src.fetch_isil_ch(client) if country == "Q39" else reg_src.fetch_isil(client, prefix)
        value_prefix = prefix + "-"
    else:
        if country != "Q40":
            raise SystemExit("Museen in Österreich gibt es nur für --country Q40.")
        records = reg_src.fetch_mure(client)
        value_prefix = None

    # 2. Wikidata: vorhandene Werte (ganz Wikidata) und Kandidaten-Pool (Land × Typen)
    statements = wd.all_statements(prop, value_prefix)
    value_rows = [(st["qid"], st["value"]) for st in statements]
    value_index: dict[str, set[str]] = {}
    item_values: dict[str, list[str]] = {}
    for q, v in value_rows:
        value_index.setdefault(compare_key(prop, normalize_value(prop, v)), set()).add(q)
        item_values.setdefault(q, []).append(v)

    items: dict[str, WikidataItem] = {}
    for t in types:
        name = f"wikidata_{country}_{t}{'_sub' if subclasses else ''}"
        d = _load(name) or fetch_wikidata(client, country, [t], subclasses)[t]
        items.update(_items_from_json(d))
    extras = wd.fetch_extras(items)
    pool = reg.Pool(dict(items), extras)  # Kopie: items wird unten für den Bericht ergänzt

    # 3. Abgleich
    matches = reg.classify_registry(records, pool, value_index, item_values)
    sure = reg.mark_sure(matches, pool, wd.locations(pool.items), item_values, prop)
    log.info("Register: %d sichere Kandidaten (Ort gleich, Label sehr ähnlich)", sure)
    # Löschvorschläge nur gegen ein vollständiges Register: CH inkl. inaktiver ISIL (eine ISIL bleibt
    # gültig, auch wenn die Einrichtung geschlossen ist); AT-ISIL führt Berlin nur vereinzelt → nur Bericht
    known = reg_src.fetch_isil_ch(client, include_inactive=True) if source == "isil" and country == "Q39" else records
    registry_keys = {compare_key(prop, r.value) for r in known}
    complete = not (source == "isil" and country == "Q40")
    cleanup = reg.registry_cleanup(statements, registry_keys, prop, value_prefix, complete)
    # Labels für Items außerhalb des Pools nachladen (für Bericht)
    outside = {q for m in matches for q in m.qids} | {c.qid for c in cleanup}
    items.update(wd.fetch_items(outside - set(items)))

    # 4. Ausgabe
    out = config.OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    stem = export.out_path(date, f"register_{source}_{country}", "", prop).name
    export.write_registry_report(matches, items, out / f"{stem}.csv")
    export.write_registry_cleanup(cleanup, items, prop, out / f"{stem}_bereinigung.csv")
    qs_files = export.write_registry_qs(matches, prop, source, country, date)
    qs_files += export.write_registry_sure_qs(matches, prop, source, country, date)
    qs_files += export.write_registry_delete_qs(cleanup, prop, country, date)
    counts = Counter(m.status for m in matches)
    summary = {
        "date": date, "source": source, "prop": prop, "country": country, "types": types, "subclasses": subclasses,
        "records": len(records), "pool": len(pool.items), "counts": counts,
        "cleanup": Counter((c.kind, c.action or "bericht") for c in cleanup),
        "qs_files": [p.name for p in qs_files], "statuses": reg.STATUSES,
        "qs_commands": counts.get(reg.STARK, 0),
    }
    export.write_registry_summary(summary, out / f"{stem}.md")
    return summary


def normalize_value(prop: str, v: str) -> str:
    return (normalize(prop, v) if prop in PROPERTIES else v.strip().upper()) or v


# --- Städte: Europeana-Orte ↔ Wikidata (P7704 place/<n>) ----------------------
BIG_CITY = "Q1549591"  # Großstadt


def cities_compare(client: CachedClient, country: str = "europa", city_class: str = BIG_CITY,
                   subclasses: bool = True, min_population: int = 100_000) -> dict:
    from . import persons as P
    from .match import classify_city
    from .sources import places

    wd = Wikidata(client)
    date = export.today()

    # 1. Städte aus Wikidata – je Land einzeln abfragen (vermeidet Timeouts)
    if country.lower() in ("europa", "europe"):
        countries = wd.european_countries()
        tag_country = "europa"
    else:
        countries = {c.strip(): c.strip() for c in country.split(",")}
        tag_country = country.replace(",", "-")
    city_country: dict[str, str] = {}
    failed: list[str] = []
    for c, name in countries.items():
        try:
            found = wd.big_cities(c, city_class, min_population, subclasses)
        except Exception as exc:  # z. B. SPARQL-Timeout bei einem Land
            log.warning("Städte für %s (%s) nicht abrufbar: %s", name, c, exc)
            failed.append(f"{name} ({c})")
            continue
        for q in found:
            city_country.setdefault(q, name)
        log.info("Wikidata: %s – %d Städte", name, len(found))
    qids = sorted(city_country, key=lambda q: int(q[1:]))
    items = wd.fetch_items(qids)
    log.info("Wikidata: %d Städte insgesamt", len(qids))

    # 2. Europeana-Orte je Stadt über sameAs auflösen
    resolved = {q: places.resolve_qid(client, q) for q in qids}
    raw = {n: places.fetch_place(client, n) for ns in resolved.values() for n in ns}
    recs = {n: places.place_record(n, d) for n, d in raw.items()}
    counts = {n: places.record_count(d) for n, d in raw.items()}

    # 3. Rückwärtssuche: Hängt ein Ort schon an einem anderen Item?
    vals = {v for n in recs for v in (f"place/base/{n}", f"place/{n}")}
    lookups = build_lookups({"P7704": wd.lookup("P7704", vals) if vals else {}})

    # 4. Klassifikation
    findings = []
    for q in qids:
        f = classify_city(q, items[q], [recs[n] for n in resolved[q]], lookups, counts)
        findings.append(f)
    resolve_collisions(findings)
    # Orte ohne Objekte in Europeana: eigene QS-Datei (wie bei den Personen)
    log.info("Neu-Vorschläge ohne Objekte in Europeana: %d", P.mark_without_objects(findings, counts))
    # Formatprüfung bereits vorhandener P7704-Werte der Städte (Korrektur/Löschung)
    already = {(f.qid, f.prop, f.old_value) for f in findings if f.old_value}
    wd_findings = [f for f in wikidata_format_findings(items, set(), already) if f.prop == "P7704"]

    # 5. Ausgabe
    tag = f"staedte_{tag_country}"
    out = config.OUT_DIR
    files = []
    regular = [f for f in findings + wd_findings if f.status != P.OHNE_OBJEKTE]
    for action in ("new", "fix", "delete"):
        files += export.write_qs(regular, "P7704", action, date, tag=tag)
    files += export.write_qs([f for f in findings if f.status == P.OHNE_OBJEKTE], "P7704", "new", date,
                             tag=tag, label="ohne_objekte")

    rows = []
    for f in findings + wd_findings:
        nums = (f.source_id or "").split(",") if f.source_id else []
        rec = recs.get(nums[0]) if len(nums) == 1 else None
        rows.append({
            "land": city_country.get(f.qid, ""), "qid": f.qid, "wikidata_label": items[f.qid].label, "europeana_id": f.source_id or "",
            "europeana_label": rec.label if rec else "", "europeana_url": rec.url if rec else "",
            "status": f.status, "P7704_vorschlag": f.source_value or "",
            "P7704_wikidata": f.old_value or "|".join(f.wikidata_values), "aktion": f.action or "",
            "hinweis": f.note,
        })
    rows.sort(key=lambda r: (r["land"], r["status"], r["wikidata_label"]))
    export.write_city_report(rows, export.out_path(date, f"staedte_{tag_country}", ".csv", "P7704"))

    per_country: dict[str, Counter] = {}
    for f in findings:
        per_country.setdefault(city_country[f.qid], Counter())[f.status] += 1

    summary = {
        "date": date, "country": tag_country, "countries": len(countries), "failed": failed,
        "per_country": {k: dict(v) for k, v in sorted(per_country.items())}, "class": city_class, "subclasses": subclasses, "cities": len(qids),
        "min_population": min_population,
        "status": dict(Counter(f.status for f in findings).most_common()),
        "files": [p.name for p in files],
        "duplicates": [f"{city_country[q]}: {q} {items[q].label}: " + ", ".join(f"place/{n} ({counts[n]} Objekte)" for n in ns)
                       for q, ns in resolved.items() if len(ns) > 1],
    }
    if wd_findings:
        summary["status"]["formatpruefung_wikidata"] = len(wd_findings)
    export.write_city_summary(summary, export.out_path(date, f"staedte_{tag_country}", ".md", "P7704"))
    return summary


# --- Personen: Europeana-Agents ↔ Wikidata (P7704 agent/<n>, P227) -------------
def persons_compare(client: CachedClient, gnd_search: bool = False) -> dict:
    from collections import defaultdict

    from . import persons as P
    from .sources import agents as agent_src
    from .sources import places
    from .sources.gnd import gnd_info, gnd_problem

    wd = Wikidata(client)
    date = export.today()

    # 1. Europeana-Agents
    recs = [agent_src.to_record(a) for a in agent_src.fetch_all(client)]
    by_num = {r.source_id: r for r in recs}

    # 2. Alle agent/…-Werte in Wikidata (eine Abfrage, ~160.000 Zeilen)
    scan = wd.agent_query("""
SELECT ?item ?v WHERE {
  ?item p:P7704 ?st . ?st ps:P7704 ?v ; wikibase:rank ?rank .
  FILTER(STRSTARTS(?v, "{prefix}")) FILTER(?rank != wikibase:DeprecatedRank)
}""")
    present: dict[str, set[str]] = defaultdict(set)
    value_owner: dict[str, set[str]] = defaultdict(set)
    for r in scan:
        q, v = r["item"].rsplit("/", 1)[-1], normalize("P7704", r["v"]) or r["v"]
        present[q].add(v)
        value_owner[v].add(q)
    log.info("Wikidata: %d agent-Werte an %d Items", len(scan), len(present))
    # veraltete Werte (wenige): blockieren denselben Neu-Vorschlag am Item
    deprecated: dict[str, set[str]] = defaultdict(set)
    for r in wd.query("""
SELECT ?item ?v WHERE {
  ?item p:P7704 ?st . ?st ps:P7704 ?v ; wikibase:rank wikibase:DeprecatedRank . FILTER(STRSTARTS(?v, "agent/"))
}"""):
        deprecated[r["item"].rsplit("/", 1)[-1]].add(normalize("P7704", r["v"]) or r["v"])

    # 3. Gruppierung nach QID (sameAs); Agents mit mehreren QIDs nur Bericht
    groups: dict[str, list] = defaultdict(list)
    multi: list[Finding] = []
    no_qid = []
    for r in recs:
        if len(r.qids) == 1:
            groups[r.qids[0]].append(r)
        elif r.qids:
            multi.append(Finding("P7704", MEHRDEUTIG, "", "europeana", r.source_id, P.agent_value(r), [],
                                 note=f"sameAs nennt mehrere QIDs: {', '.join(r.qids)}"))
        else:
            no_qid.append(r)
    # Rückfall ohne QID: GND → P227 (nur bei eindeutigem Treffer)
    if no_qid:
        owners = wd.lookup("P227", {g for r in no_qid for g in r.ids["P227"]})
        for r in no_qid:
            hits = {q for g in r.ids["P227"] for q in owners.get(g, [])}
            if len(hits) == 1:
                groups[hits.pop()].append(r)
            else:
                multi.append(Finding("P7704", MEHRDEUTIG if hits else KEIN_TREFFER, "", "europeana",
                                     r.source_id, P.agent_value(r), [],
                                     note=f"ohne QID; GND trifft {', '.join(sorted(hits)) or 'nichts'}"))

    # 4. Detaildaten: Items ohne agent-Wert und – nur mit gnd_search – zugeordnete Items ohne GND
    #    (die P227-Suche ist teuer und hat zuletzt keine Vorschläge ergeben → standardmäßig aus)
    gap = {q for q in groups if not present.get(q)}
    gnd_cands: set[str] = set()
    if gnd_search:
        no_gnd = {r["item"].rsplit("/", 1)[-1] for r in wd.agent_query("""
SELECT ?item WHERE { ?item wdt:P7704 ?v . FILTER(STRSTARTS(?v, "{prefix}")) FILTER NOT EXISTS { ?item wdt:P227 ?g } }""")}
        gnd_cands = {q for q in groups if q in no_gnd and any(r.ids["P227"] for r in groups[q])}
    items = wd.fetch_items(gap | gnd_cands)
    missing = {q for q, it in items.items() if not (it.labels or it.p31 or it.claims)}
    redirects = wd.resolve_redirects(missing) if missing else {}
    for old, new in redirects.items():
        groups[new].extend(groups.pop(old))
        items.pop(old, None)
    new_targets = {t for t in redirects.values() if t not in items and not present.get(t)}
    if new_targets:
        items.update(wd.fetch_items(new_targets))
    log.info("Personen: %d Items ohne agent-Wert, %d Kandidaten für P227, %d Weiterleitungen",
             len(gap), len(gnd_cands), len(redirects))

    # 5. Wem gehören die GND in Wikidata?
    relevant_gnd = {g for q in groups if q in items for r in groups[q] for g in r.ids["P227"]}
    gnd_owner = {g: set(qs) for g, qs in wd.lookup("P227", relevant_gnd).items()} if relevant_gnd else {}

    # 6. Europeana-Dubletten: Objektzahlen nur dort abrufen, wo nötig
    counts: dict[str, int] = {}
    for q, rs in groups.items():
        if len(rs) > 1 and not present.get(q) and q in items:
            for r in rs:
                counts[r.source_id] = places.record_count(agent_src.fetch_agent(client, r.source_id))

    # 7. Klassifikation P7704
    findings: list[Finding] = list(multi)
    matched: dict[str, object] = {}
    for q, rs in groups.items():
        item = None if q in missing else items.get(q)  # verbliebene „missing“ = gelöschte Items
        f, rec = P.classify_person(q, rs, present.get(q, set()), item, value_owner, gnd_owner, counts)
        findings.append(f)
        if rec is not None and f.status in (IDENTISCH, FEHLT):
            matched[q] = rec
    resolve_collisions(findings)
    log.info("Neu-Vorschläge mit veraltetem Wert in Wikidata: %d", P.mark_deprecated(findings, deprecated))

    # 7b. Objektzahl je Neu-Vorschlag: Agents ohne Objekte kommen in eine eigene QS-Datei
    todo_counts = [f.source_id for f in findings
                   if f.prop == "P7704" and f.status == FEHLT and f.action == "new" and f.source_id not in counts]
    log.info("Europeana: Objektzahl für %d neue Agents", len(todo_counts))
    for i, sid in enumerate(todo_counts, 1):
        counts[sid] = places.record_count(agent_src.fetch_agent(client, sid)) or 0
        if i % 2000 == 0:
            log.info("Objektzahl: %d/%d", i, len(todo_counts))
    log.info("Neu-Vorschläge ohne Objekte in Europeana: %d", P.mark_without_objects(findings, counts))

    # 8. P227 aus Europeana, geprüft über lobid (nur mit gnd_search)
    gnd_problems: dict[str, str | None] = {}
    todo: set[str] = set()
    gnd_findings: list[Finding] = []
    if gnd_search:
        todo = {r.ids["P227"][0] for q, r in matched.items()
                if q in items and len(r.ids["P227"]) == 1 and not items[q].values("P227")
                and "Q5" in items[q].p31 and not (gnd_owner.get(r.ids["P227"][0], set()) - {q})}
        log.info("lobid: prüfe %d GND", len(todo))
        for g in sorted(todo):
            try:
                gnd_problems[g] = gnd_problem(g, gnd_info(client, g))
            except SkippedRequest as exc:  # ungeprüfte GND → kein Vorschlag
                log.warning("lobid %s: %s", g, exc)
        # ungeprüfte (übersprungene) GND → person_gnd meldet „GND nicht geprüft“ (nur Bericht)
        gnd_findings = [f for q, r in matched.items()
                        if (f := P.person_gnd(q, r, items.get(q), gnd_owner, gnd_problems)) is not None]

    # 9. Rückwärts: Wikidata-Werte, deren Nummer Europeana nicht (mehr) führt
    orphan = [(q, v) for q, vs in present.items() for v in vs if v.rsplit("/", 1)[-1] not in by_num]
    log.info("Wikidata-Werte ohne Europeana-Agent in der Liste: %d", len(orphan))
    orphan_items = wd.fetch_items({q for q, _ in orphan}) if orphan else {}
    cleanups = []
    for q, v in orphan:
        st = next((s for s in orphan_items.get(q, WikidataItem(q)).values("P7704")
                   if normalize("P7704", s.value) == v), None)
        if st is None:
            # Wert steht nicht mehr am Item (z. B. schon gelöscht, Query-Service hinkt nach) –
            # ohne Rohwert wäre ein Löschbefehl ohnehin wirkungslos
            log.info("%s: %s nicht mehr in Wikidata – übersprungen", q, v)
            continue
        c = P.agent_cleanup(q, v, agent_src.fetch_agent(client, v.rsplit("/", 1)[-1]),
                            bool(st and st.has_refs), bool(st and st.has_qualifiers), value_owner)
        if c:
            c.old_raw = st.value  # Rohwert, wie er in Wikidata steht (ggf. mit base)
            cleanups.append(c)
    cleanup_findings = [
        Finding("P7704", FORMATFEHLER if c.kind == "zusammengefuehrt" else KONFLIKT, c.qid, "europeana",
                (c.new_value or "").rsplit("/", 1)[-1], c.new_value,
                sorted(present.get(c.qid, set())), old_value=c.old_raw, action=c.action, note=c.note)
        for c in cleanups]
    for f in cleanup_findings:
        if f.action == "fix" and f.source_value in f.wikidata_values:
            f.note += "; korrekter Wert bereits vorhanden – nur löschen"

    # 10. Ausgabe
    out = config.OUT_DIR
    files = []
    files += export.write_qs([f for f in findings if f.status != P.OHNE_OBJEKTE], "P7704", "new", date,
                             tag="personen")
    files += export.write_qs([f for f in findings if f.status == P.OHNE_OBJEKTE], "P7704", "new", date,
                             tag="personen", label="ohne_objekte")
    files += export.write_qs(gnd_findings, "P227", "new", date, tag="personen")
    files += export.write_qs(cleanup_findings, "P7704", "fix", date, tag="personen")
    files += export.write_qs(cleanup_findings, "P7704", "delete", date, tag="personen", label="loeschen_pruefen")

    labels = {q: it.label for q, it in {**orphan_items, **items}.items()}
    rows = []
    for f in findings + gnd_findings + cleanup_findings:
        if f.prop == "P7704" and f.status == IDENTISCH:
            continue  # nur Abweichungen in den Bericht (identische zählt die Zusammenfassung)
        rec = by_num.get((f.source_id or "").split(",")[0])
        rows.append({
            "qid": f.qid, "wikidata_label": labels.get(f.qid, ""), "property": f.prop, "status": f.status,
            "europeana_id": f.source_id or "", "europeana_label": rec.label if rec else "",
            "europeana_url": rec.url if rec else "", "vorschlag": f.source_value or "",
            "wikidata": f.old_value or "|".join(f.wikidata_values), "aktion": f.action or "", "hinweis": f.note,
        })
    rows.sort(key=lambda r: (r["property"], r["status"], r["qid"]))
    export.write_person_report(rows, export.out_path(date, "personen", ".csv"))

    summary = {
        "date": date, "agents": len(recs), "wikidata_values": len(scan), "items": len(groups),
        "p7704": dict(Counter(f.status for f in findings).most_common()),
        "p227": dict(Counter(f.status for f in gnd_findings).most_common()),
        "cleanup": dict(Counter(f"{c.kind}/{c.action or 'bericht'}" for c in cleanups).most_common()),
        "gnd_checked": len(gnd_problems), "files": [p.name for p in files],
    }
    export.write_person_summary(summary, export.out_path(date, "personen", ".md"))
    return summary


# --- Organisationen: Europeana ↔ Wikidata europaweit (P7704 organisation/<n>) ----------------
ORG_CLASS_CACHE = "klassen_organisation"  # data/klassen_organisation.json: Klasse → Unterklasse von Q43229?


def _org_classes(wd: Wikidata, classes: set[str]) -> dict[str, bool]:
    """P279*-Prüfung gegen Organisation (Q43229) mit dauerhaftem Zwischenspeicher (teure Abfrage)."""
    from .orgs import ORGANIZATION

    known: dict[str, bool] = _load(ORG_CLASS_CACHE) or {}
    todo = sorted(classes - set(known))
    if todo:
        log.info("Klassenprüfung (Organisation): %d neue Klassen", len(todo))
        ok = wd.subclasses_of(todo, ORGANIZATION)
        known.update({c: c in ok for c in todo})
        _save(ORG_CLASS_CACHE, dict(sorted(known.items())))
    return known


def organisations_compare(client: CachedClient, candidates: bool = True) -> dict:
    from collections import defaultdict

    from . import orgs as O
    from .persons import ABGELEHNT, OHNE_OBJEKTE

    wd = Wikidata(client, patient=True)  # lange Abfrageserien: bei Drosselung warten statt abbrechen
    date = export.today()

    # 1. Europeana-Organisationen (alle Länder, auch ohne Land)
    orgs = {o.num: o for o in (O.org_from_raw(r) for r in europeana.fetch_all_raw(client))}

    # 2. Wikidata: alle P7704 außer agent/… (Organisationen, Orte, Fehlformen)
    scan = wd.p7704_non_agent()
    value_owner: dict[str, set[str]] = defaultdict(set)
    scan_qids: set[str] = set()
    for r in scan:
        v = normalize("P7704", r["value"])
        if not v or v.startswith("organisation/"):
            scan_qids.add(r["qid"])
        if v and v.startswith("organisation/") and "deprecated" not in r["rank"].lower():
            value_owner[v].add(r["qid"])
    log.info("Wikidata: %d organisation-Werte, %d Items mit organisation-/Fehlform-Werten",
             sum(len(q) for q in value_owner.values()), len(scan_qids))

    # 3. Stufe 1: sameAs → QID (Weiterleitungen aufgelöst)
    redirects = wd.resolve_redirects({q for o in orgs.values() for q in o.qids})
    groups: dict[str, list] = defaultdict(list)
    multi, no_qid = [], []
    for o in orgs.values():
        o.qids = sorted({redirects.get(q, q) for q in o.qids})
        if len(o.qids) == 1:
            groups[o.qids[0]].append(o)
        elif o.qids:
            multi.append(o)
        else:
            no_qid.append(o)
    claimed: dict[str, list[str]] = defaultdict(list)
    for o in orgs.values():
        for q in o.qids:
            claimed[q].append(o.num)

    # 4. Stufe 2: Organisationen ohne sameAs – Kandidaten über Website (P856) und GND/VIAF/ISNI
    cands: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    todo = [o for o in no_qid if o.value not in value_owner]
    if candidates:
        full_map: dict[str, set[str]] = defaultdict(set)
        dom_map: dict[str, set[str]] = defaultdict(set)
        for o in todo:
            for h in o.homepages:
                full, dom = O.website_variants(h)
                for v in full:
                    full_map[v].add(o.num)
                for v in dom:
                    dom_map[v].add(o.num)
        log.info("Stufe 2: %d Organisationen ohne sameAs, %d Website-Varianten", len(todo),
                 len(set(full_map) | set(dom_map)))
        for iri, qs in wd.by_website(set(full_map) | set(dom_map)).items():
            for kind, m in (("website", full_map), ("domain", dom_map)):
                for num in m.get(iri, ()):
                    for q in qs:
                        cands[num][q].add(kind)
        for prop in O.ID_PROPS:
            vals = {v for o in todo for v in o.ids.get(prop, [])}
            owners = wd.lookup(prop, vals) if vals else {}
            for o in todo:
                for v in o.ids.get(prop, []):
                    for q in owners.get(v, []):
                        cands[o.num][q].add(prop)
        log.info("Stufe 2: Kandidaten für %d Organisationen", len(cands))

    # 5. Detaildaten, Namen (alle Sprachen) und Klassenprüfung
    cand_q = {q for d in cands.values() for q in d}
    items = wd.fetch_items(set(groups) | {q for o in multi for q in o.qids} | cand_q | scan_qids)
    missing = {q for q, it in items.items() if not (it.labels or it.p31 or it.claims)}
    gap = {q for q in groups if q not in missing and not O.present_values(items[q])}
    name_q = (gap | cand_q) - missing
    raw_names = wd.all_names(name_q)
    names = {q: list(d["labels"].values()) + d["aliases"] for q, d in raw_names.items()}
    known = _org_classes(wd, {c for q in name_q for c in items[q].p31})
    # Gemeinden, Regionen u. Ä. liegen oft auch unter Organisation; die Einwohnerzahl (P1082)
    # kennzeichnet das Gebiet – die Verwaltung (Ministerium, Bistum, County Council) hat keine.
    populated = wd.having(name_q, "P1082")
    is_org = {q: any(known.get(c) for c in items[q].p31) and q not in populated for q in name_q}

    def label(q: str) -> str:
        it, ls = items.get(q), raw_names.get(q, {}).get("labels", {})
        return (it.label if it else "") or ls.get("de") or ls.get("en") or ls.get("mul") or next(iter(ls.values()), "")

    # 6. Klassifikation
    stage1 = []
    for q, os_ in groups.items():
        f, _ = O.classify_org(q, os_, None if q in missing else items[q], names.get(q, []),
                              is_org.get(q, False), value_owner)
        stage1.append(f)
    stage1 += [O.multi_qid(o, items) for o in multi]
    stage2 = [O.classify_candidate(o, cands.get(o.num, {}), items, names, is_org, claimed, value_owner)
              for o in no_qid]
    resolve_collisions(stage1 + stage2)
    territory = "Gebiet: Item hat eine Einwohnerzahl (P1082)"
    for f in stage1:
        if f.status == O.KEIN_ORGANISATION and f.qid in populated:
            f.note = f"{territory}; {f.note}"
    for f in stage2:
        for q in populated & set(cands.get(f.source_id, {})):
            f.note = f.note.replace(f"{q} keine Organisation", f"{q} keine Organisation ({territory})")

    # 7. Formatprüfung vorhandener Werte (organization/ → organisation/, Fehlformen) und Dubletten
    already = {(f.qid, f.prop, f.old_value) for f in stage1 + stage2 if f.old_value}
    scope = {q: items[q] for q in scan_qids | set(groups) if q in items and q not in missing}
    wd_findings = [f for f in wikidata_format_findings(scope, set(orgs), already) if f.prop == "P7704"]
    wd_findings += p7704_duplicates(scan)
    hold_duplicate_fixes(wd_findings)

    # 8. Ausgabe
    def country_key(f):
        o = orgs.get((f.source_id or "").split(",")[0])
        return ((o.country or "~") if o else "~", (o.label if o else "").casefold())

    stage1.sort(key=country_key)
    stage2.sort(key=lambda f: (f.status != O.KANDIDAT_STARK, *country_key(f)))
    labels = {q: label(q) for q in items}
    commented = {O.NAME_PRUEFEN, O.KANDIDAT_SCHWACH}
    tag = "organisationen"

    def write(fs, name):
        return export.write_qs_groups(O.qs_groups(fs, date, orgs, labels, commented), "P7704", tag, name, date)

    def objects(f):
        return orgs[f.source_id].record_count if f.source_id in orgs else 0

    cand_fs = [f for f in stage2 if f.status in (O.KANDIDAT_STARK, O.KANDIDAT_SCHWACH)]
    files = []
    files += write([f for f in stage1 if f.status == FEHLT], "neu")
    files += write([f for f in stage1 if f.status == OHNE_OBJEKTE], "ohne_objekte")
    files += write([f for f in stage1 if f.status == O.NAME_PRUEFEN], "pruefen")
    files += write([f for f in cand_fs if objects(f)], "kandidaten")
    files += write([f for f in cand_fs if not objects(f)], "kandidaten_ohne_objekte")
    files += export.write_qs(wd_findings, "P7704", "fix", date, tag=tag)
    files += export.write_qs(wd_findings, "P7704", "delete", date, tag=tag)

    def target(f, stage):
        if f.action == "new" and f.qid:
            if stage == 1:
                return {FEHLT: "neu", OHNE_OBJEKTE: "ohne_objekte", O.NAME_PRUEFEN: "pruefen (#)"}.get(f.status, "")
            return ("kandidaten" if objects(f) else "kandidaten_ohne_objekte") + \
                (" (#)" if f.status == O.KANDIDAT_SCHWACH else "")
        return {"fix": "korrektur", "delete": "loeschen"}.get(f.action or "", "")

    rows = []
    for stage, fs in ((1, stage1), (2, stage2), (0, wd_findings)):
        for f in fs:
            o = orgs.get((f.source_id or "").split(",")[0])
            rows.append({
                "stufe": {1: "sameAs", 2: "kandidat", 0: "wikidata"}[stage], "land": (o.country or "") if o else "",
                "europeana_id": f.source_id or "", "europeana_label": o.label if o else "",
                "objekte": o.record_count if o else "", "homepage": " ".join(o.homepages) if o else "",
                "europeana_url": o.url if o else "", "qid": f.qid or "", "wikidata_label": labels.get(f.qid, ""),
                "status": f.status, "vorschlag": f.source_value or "",
                "wikidata": f.old_value or "|".join(f.wikidata_values), "datei": target(f, stage), "hinweis": f.note,
            })
    export.write_org_report(rows, export.out_path(date, "organisationen", ".csv", "P7704"))

    def count(fs):
        return dict(Counter(f.status for f in fs).most_common())

    new_by_country: dict[str, Counter] = defaultdict(Counter)
    for f in stage1 + cand_fs:
        if f.action == "new" and f.qid:
            o = orgs.get(f.source_id)
            new_by_country[(o.country if o else None) or "–"][f.status] += 1
    summary = {
        "date": date, "orgs": len(orgs), "with_qid": len(orgs) - len(no_qid), "multi_qid": len(multi),
        "wikidata_values": sum(len(q) for q in value_owner.values()), "candidates": candidates,
        "no_qid": len(no_qid), "no_qid_searched": len(todo), "with_candidates": len(cands),
        "stage1": count(stage1), "stage2": count(stage2),
        "wikidata": dict(Counter(f"{f.status}/{f.action or 'bericht'}" for f in wd_findings).most_common()),
        "by_country": {k: dict(v) for k, v in sorted(new_by_country.items(), key=lambda kv: -sum(kv[1].values()))},
        "files": [p.name for p in files], "classes": len(known),
    }
    export.write_org_summary(summary, export.out_path(date, "organisationen", ".md", "P7704"))
    log.info("Neu-Vorschläge mit veraltetem Wert: %d", sum(1 for f in stage1 + stage2 if f.status == ABGELEHNT))
    return summary


# --- GND-Dump: Körperschaften ↔ Wikidata (Kandidaten nach Name + Ort, Mix'n'match) --------
GDR = "Q16957"  # Deutsche Demokratische Republik: Pool-Items mit P17 = DDR zählen bei Q183 mit


def gnd_table(rebuild: bool = False) -> dict:
    """Stellt die lokale GND-Tabelle bereit (baut sie bei Bedarf aus dem Dump)."""
    from .sources import gnd as gnd_src

    dump = config.gnd_dump_path()
    if not dump or not dump.is_file():
        raise SystemExit("GND-Dump nicht gefunden: GND_DUMP_PATH in .env setzen (siehe README, „GND-Dump Körperschaften“).")
    return gnd_src.ensure_table(dump, config.GND_TABLE, rebuild)


def gnd_redirects(rebuild: bool = False) -> tuple[dict, dict]:
    """Umlenkungen/Löschungen (GND_REDIRECTS_PATH) → (alte GND → Redirect, Angaben zur Datei).

    Fehlt die Datei, läuft der Abgleich ohne diese Prüfung weiter (mit Warnung und Hinweis im Bericht).
    """
    from .sources import gnd_redirects as rd

    path = config.gnd_redirects_path()
    if not path or not path.is_file():
        log.warning("GND-Umlenkungsdatei nicht gefunden (GND_REDIRECTS_PATH, siehe README) – Prüfung entfällt.")
        return {}, {}
    stats = rd.ensure(path, config.GND_TABLE, rebuild)
    return rd.load(config.GND_TABLE), {"file": path.name, "date": rd.file_date(path), "stats": stats}


def gnd_compare(client: CachedClient, country: str = "Q183", types: list[str] | None = None,
                subclasses: bool = True, limit: int | None = None, rebuild: bool = False,
                include_historical: bool = False, propose_dates: bool = False) -> dict:
    from . import gnd as g
    from . import registry as reg
    from .sources import gnd as gnd_src
    from .sources.gnd_redirects import file_date, resolve

    if country not in gnd_src.COUNTRY_GROUPS:
        raise SystemExit(f"--country für die GND: {', '.join(gnd_src.COUNTRY_GROUPS)}")
    types = types or list(TYPES)
    wd = Wikidata(client)
    date = export.today()

    # 1. GND-Tabelle (aus dem Dump) und Umlenkungen laden, nach Land und Typ filtern
    dump_stats = gnd_table(rebuild)
    redirects, rd_info = gnd_redirects(rebuild)
    meta = gnd_src.table_meta(config.GND_TABLE)
    bodies = [b for b in gnd_src.load_table(config.GND_TABLE, gnd_src.COUNTRY_GROUPS[country])
              if set(b.types) & set(types)]
    total = len(bodies)
    if limit:
        bodies = bodies[:limit]
    log.info("GND: %d Einrichtungen (%s)%s", len(bodies), country, f", Trockenlauf von {total}" if limit else "")

    # 2. Wikidata: P227 (aktuelle und alte GND) und Kandidaten-Pool (Land × Typen, bei Q183 auch DDR)
    values = {b.gnd for b in bodies} | {o for b in bodies for o in b.old_ids}
    value_index = {v: set(qs) for v, qs in wd.lookup("P227", values).items()}
    items: dict[str, WikidataItem] = {}
    for c in [country] + ([GDR] if country == "Q183" else []):
        for t in types:
            name = f"wikidata_{c}_{t}{'_sub' if subclasses else ''}"
            d = _load(name) or fetch_wikidata(client, c, [t], subclasses)[t]
            items.update(_items_from_json(d))
    extras = wd.fetch_extras(items)
    pool = reg.Pool(dict(items), extras)
    gp = g.GndPool(pool, wd.locations(items))
    item_values = {q: [s.value for s in it.values("P227")] for q, it in items.items() if it.values("P227")}

    # 3. Abgleich
    matches = g.classify_gnd(bodies, gp, value_index, item_values, redirects, include_historical)
    log.info("GND: %d sichere Kandidaten (Ort gleich, Label sehr ähnlich) → P227-QuickStatements",
             g.mark_sure(matches, gp, item_values))
    fmt_findings = g.p227_format_findings(items)

    # 4. Zeitraum: Anmerkungen bei vorhandenen GND-IDs (P571/P576 live)
    present = {q for m in matches if m.status == g.VORHANDEN for q in m.qids}
    wd_years = wd.years(present) if present else {}
    annotations = {m.body.gnd: g.period_annotation(m.body, *wd_years.get(m.qids[0], (set(), set()))[:2])
                   for m in matches if m.status == g.VORHANDEN and m.qids}
    dates: list = []
    if propose_dates:  # Zeitraum als Qualifikator P580/P582 an der P227-Aussage
        pairs = {(m.qids[0], m.body.gnd) for m in matches if m.status == g.VORHANDEN and len(m.qids) == 1}
        dates = g.qualifier_proposals(matches, wd.p227_qualifiers(pairs), int(date[:4]))

    # 5. P227-Werte auf umgelenkte/gelöschte GND: Werte im Pool und alte IDs, deren Endziel hier liegt
    redirect_findings: list = []
    if redirects:
        gnds = {b.gnd for b in bodies}
        scope = {v for vs in item_values.values() for v in vs}
        scope |= {o for o in redirects if resolve(redirects, o)[0] in gnds}
        statements = wd.p227_statements(scope & set(redirects))
        targets = {resolve(redirects, st["value"])[0] for st in statements} - {None}
        owners = wd.lookup("P227", targets) if targets else {}
        redirect_findings = g.p227_redirect_findings(statements, redirects, owners)
    outside = ({q for m in matches for q in m.qids} | {f.qid for f in redirect_findings}) - set(items)
    items.update(wd.fetch_items(outside))

    # 6. Ausgabe
    out = config.OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    stem = export.out_path(date, f"register_gnd_{country}", "", "P227").name
    export.write_gnd_report(matches, items, out / f"{stem}.csv", annotations)
    export.write_gnd_format(fmt_findings, items, out / f"{stem}_formatfehler.csv")
    export.write_gnd_redirects(redirect_findings, items, out / f"{stem}_umlenkungen.csv")
    tag = f"gnd_{country}"
    qs_files = export.write_qs_groups(g.p227_commands(matches, date), "P227", tag, "neu", date)
    qs_files += export.write_qs_groups(g.redirect_commands(redirect_findings, date), "P227", tag, "korrektur", date)
    qs_files += export.write_qs_groups(g.qualifier_commands(dates), "P227", tag, "zeitraum", date)
    _save(f"gnd_results_{country}_{date}", [m.to_dict() for m in matches])
    mnm_files = export_mnm(country, date, include_historical)
    counts = Counter(m.status for m in matches)
    by_group = Counter((t, m.body.country) for m in matches for t in m.body.types if t in types)
    linked = Counter((t, m.body.country) for m in matches for t in m.body.types if t in types and m.body.wikidata)
    summary = {
        "date": date, "country": country, "types": types, "subclasses": subclasses, "limit": limit,
        "total": total, "records": len(bodies), "pool": len(pool.items), "counts": counts,
        "statuses": g.STATUSES, "dump": meta.get("dump"), "dump_date": file_date(config.gnd_dump_path()),
        "built": meta.get("built"), "dump_stats": dump_stats,
        "groups": {f"{t}/{c}": [by_group[(t, c)], linked[(t, c)]] for (t, c) in sorted(by_group)},
        "mnm": {p.name: n for p, n in mnm_files}, "format": len(fmt_findings),
        "in_wikidata": sum(m.in_wikidata for m in matches),
        "include_historical": include_historical, "propose_dates": propose_dates,
        "period": {
            "historisch": sum(m.body.period_status == "historisch" for m in matches),
            "vorgaenger": sum(m.body.period_status == "vorgaenger" for m in matches),
            "annotations": Counter(n for a in annotations.values() for n in a["anmerkung"].split(", ") if n),
            "dates": {"P580": sum(bool(d.start) for d in dates), "P582": sum(bool(d.end) for d in dates),
                      "statements": len(dates)},
        },
        "redirects": {**rd_info, "findings": Counter((f.status, f.action) for f in redirect_findings)},
        "qs": [p.name for p in qs_files],
    }
    export.write_gnd_summary(summary, out / f"{stem}.md")
    return summary


def _latest_gnd_results(country: str, date: str | None) -> tuple[str, list[dict]]:
    if date:
        d = _load(f"gnd_results_{country}_{date}")
        if d is None:
            raise SystemExit(f"Kein GND-Abgleich vom {date} für {country} (data/gnd_results_{country}_{date}.json).")
        return date, d
    files = sorted(config.DATA_DIR.glob(f"gnd_results_{country}_*.json"))
    if not files:
        raise SystemExit(f"Noch kein GND-Abgleich für {country}: zuerst `registry --source gnd` ausführen.")
    return files[-1].stem.rsplit("_", 1)[-1], json.loads(files[-1].read_text(encoding="utf-8"))


def export_mnm(country: str = "Q183", date: str | None = None,
               include_historical: bool = False) -> list[tuple[Path, int]]:
    """Mix'n'match-CSV je Typ und Land aus dem letzten GND-Abgleich (nur GND ohne P227 in Wikidata).

    Historische Einrichtungen und frühere Namensformen nur mit include_historical.
    """
    from . import gnd as g
    from .sources.gnd import COUNTRY_GROUPS

    date, rows = _latest_gnd_results(country, date)
    matches = [g.GndMatch.from_dict(d) for d in rows]
    return export.write_mnm(g.mnm_rows(matches, include_historical), date, lands=COUNTRY_GROUPS[country])


def gnd_labels(client: CachedClient, country: str = "Q183", rebuild: bool = False) -> dict:
    """Deutsche Labels, Beschreibungen und Aliase aus der GND für Items mit passender P227.

    Liest Wikidata live (Cache-Dauer: GLAMSYNC_WIKIDATA_CACHE_HOURS) und schreibt nur Dateien:
    QuickStatements je Art (Lde/Dde/Ade) und einen Bericht. Vorhandenes wird nie überschrieben.
    """
    from . import gnd as g
    from .sources import gnd as gnd_src

    if country not in gnd_src.COUNTRY_GROUPS:
        raise SystemExit(f"--country für die GND: {', '.join(gnd_src.COUNTRY_GROUPS)}")
    date = export.today()
    gnd_table(rebuild)
    bodies = {b.gnd: b for b in gnd_src.load_table(config.GND_TABLE, gnd_src.COUNTRY_GROUPS[country])}
    terms = Wikidata(client).gnd_terms(bodies)
    log.info("GND-Bezeichnungen: %d Items mit P227 aus %d Einrichtungen", len(terms), len(bodies))
    proposals = g.term_proposals(terms, bodies)

    out = config.OUT_DIR
    tag = f"gnd_{country}"
    report = export.out_path(date, f"bezeichnungen_{tag}", ".csv")
    export.write_terms_report(proposals, report)
    files, counts = [], {}
    for kind in export.TERM_KINDS:
        groups = g.term_commands(proposals, kind)
        counts[kind] = {"items": len(groups), "lines": sum(len(x) for x in groups)}
        files += export.write_qs_groups(groups, kind, tag, "neu", date)
    return {"date": date, "country": country, "items": len(terms), "counts": counts,
            "notes": sum(bool(p.note) for p in proposals), "report": report.name,
            "files": [p.name for p in files]}


def wikidata_labels(client: CachedClient, country: str = "europa", types: list[str] | None = None) -> dict:
    """mul- und englische Labels für Museen, Bibliotheken und Archive (P31/P279*) ohne mul- und
    ohne en-Label, je Staat abgefragt (siehe glamsync/labels.py).

    Labels und Aliase live über die API; englische Namen aus P1448 und – für Items mit
    Europeana-Organisation (P7704 in Wikidata, sonst einzige sameAs-QID) – aus Europeana.
    Schreibt nur Dateien.
    """
    from collections import defaultdict

    from . import labels as L
    from . import orgs as O

    wd = Wikidata(client, patient=True)
    date = export.today()
    types = types or list(TYPES)
    if country.lower() in ("europa", "europe"):
        countries = wd.european_countries()
        tag = "europa"
    else:
        countries = {c.strip(): c.strip() for c in country.split(",")}
        tag = country.replace(",", "-")

    # 1. Pool je Staat und Typ
    item_countries: dict[str, set[str]] = defaultdict(set)
    failed = []
    for c, name in countries.items():
        for t in types:
            try:
                found = wd.without_en_mul(c, TYPES[t])
            except Exception as exc:  # z. B. SPARQL-Timeout
                log.warning("%s/%s nicht abrufbar: %s", name, t, exc)
                failed.append(f"{name} ({c}) {t}")
                continue
            for q in found:
                item_countries[q].add(c)
            log.info("Wikidata %s/%s: %d Items ohne en- und mul-Label", name, t, len(found))
    log.info("Pool: %d Items", len(item_countries))

    # 2. Bezeichnungen live, Amtssprachen, offizielle englische Namen
    terms = wd.terms(item_countries)
    official = wd.country_languages(countries)
    off_en = wd.official_names(terms)
    items = {q: {**t, "countries": sorted(item_countries[q]), "official_en": off_en.get(q, [])}
             for q, t in terms.items()}

    # 3. Europeana-Verknüpfungen (für englische Namen)
    orgs = {o.num: o for o in (O.org_from_raw(r) for r in europeana.fetch_all_raw(client))}
    owner: dict[str, set[str]] = defaultdict(set)
    for r in wd.p7704_non_agent():
        v = normalize("P7704", r["value"])
        if v and v.startswith("organisation/") and "deprecated" not in r["rank"].lower():
            owner[v].add(r["qid"])
    redirects = wd.resolve_redirects({q for o in orgs.values() for q in o.qids})
    linked: dict[str, list] = defaultdict(list)
    for o in orgs.values():
        qs = owner.get(o.value, set())
        same = sorted({redirects.get(q, q) for q in o.qids})
        q = next(iter(qs)) if len(qs) == 1 else (same[0] if not qs and len(same) == 1 else None)
        if q in items:
            linked[q].append(o)

    proposals = L.label_proposals(items, official, linked)

    # 4. Ausgabe
    out = config.OUT_DIR
    report = export.out_path(date, f"bezeichnungen_{tag}", ".csv")
    export.write_label_report(proposals, report, countries)
    files, counts = [], {}
    for kind in ("Lmul", "Len"):
        groups = L.label_commands(proposals, kind)
        counts[kind] = {"items": len(groups), "lines": sum(len(x) for x in groups)}
        files += export.write_qs_groups(groups, kind, tag, "neu", date)
    return {"date": date, "items": len(items), "countries": len(countries), "failed": failed,
            "counts": counts, "notes": sum(bool(p.note) for p in proposals), "report": report.name,
            "files": [p.name for p in files], "en_sources": Counter(p.en_source for p in proposals if p.en)}


# --- Anreicherung aus der GND (P214, P213, P571, P131, P31) -----------------------------
def gnd_enrich(client: CachedClient, country: str = "Q183") -> dict:
    """QuickStatements für Items mit GND (Pool des Landes): fehlende VIAF/ISNI/Gründung/Ort und
    genauere Art (P31) aus dem GND-Satz. Siehe glamsync/enrich.py."""
    from . import enrich as E
    from .sources import gnd as gnd_src

    wd = Wikidata(client)
    date = export.today()
    by_gnd = {b.gnd: b for b in gnd_src.load_table(config.GND_TABLE, gnd_src.COUNTRY_GROUPS[country])}

    # 1. Pool des Landes (Museum/Bibliothek/Archiv, bei Q183 auch DDR), Aussagen live über die API
    qids: set[str] = set()
    for c in [country] + ([GDR] if country == "Q183" else []):
        for t in TYPES:
            # nur die QIDs werden gebraucht (Aussagen kommen live über die API) → auch ältere Datei
            path = _data_path(f"wikidata_{c}_{t}_sub")
            d = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else fetch_wikidata(client, c, [t], True)[t]
            qids |= set(d)
    log.info("Pool %s: %d Items – lade Aussagen über die Wikidata-API", country, len(qids))
    ents = wd.entity_claims(qids, E.PROPS)
    pairs = []
    for q, e in ents.items():
        gnds = [s["value"] for s in e["claims"].get("P227", []) if s["rank"] != "deprecated" and s["value"] in by_gnd]
        if len(gnds) == 1:
            pairs.append((q, e, by_gnd[gnds[0]]))
    log.info("Items mit genau einer GND aus der Tabelle: %d", len(pairs))

    # 2. VIAF/ISNI: in ganz Wikidata schon vergeben?
    cand = {"P214": set(), "P213": set()}
    for q, e, b in pairs:
        for prop, raw in (("P214", b.viaf[:1]), ("P213", b.isni[:1])):
            if raw and not E._values(e, prop) and (v := normalize(prop, raw[0])):
                cand[prop].add(v)
    owners = {p: wd.lookup(p, vs) if vs else {} for p, vs in cand.items()}

    # 3. Ortsteile der GND-Orte (nur für P131-Kandidaten)
    pc = {p["qid"] for q, e, b in pairs if not E._values(e, "P131")
          for p in b.places if p.get("qid") and len({x.get("qid") for x in b.places if x.get("qid")}) == 1}
    districts = wd.settlement_labels(pc) if pc else {}

    # 4. P31: Zuordnungstabelle Oberbegriff → Klasse (neue Begriffe als Entwurf ergänzen)
    map_path = config.DATA_DIR / "gnd_oberbegriff_p31.csv"
    class_map, class_root, rows = E.load_class_map(map_path)
    generic_items = [(q, e, b) for q, e, b in pairs
                     if (v := E._values(e, "P31")) and all(x in E.GENERIC for x in v) and b.broader]
    need: dict[str, int] = Counter(t for _, _, b in generic_items for t in b.broader if t not in E.GENERIC.values())
    new_terms = [t for t in need if not rows.get(t, {}).get("qid")]
    found = wd.class_by_label(new_terms, E.GENERIC) if new_terms else {}
    for t, n in need.items():
        r = rows.setdefault(t, {"oberbegriff": t, "qid": "", "wurzel": "", "alternativen": "", "freigegeben": ""})
        r[f"anzahl_{country}"] = str(n)
        r["anzahl"] = str(sum(int(r.get(f"anzahl_{c}") or 0) for c in ("Q183", "Q40", "Q39")))
        if t in found and not r["qid"]:
            hits = sorted(set(found[t]))
            r["qid"], r["wurzel"] = hits[0]
            r["alternativen"] = ", ".join(f"{c} ({w})" for c, w in hits[1:])
    E.write_class_map(map_path, rows)

    # 5. Vorschläge
    props: list = []
    for q, e, b in pairs:
        props += E.id_proposals(q, e, b, owners)
        props += [x for x in (E.inception_proposal(q, e, b), E.place_proposal(q, e, b, districts),
                              E.class_proposal(q, e, b, class_map, class_root)) if x]

    # 6. Ausgabe
    out = config.OUT_DIR
    tag = f"gnd_{country}"
    files = []
    for prop, label in (("P214", "neu"), ("P213", "neu"), ("P571", "neu"), ("P131", "neu"), ("P31", "ersetzen")):
        groups = [x.commands(date) for x in props if x.prop == prop and x.action == "neu"]
        files += export.write_qs_groups(groups, prop, tag, label, date)
    report = export.out_path(date, f"anreicherung_{tag}", ".csv")
    with report.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["qid", "label", "gnd", "property", "wert", "entfernt", "aktion", "hinweis"])
        lab = {q: (e["labels"].get("de") or e["labels"].get("mul") or next(iter(e["labels"].values()), "")) for q, e in ents.items()}
        gnd_of = {q: b.gnd for q, _, b in pairs}
        for x in props:
            w.writerow([x.qid, lab.get(x.qid, ""), gnd_of.get(x.qid, ""), x.prop, x.value, x.remove, x.action, x.note])
    counts = Counter((x.prop, x.action) for x in props)
    return {"date": date, "country": country, "items": len(pairs), "counts": counts,
            "files": [p.name for p in files], "report": report.name, "map": str(map_path),
            "map_open": sum(1 for t in need if t not in class_map)}


# --- Websites (P856) aus ISIL-Register, GND, Museen in Österreich, Europeana, DDB ---------------
WEBSITE_COUNTRIES = ("Q183", "Q40", "Q39")


def websites_compare(client: CachedClient, country: str = "Q183", types: list[str] | None = None,
                     limit: int | None = None) -> dict:
    """P856 für Pool-Items ohne Website: Adressen aus Quellen, die über eine Kennung am Item hängen,
    werden abgerufen und geprüft (siehe glamsync/websites.py). Schreibt nur Dateien."""
    from collections import defaultdict

    from . import orgs as O
    from . import websites as W
    from .registry import norm_url
    from .sources import gnd as gnd_src
    from .sources import linkcheck
    from .sources import registry as reg_src

    if country not in WEBSITE_COUNTRIES:
        raise SystemExit(f"websites: nur {', '.join(WEBSITE_COUNTRIES)} (Quellen nur für DE/AT/CH).")
    wd = Wikidata(client, patient=True)
    date = export.today()
    types = types or list(TYPES)

    # 1. Pool des Landes (bei Q183 auch DDR); Aussagen live über die API
    qids: set[str] = set()
    for c in [country] + ([GDR] if country == "Q183" else []):
        for t in types:
            path = _data_path(f"wikidata_{c}_{t}_sub")
            d = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else fetch_wikidata(client, c, [t], True)[t]
            qids |= set(d)
    log.info("Pool %s: %d Items – lade Aussagen über die Wikidata-API", country, len(qids))
    ents = wd.entity_claims(qids, W.PROPS)

    # 2. Quellen
    gnd_by = {b.gnd: b for b in gnd_src.load_table(config.GND_TABLE)} if config.GND_TABLE.is_file() else {}
    if not gnd_by:
        log.warning("GND-Tabelle fehlt (%s) – GND-Homepages werden nicht berücksichtigt", config.GND_TABLE)
    isil_recs = reg_src.fetch_isil_ch(client) if country == "Q39" else reg_src.fetch_isil(client, ISIL_PREFIX[country])
    isil_by = {r.value.upper(): r for r in isil_recs}
    mure_by = {r.value.upper(): r for r in reg_src.fetch_mure(client)} if country == "Q40" else {}
    orgs = {o.value: o for o in (O.org_from_raw(x) for x in europeana.fetch_all_raw(client))}
    ddb_isil: dict[str, list] = defaultdict(list)
    ddb_gnd: dict[str, list] = defaultdict(list)
    if country == "Q183":
        recs = [SourceRecord(**r) for r in (_load("ddb_archive-library-museum") or [])] or fetch_ddb(client, list(TYPES), None)
        for r in recs:
            web = ddb.fetch_website(client, r.source_id)
            if not web:
                continue
            s = W.SourceUrl("ddb", r.source_id, web, r.url, r.label)
            for v in r.ids.get("P791", []):
                ddb_isil[v.upper()].append(s)
            for v in r.ids.get("P227", []):
                ddb_gnd[v].append(s)
        log.info("DDB: %d Institutionen, Website über ISIL %d / GND %d", len(recs), len(ddb_isil), len(ddb_gnd))

    # 3. Items ohne gültige P856 mit mindestens einer Quell-Adresse
    def vals(cl: dict, prop: str) -> list[str]:
        return [s["value"] for s in cl.get(prop, []) if s["rank"] != "deprecated" and isinstance(s["value"], str)]

    with_p856 = 0
    infos: list = []
    for q in sorted(ents, key=lambda x: int(x[1:])):
        e = ents[q]
        cl = e["claims"]
        if vals(cl, "P856"):
            with_p856 += 1
            continue
        labels = e["labels"]
        info = W.ItemInfo(q, labels.get("de") or labels.get("mul") or labels.get("en") or next(iter(labels.values()), ""),
                          names=list(labels.values()))
        hist = []
        for g in vals(cl, "P227"):
            b = gnd_by.get(g)
            if b:
                hist.append(b.historical)
                info.names += [b.name, *b.variants]
                info.places += b.place_names
                info.sources += [W.SourceUrl("gnd", b.gnd, u, f"https://d-nb.info/gnd/{b.gnd}", b.name) for u in b.homepages]
            info.sources += ddb_gnd.get(g, [])
        for v in vals(cl, "P791"):
            r = isil_by.get(v.upper())
            if r:
                info.names += [r.name, *r.alt_names]
                info.places += [r.city] if r.city else []
                info.sources += [W.SourceUrl(r.source, r.value, u, r.url, r.name) for u in r.websites]
            info.sources += ddb_isil.get(v.upper(), [])
        for v in vals(cl, "P7917"):
            r = mure_by.get(v.upper())
            if r:
                info.names.append(r.name)
                info.places += [r.city] if r.city else []
                info.sources += [W.SourceUrl("mure", r.value, u, r.url, r.name) for u in r.websites]
        for v in vals(cl, "P7704"):
            o = orgs.get(normalize("P7704", v) or v)
            if o:
                info.names += o.names
                info.sources += [W.SourceUrl("europeana", o.value, u, o.url, o.label) for u in o.homepages]
        uniq = {}
        for s in info.sources:
            uniq.setdefault((s.source, s.url.strip()), s)
        info.sources = list(uniq.values())
        if not info.sources:
            continue
        if vals(cl, "P576"):
            info.dissolved = "P576 (aufgelöst) am Item"
        elif hist and all(hist):
            info.dissolved = "GND: aufgelöst (ohne Nachfolger)"
        info.deprecated = [s["value"] for s in cl.get("P856", []) if s["rank"] == "deprecated"]
        infos.append(info)
    log.info("Items ohne P856: %d, davon mit Quell-Adresse: %d", len(ents) - with_p856, len(infos))
    if limit:
        infos = infos[:limit]
        log.info("Trockenlauf: nur die ersten %d Items", len(infos))

    # 4. Linkprüfung
    urls = {s.url.strip() for i in infos for s in i.sources if not W.url_problem(s.url)}
    checks = linkcheck.check_urls(urls, use_cache=client.use_cache)

    # 5. P856 an anderen Items (ganz Wikidata), alle Schreibweisen von Quell- und Zieladresse
    forms = set(urls) | {W.proposed_value(c.url, c.final) for c in checks.values() if c.final}
    variants: set[str] = set()
    for u in forms:
        variants |= O.website_variants(u)[0]
    owners: dict[str, set[str]] = defaultdict(set)
    for iri, qs in wd.by_website(variants).items():
        owners[norm_url(iri)] |= set(qs)
    shared: dict[str, set[str]] = defaultdict(set)
    for i in infos:
        for s in i.sources:
            shared[norm_url(s.url)].add(i.qid)

    # 6. Klassifikation
    proposals = [W.classify_item(i, checks, owners, shared, country) for i in infos]
    collisions = W.resolve_collisions(proposals)

    # 7. Ausgabe
    out = config.OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for status in (W.NEU, W.PRUEFEN):
        groups = [W.commands(p, date) for p in proposals if p.status == status]
        files += export.write_qs_groups(groups, "P856", country, status, date)
    report = export.out_path(date, f"webseiten_{country}", ".csv", "P856")
    export.write_website_report(proposals, report)
    states = Counter(f.state for p in proposals for f in p.urls)
    summary = {
        "date": date, "country": country, "pool": len(ents), "with_p856": with_p856,
        "without_p856": len(ents) - with_p856, "items": len(infos), "urls": len(urls),
        "status": {s: n for s in W.STATUSES if (n := sum(p.status == s for p in proposals))},
        "url_states": dict(states.most_common()),
        "sources": dict(Counter(s.source for i in infos for s in i.sources).most_common()),
        "first_source": {st: dict(Counter(p.sources[0].source for p in proposals if p.status == st and p.sources))
                         for st in (W.NEU, W.PRUEFEN)},
        "multi_source": sum(1 for p in proposals if p.status in (W.NEU, W.PRUEFEN) and len(p.sources) > 1),
        "evidence": dict(Counter(p.evidence or "–" for p in proposals if p.status in (W.NEU, W.PRUEFEN))),
        "p407": dict(Counter(p.lang or "ohne" for p in proposals if p.status in (W.NEU, W.PRUEFEN))),
        "collisions": collisions, "files": [p.name for p in files], "report": report.name,
    }
    export.write_website_summary(summary, export.out_path(date, f"webseiten_{country}", ".md", "P856"))
    return summary
