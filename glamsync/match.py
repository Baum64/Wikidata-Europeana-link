"""Abgleichslogik (ohne Netzzugriff, damit testbar).

Schlüsselreihenfolge für die Zuordnung Einrichtung → Wikidata-Item:
  1. Europeana sameAs → QID
  2. ISIL (P791) exakt (Groß-/Kleinschreibung egal)
  3. GND (P227) exakt
Kein automatischer Namensabgleich; Namensähnlichkeiten nur als Kandidatenliste.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .models import (
    FEHLT,
    FORMATFEHLER,
    IDENTISCH,
    KEIN_TREFFER,
    KONFLIKT,
    MEHRDEUTIG,
    Finding,
    SourceRecord,
    WikidataItem,
)
from .normalize import PROPERTIES, check, compare_key, normalize

# Properties, die nur übernommen werden, wenn die Quelle genau einen Wert liefert
SINGLE_VALUE_ONLY = {"P214", "P213", "P7704"}

# Lookup-Tabellen: Property → Vergleichsschlüssel → QIDs, in denen der Wert vorkommt
Lookups = dict[str, dict[str, set[str]]]


def build_lookups(raw: dict[str, dict[str, list[str]]]) -> Lookups:
    """Wandelt Wikidata-Rückwärtssuchen (Rohwert → QIDs) in normalisierte Schlüssel um."""
    out: Lookups = {p: defaultdict(set) for p in PROPERTIES}
    for prop, mapping in raw.items():
        for value, qids in mapping.items():
            norm = normalize(prop, value) or value
            out[prop][compare_key(prop, norm)].update(qids)
    return out


@dataclass
class RecordMatch:
    record: SourceRecord
    qid: str | None
    via: str  # sameAs | ISIL | GND | ""
    status: str = ""  # "" | kein_treffer | mehrdeutig
    note: str = ""
    findings: list[Finding] = field(default_factory=list)


def _qids_for(rec: SourceRecord, prop: str, lookups: Lookups) -> set[str]:
    found: set[str] = set()
    for v in rec.ids.get(prop, []):
        found |= lookups.get(prop, {}).get(compare_key(prop, v), set())
    return found


def decide_qid(rec: SourceRecord, lookups: Lookups, redirects: dict[str, str]) -> RecordMatch:
    same_as = {redirects.get(q, q) for q in rec.qids}
    by_isil = _qids_for(rec, "P791", lookups)
    by_gnd = _qids_for(rec, "P227", lookups)

    def disagreement(qid: str) -> str:
        notes = []
        if by_isil and by_isil != {qid}:
            notes.append(f"ISIL zeigt auf {','.join(sorted(by_isil))}")
        if by_gnd and by_gnd != {qid}:
            notes.append(f"GND zeigt auf {','.join(sorted(by_gnd))}")
        return "; ".join(notes)

    for via, cands in (("sameAs", same_as), ("ISIL", by_isil), ("GND", by_gnd)):
        if len(cands) == 1:
            qid = next(iter(cands))
            return RecordMatch(rec, qid, via, note=disagreement(qid))
        if len(cands) > 1:
            return RecordMatch(
                rec, None, via, status=MEHRDEUTIG, note=f"{via} passt auf mehrere QIDs: {','.join(sorted(cands))}"
            )
    return RecordMatch(rec, None, "", status=KEIN_TREFFER)


def classify_property(
    rec: SourceRecord, qid: str, item: WikidataItem, prop: str, lookups: Lookups
) -> Finding | None:
    src_vals = rec.ids.get(prop) or []
    if not src_vals:
        return None
    src_keys = {compare_key(prop, v) for v in src_vals}
    stmts = item.values(prop)
    wd_raw = [s.value for s in stmts]
    f = Finding(prop, "", qid, rec.source, rec.source_id, "|".join(src_vals), wd_raw)

    # 1. identisch: korrekt formatierter Wert stimmt überein
    checks = [(s, check(prop, s.value)) for s in stmts]
    for s, c in checks:
        if c.normalized and compare_key(prop, c.normalized) in src_keys and c.format_ok:
            f.status, f.source_value = IDENTISCH, c.normalized
            return f

    # 2. formatfehler: Wert stimmt nach Normalisierung überein
    for s, c in checks:
        if c.normalized and compare_key(prop, c.normalized) in src_keys:
            f.status, f.source_value, f.old_value = FORMATFEHLER, c.normalized, s.value
            f.note = c.problem or ""
            if s.has_refs or s.has_qualifiers:
                f.note += "; alte Aussage hat Quellen/Qualifier – manuell prüfen"
            elif c.normalized == s.value:
                f.note += "; nicht automatisch korrigierbar"
            else:
                f.action = "fix"
            return f

    # 2b. P7704: veraltete (CRM-)ID dieser Organisation
    if prop == "P7704":
        legacy = {v for v in rec.legacy_p7704}
        for s, c in checks:
            if c.normalized in legacy:
                f.status, f.old_value, f.source_value = FORMATFEHLER, s.value, src_vals[0]
                f.note = "veraltete Europeana-ID; manuell durch aktuelle ersetzen"
                return f

    # Quelle mit mehreren Werten: nur Bericht
    if len(src_vals) > 1 and prop in SINGLE_VALUE_ONLY:
        f.status, f.note = MEHRDEUTIG, "Quelle liefert mehrere Werte"
        return f

    valid_wd = [c for _, c in checks if c.normalized]
    if not valid_wd:
        # 3. fehlt_in_wikidata – sofern der Wert nicht schon an einem anderen Item hängt
        value = src_vals[0]
        others = lookups.get(prop, {}).get(compare_key(prop, value), set()) - {qid}
        f.source_value = value
        if len(src_vals) > 1:
            f.status, f.note = MEHRDEUTIG, "Quelle liefert mehrere Werte"
        elif others:
            f.status, f.note = MEHRDEUTIG, f"Wert bereits an {','.join(sorted(others))}"
        else:
            f.status, f.action = FEHLT, "new"
            if wd_raw:
                f.note = f"vorhandene ungültige Werte: {wd_raw}"
        return f

    # 4. konflikt
    f.status = KONFLIKT
    f.note = "Wikidata hat abweichenden Wert"
    return f


def classify_record(m: RecordMatch, items: dict[str, WikidataItem], lookups: Lookups) -> None:
    if not m.qid:
        return
    item = items.get(m.qid)
    if item is None:
        m.note = (m.note + "; " if m.note else "") + "QID nicht abrufbar"
        return
    for prop in PROPERTIES:
        f = classify_property(m.record, m.qid, item, prop, lookups)
        if f:
            m.findings.append(f)


def resolve_collisions(findings: list[Finding]) -> None:
    """Neue Werte, die sich widersprechen, werden zu 'mehrdeutig' (nur Bericht).

    - mehrere unterschiedliche Werte für dieselbe (QID, Property)
    - derselbe Wert für mehrere QIDs
    """
    new = [f for f in findings if f.action == "new"]
    by_item: dict[tuple, set[str]] = defaultdict(set)
    by_value: dict[tuple, set[str]] = defaultdict(set)
    for f in new:
        key = compare_key(f.prop, f.source_value)
        by_item[(f.qid, f.prop)].add(key)
        by_value[(f.prop, key)].add(f.qid)
    for f in new:
        key = compare_key(f.prop, f.source_value)
        if len(by_item[(f.qid, f.prop)]) > 1:
            f.status, f.action = MEHRDEUTIG, None
            f.note = f"Quellen schlagen verschiedene Werte vor: {sorted(by_item[(f.qid, f.prop)])}"
        elif len(by_value[(f.prop, key)]) > 1:
            f.status, f.action = MEHRDEUTIG, None
            f.note = f"Wert wird für mehrere Items vorgeschlagen: {sorted(by_value[(f.prop, key)])}"


def wikidata_format_findings(
    items: dict[str, WikidataItem],
    europeana_org_ids: set[str],
    already: set[tuple[str, str, str]],
) -> list[Finding]:
    """Formatprüfung aller Wikidata-Werte unabhängig von den Quellen.

    `already` enthält (qid, prop, alter Wert), die schon aus dem Quellabgleich stammen.
    """
    out = []
    for qid, item in items.items():
        for prop in PROPERTIES:
            stmts = item.values(prop)
            present = {s.value for s in stmts}
            for s in stmts:
                c = check(prop, s.value)
                if c.format_ok or (qid, prop, s.value) in already:
                    continue
                f = Finding(prop, FORMATFEHLER, qid, "wikidata", "", c.normalized, sorted(present),
                            old_value=s.value, note=c.problem or "")
                if c.normalized is None:
                    # ungültige ID → Löschung vorschlagen (wie Korrekturen nur ohne Quellen/Qualifier)
                    if s.has_refs or s.has_qualifiers:
                        f.note += "; ungültig, aber Aussage hat Quellen/Qualifier – manuell prüfen"
                    else:
                        f.action = "delete"
                        f.note += "; ungültig – Löschung vorgeschlagen"
                        if "P727" in f.note:
                            f.note += " (Wert ggf. vorher nach P727 übertragen)"
                elif c.normalized == s.value:
                    f.note += "; nicht automatisch korrigierbar"
                elif s.has_refs or s.has_qualifiers:
                    f.note += "; alte Aussage hat Quellen/Qualifier – manuell prüfen"
                elif prop == "P7704" and c.normalized.startswith("organisation/") and \
                        c.normalized.split("/")[1] not in europeana_org_ids:
                    f.note += "; ID nicht in Europeana-Organisationsliste – manuell prüfen"
                else:
                    f.action = "fix"
                    if prop == "P7704" and c.normalized.startswith("organisation/"):
                        f.source = "europeana"  # Existenz in Europeana geprüft → Europeana als Fundstelle
                        f.source_id = c.normalized.split("/")[1]
                    if c.normalized in present:
                        f.note += "; korrekter Wert bereits vorhanden – nur löschen"
                out.append(f)
    return out


def p7704_duplicates(scan: list[dict[str, str]]) -> list[Finding]:
    """Organisations-P7704-Werte, die an mehreren Items hängen."""
    by_value: dict[str, set[str]] = defaultdict(set)
    for r in scan:
        if "deprecated" in r["rank"].lower():
            continue
        norm = normalize("P7704", r["value"])
        if norm and norm.startswith("organisation/"):
            by_value[norm].add(r["qid"])
    out = []
    for value, qids in sorted(by_value.items()):
        if len(qids) > 1:
            for q in sorted(qids):
                out.append(Finding("P7704", MEHRDEUTIG, q, "wikidata", "", value, [],
                                   note=f"Dubletten-Verdacht: {value} an {','.join(sorted(qids))}"))
    return out


def hold_duplicate_fixes(findings: list[Finding]) -> int:
    """Korrekturen für Werte, die an mehreren Items hängen (Dubletten-Verdacht), nur berichten:
    sonst stünde der korrigierte Wert mit Fundstelle an allen Dubletten."""
    dups = {f.source_value for f in findings if f.prop == "P7704" and f.note.startswith("Dubletten-Verdacht")}
    n = 0
    for f in findings:
        if f.prop == "P7704" and f.action == "fix" and f.source_value in dups:
            f.action, n = None, n + 1
            f.note += "; Wert an mehreren Items (Dublette) – erst klären"
    return n


# --- Namenskandidaten (nur zur Handprüfung) ----------------------------------
def _norm_name(s: str) -> str:
    s = s.lower().replace("ß", "ss")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_candidates(
    unmatched: list[SourceRecord], items: dict[str, WikidataItem], cutoff: float = 0.88, n: int = 3
) -> list[dict[str, str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for qid, it in items.items():
        for lab in set(it.labels.values()):
            index[_norm_name(lab)].append(qid)
    names = list(index)
    rows = []
    for rec in unmatched:
        key = _norm_name(rec.label)
        if not key:
            continue
        for cand in difflib.get_close_matches(key, names, n=n, cutoff=cutoff):
            score = difflib.SequenceMatcher(None, key, cand).ratio()
            for qid in index[cand]:
                rows.append({
                    "source": rec.source, "source_id": rec.source_id, "label": rec.label,
                    "qid": qid, "wikidata_label": items[qid].label, "score": f"{score:.2f}",
                })
    return rows


# --- Orte (Städte) ----------------------------------------------------------------
def classify_city(qid: str, item: WikidataItem, places: list[SourceRecord], lookups: Lookups,
                  record_counts: dict[str, int] | None = None) -> Finding:
    """P7704 für eine Wikidata-Stadt anhand der per sameAs aufgelösten Europeana-Orte.

    - kein Ort                → kein_treffer (nur Bericht)
    - genau ein Ort           → wie beim Organisationsabgleich (identisch / fehlt / formatfehler / konflikt …)
    - mehrere Orte (Dublette in Europeana; P7704 ist einwertig):
        * Wikidata führt bereits einen davon → dieser zählt
        * genau ein Ort hat die meisten verknüpften Objekte (recordCount > 0) → dieser wird vorgeschlagen
        * sonst mehrdeutig (nur Bericht)
    """
    wd_raw = [s.value for s in item.values("P7704")]
    if not places:
        return Finding("P7704", KEIN_TREFFER, qid, "europeana", "", None, wd_raw,
                       note="kein Europeana-Ort mit sameAs auf diese QID")
    if len(places) == 1:
        return classify_property(places[0], qid, item, "P7704", lookups)

    ids = [p.ids["P7704"][0] for p in places]
    note = f"Europeana führt {len(places)} Orte mit sameAs auf {qid}: {', '.join(ids)}"
    present = {normalize("P7704", v) for v in wd_raw}
    hit = [v for v in ids if v in present]
    if hit:
        f = classify_property(next(p for p in places if p.ids["P7704"][0] == hit[0]), qid, item, "P7704", lookups)
        f.note = "; ".join(x for x in (f.note, note) if x)
        return f
    counts = record_counts or {}
    ranked = sorted(places, key=lambda p: counts.get(p.source_id, 0), reverse=True)
    best, second = counts.get(ranked[0].source_id, 0), counts.get(ranked[1].source_id, 0)
    if best > second:
        f = classify_property(ranked[0], qid, item, "P7704", lookups)
        detail = ", ".join(f"{p.ids['P7704'][0]}: {counts.get(p.source_id, 0)} Objekte" for p in ranked)
        f.note = "; ".join(x for x in (f.note, f"{note}; gewählt nach verknüpften Objekten ({detail})") if x)
        return f
    return Finding("P7704", MEHRDEUTIG, qid, "europeana", ",".join(p.source_id for p in places), "|".join(ids),
                   wd_raw, note=note + " – manuell wählen")
