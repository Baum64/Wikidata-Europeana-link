"""Ablaufsteuerung für fetch / compare / export-qs."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from . import config, export
from .http import CachedClient
from .match import (
    RecordMatch,
    build_lookups,
    classify_record,
    decide_qid,
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
        if not r["rank"].lower().startswith("deprecated"):
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
    export.write_report(match_rows, wd_findings, out / f"report_{date}.csv")

    unmatched = [m.record for m in matches if not m.qid]
    pool = {q: it for q, it in items.items()}
    cands = name_candidates(unmatched, pool)
    export.write_candidates(cands, out / f"kandidaten_namen_{date}.csv")

    results = {
        "date": date, "country": country, "types": types, "limit": limit, "subclasses": subclasses,
        "matches": match_rows, "wikidata_findings": [f.to_dict() for f in wd_findings],
    }
    results_path = out / f"results_{date}.json"
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = build_summary(results, base, notes)
    summary["candidates"] = len(cands)
    export.write_summary(summary, out / f"summary_{date}.md")
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
    path = config.OUT_DIR / f"results_{date}.json"
    if not path.is_file():
        candidates = sorted(config.OUT_DIR.glob("results_*.json"))
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
        records = reg_src.fetch_isil(client, prefix)
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
    registry_keys = {compare_key(prop, r.value) for r in records}
    cleanup = reg.registry_cleanup(statements, registry_keys, prop, value_prefix)
    # Labels für Items außerhalb des Pools nachladen (für Bericht)
    outside = {q for m in matches for q in m.qids} | {c.qid for c in cleanup}
    items.update(wd.fetch_items(outside - set(items)))

    # 4. Ausgabe
    out = config.OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    stem = f"register_{source}_{country}_{date}"
    export.write_registry_report(matches, items, out / f"{stem}.csv")
    export.write_registry_cleanup(cleanup, items, prop, out / f"{stem}_bereinigung.csv")
    qs_files = export.write_registry_qs(matches, prop, source, country, date)
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


# --- Städte: Europeana-Orte ↔ Wikidata (P7704 place/base/<n>) ----------------------
BIG_CITY = "Q1549591"  # Großstadt


def cities_compare(client: CachedClient, country: str = "europa", city_class: str = BIG_CITY,
                   subclasses: bool = True, min_population: int = 100_000) -> dict:
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
    # Formatprüfung bereits vorhandener P7704-Werte der Städte (Korrektur/Löschung)
    already = {(f.qid, f.prop, f.old_value) for f in findings if f.old_value}
    wd_findings = [f for f in wikidata_format_findings(items, set(), already) if f.prop == "P7704"]

    # 5. Ausgabe
    tag = f"staedte_{tag_country}"
    out = config.OUT_DIR
    files = []
    for action in ("new", "fix", "delete"):
        files += export.write_qs(findings + wd_findings, "P7704", action, date, tag=tag)

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
    export.write_city_report(rows, out / f"staedte_{tag_country}_{date}.csv")

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
    export.write_city_summary(summary, out / f"staedte_{tag_country}_{date}.md")
    return summary


# --- Personen: Europeana-Agents ↔ Wikidata (P7704 agent/base/<n>, P227) -------------
def persons_compare(client: CachedClient, check_gnd: bool = True) -> dict:
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

    # 4. Detaildaten: Items ohne agent-Wert und – für P227 – zugeordnete Items ohne GND
    gap = {q for q in groups if not present.get(q)}
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

    # 8. P227 aus Europeana, geprüft über lobid
    gnd_problems: dict[str, str | None] = {}
    todo = {r.ids["P227"][0] for q, r in matched.items()
            if q in items and len(r.ids["P227"]) == 1 and not items[q].values("P227")
            and "Q5" in items[q].p31 and not (gnd_owner.get(r.ids["P227"][0], set()) - {q})}
    if check_gnd:
        log.info("lobid: prüfe %d GND", len(todo))
        for g in sorted(todo):
            gnd_problems[g] = gnd_problem(g, gnd_info(client, g))
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
        c = P.agent_cleanup(q, v, agent_src.fetch_agent(client, v.rsplit("/", 1)[-1]),
                            bool(st and st.has_refs), bool(st and st.has_qualifiers), value_owner)
        if c:
            c.old_raw = st.value if st else v  # Rohwert, wie er in Wikidata steht
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
    files += export.write_qs(findings, "P7704", "new", date, tag="personen")
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
    export.write_person_report(rows, out / f"personen_{date}.csv")

    summary = {
        "date": date, "agents": len(recs), "wikidata_values": len(scan), "items": len(groups),
        "p7704": dict(Counter(f.status for f in findings).most_common()),
        "p227": dict(Counter(f.status for f in gnd_findings).most_common()),
        "cleanup": dict(Counter(f"{c.kind}/{c.action or 'bericht'}" for c in cleanups).most_common()),
        "gnd_checked": len(gnd_problems), "files": [p.name for p in files],
    }
    export.write_person_summary(summary, out / f"personen_{date}.md")
    return summary
