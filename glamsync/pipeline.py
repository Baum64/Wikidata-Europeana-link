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
from .models import FEHLT, FORMATFEHLER, IDENTISCH, KONFLIKT, MEHRDEUTIG, Finding, SourceRecord, Statement, WikidataItem
from .normalize import PROPERTIES, normalize
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
    p = _data_path(name)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


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
            subclasses: bool = False, sources: tuple[str, ...] = ("europeana", "ddb")) -> dict:
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
        "date": date, "country": country, "types": types, "limit": limit,
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
        for action in ("new", "fix"):
            groups = export.qs_commands(all_findings, prop, action, results["date"])
            if groups:
                qs.append({"prop": prop, "action": action, "commands": len(groups),
                           "files": len(export.split_parts(groups))})

    return {
        "date": results["date"], "country": results["country"], "types": results["types"],
        "limit": results["limit"], "base": base_rows, "matching": matching,
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
    actions = ["new", "fix"] if action == "all" else [action]
    written = []
    for p in props:
        for a in actions:
            written += export.write_qs(findings, p, a, results["date"])
    return written
