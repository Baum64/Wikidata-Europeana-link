"""Ausgabe: QuickStatements, Bericht (CSV), Zusammenfassung (Markdown)."""

from __future__ import annotations

import csv
import datetime as dt
import logging
from collections import Counter
from pathlib import Path

from . import config
from .models import Finding
from .normalize import PROPERTIES
from .sources.wikidata import SOURCE_ITEMS

log = logging.getLogger(__name__)

BLOCK_SIZE = 50
ACTION_LABELS = {"new": "neu", "fix": "korrektur", "delete": "loeschen"}
PROP_NAMES = {"P7704": "Europeana entity", "P791": "ISIL", "P227": "GND", "P214": "VIAF", "P213": "ISNI"}


def today() -> str:
    return dt.date.today().isoformat()


def _qs_string(v: str) -> str:
    return '"' + v.replace('"', '\\"') + '"'


def _reference(source: str, date: str) -> str:
    item = SOURCE_ITEMS.get(source)
    if not item:
        return ""
    return f"|S248|{item}|S813|+{date}T00:00:00Z/11"


def qs_commands(findings: list[Finding], prop: str, action: str, date: str) -> list[list[str]]:
    """Liefert Befehlsgruppen (eine Gruppe = Zeilen, die zusammen bleiben müssen)."""
    groups: list[list[str]] = []
    seen: set[str] = set()
    for f in findings:
        if f.prop != prop or f.action != action or not f.qid:
            continue
        if action == "delete":
            key = f"{f.qid}|{prop}|{f.old_value}"
            if f.old_value and key not in seen:
                seen.add(key)
                groups.append([f"-{f.qid}|{prop}|{_qs_string(f.old_value)}"])
            continue
        if not f.source_value:
            continue
        add = f"{f.qid}|{prop}|{_qs_string(f.source_value)}{_reference(f.source, date)}"
        if action == "new":
            key = f"{f.qid}|{prop}|{f.source_value}"
            if key in seen:
                continue
            seen.add(key)
            groups.append([add])
        elif action == "fix":
            key = f"{f.qid}|{prop}|{f.old_value}"
            if key in seen:
                continue
            seen.add(key)
            lines = [f"-{f.qid}|{prop}|{_qs_string(f.old_value)}"]
            if f.source_value not in f.wikidata_values:
                lines.append(add)
            groups.append(lines)
    return groups


def split_parts(groups: list[list[str]], size: int = BLOCK_SIZE) -> list[list[str]]:
    """Teilt Befehlsgruppen in Dateien à max. `size` Zeilen; Gruppen werden nicht zerrissen."""
    parts: list[list[str]] = []
    current: list[str] = []
    for group in groups:
        if current and len(current) + len(group) > size:
            parts.append(current)
            current = []
        current.extend(group)
    if current:
        parts.append(current)
    return parts


def write_qs(findings: list[Finding], prop: str, action: str, date: str | None = None,
             out_dir: Path | None = None, tag: str = "") -> list[Path]:
    """Schreibt QS-Dateien in Teilen à max. 50 Zeilen (…_part01.qs usw.).

    `tag` unterscheidet weitere Abgleiche im Dateinamen, z. B. 'staedte_Q183'.
    """
    date = date or today()
    out_dir = out_dir or config.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    label = ACTION_LABELS[action]
    stem = f"quickstatements_{prop}_{tag + '_' if tag else ''}{label}_{date}"
    for old in out_dir.glob(f"{stem}_part*.qs"):
        old.unlink()
    paths = []
    for i, lines in enumerate(split_parts(qs_commands(findings, prop, action, date)), 1):
        p = out_dir / f"{stem}_part{i:02d}.qs"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


def write_report(matches: list[dict], wd_findings: list[Finding], path: Path) -> None:
    cols = ["source", "source_id", "label", "url", "qid", "wikidata_label", "match_via", "match_status", "note"]
    for p in PROPERTIES:
        cols += [f"{p}_status", f"{p}_quelle", f"{p}_wikidata", f"{p}_aktion", f"{p}_hinweis"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";")
        w.writeheader()
        for m in matches:
            row = {k: m.get(k, "") for k in cols[:9]}
            for f in m["findings"]:
                p = f["prop"]
                row.update({
                    f"{p}_status": f["status"], f"{p}_quelle": f["source_value"] or "",
                    f"{p}_wikidata": "|".join(f["wikidata_values"]), f"{p}_aktion": f["action"] or "",
                    f"{p}_hinweis": f["note"],
                })
            w.writerow(row)
        for f in wd_findings:
            w.writerow({
                "source": "wikidata", "source_id": f.source_id, "qid": f.qid, "match_status": "nur_wikidata",
                f"{f.prop}_status": f.status, f"{f.prop}_quelle": f.source_value or "",
                f"{f.prop}_wikidata": f.old_value or "|".join(f.wikidata_values),
                f"{f.prop}_aktion": f.action or "", f"{f.prop}_hinweis": f.note,
            })


def write_candidates(rows: list[dict], path: Path) -> None:
    cols = ["source", "source_id", "label", "qid", "wikidata_label", "score"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def write_summary(summary: dict, path: Path) -> None:
    L = [f"# Abgleich Europeana / DDB ↔ Wikidata – {summary['date']}", ""]
    L.append(f"Land: {summary['country']} · Typen: {', '.join(summary['types'])}"
             + (f" · **Trockenlauf, max. {summary['limit']} Einrichtungen je Quelle**" if summary.get("limit") else ""))
    L.append("")
    if summary.get("base"):
        L += [f"## Wikidata-Grundmenge ({'P31/P279*' if summary.get('subclasses') else 'direkte P31'}) – vorher → nachher-Potenzial", "",
              "| Typ | gesamt | ISIL | GND | GND oder ISIL | Europeana |", "|---|---|---|---|---|---|"]
        for t, row in summary["base"].items():
            cells = [f"{row['before'][k]} → {row['after'][k]}" if row['after'][k] != row['before'][k]
                     else str(row['before'][k]) for k in ("isil", "gnd", "either", "europeana")]
            L.append(f"| {t} | {row['total']} | " + " | ".join(cells) + " |")
        L += ["", "„Nachher“ zählt nur Werte aus `fehlt_in_wikidata`-QuickStatements.", ""]
    L += ["## Zuordnung der Quell-Einrichtungen", "", "| Quelle | Einrichtungen | sameAs | ISIL | GND | mehrdeutig | kein Treffer |",
          "|---|---|---|---|---|---|---|"]
    for src, c in summary["matching"].items():
        L.append(f"| {src} | {c['total']} | {c.get('sameAs', 0)} | {c.get('ISIL', 0)} | {c.get('GND', 0)} | "
                 f"{c.get('mehrdeutig', 0)} | {c.get('kein_treffer', 0)} |")
    L += ["", "## Klassifikation je Property", "",
          "| Property | Quelle | " + " | ".join(summary["statuses"]) + " |",
          "|---|---|" + "---|" * len(summary["statuses"])]
    for (prop, src), c in sorted(summary["classification"].items()):
        L.append(f"| {prop} {PROP_NAMES[prop]} | {src} | " + " | ".join(str(c.get(s, 0)) for s in summary["statuses"]) + " |")
    L += ["", "## QuickStatements", "", "| Property | Aktion | Befehle | Dateien |", "|---|---|---|---|"]
    for r in summary["qs"]:
        L.append(f"| {r['prop']} | {r['action']} | {r['commands']} | {r['files']} |")
    if summary.get("notes"):
        L += ["", "## Hinweise", ""] + [f"- {n}" for n in summary["notes"]]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def count_classification(matches: list[dict], wd_findings: list[Finding]) -> dict:
    c: dict[tuple, Counter] = {}
    for m in matches:
        for f in m["findings"]:
            c.setdefault((f["prop"], m["source"]), Counter())[f["status"]] += 1
    for f in wd_findings:
        c.setdefault((f.prop, "wikidata"), Counter())[f.status] += 1
    return c


# --- Register (ISIL-Verzeichnis, Museen in Österreich) ------------------------------
REGISTRY_NAMES = {"isil": "Deutsche ISIL-Agentur (ISIL-Verzeichnis)", "mure": "Museen in Österreich"}


def _registry_reference(source: str, url: str, date: str) -> str:
    if source in SOURCE_ITEMS:
        return f"|S248|{SOURCE_ITEMS[source]}|S813|+{date}T00:00:00Z/11"
    # kein Wikidata-Item für die Quelle → Referenz-URL des Eintrags
    return f"|S854|{_qs_string(url)}|S813|+{date}T00:00:00Z/11"


def write_registry_qs(matches, prop: str, source: str, country: str, date: str,
                      out_dir: Path | None = None) -> list[Path]:
    """Starke Kandidaten (Name + Website gleich) → eigene Kandidaten-Dateien zur Handprüfung."""
    from .registry import STARK

    out_dir = out_dir or config.OUT_DIR
    for old in out_dir.glob(f"quickstatements_{prop}_kandidaten_{country}_{date}_part*.qs"):
        old.unlink()
    groups = [[f"{m.qids[0]}|{prop}|{_qs_string(m.record.value)}{_registry_reference(source, m.record.url, date)}"]
              for m in matches if m.status == STARK]
    paths = []
    for i, lines in enumerate(split_parts(groups), 1):
        p = out_dir / f"quickstatements_{prop}_kandidaten_{country}_{date}_part{i:02d}.qs"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


def write_registry_report(matches, items, path: Path) -> None:
    cols = ["status", "wert", "name", "ort", "website", "register_url", "qids", "wikidata_label", "score", "hinweis"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";")
        w.writeheader()
        for m in matches:
            r = m.record
            w.writerow({
                "status": m.status, "wert": r.value, "name": r.name, "ort": r.city,
                "website": " ".join(r.websites), "register_url": r.url, "qids": " ".join(m.qids),
                "wikidata_label": items[m.qids[0]].label if m.qids and m.qids[0] in items else "",
                "score": f"{m.score:.2f}" if m.score is not None else "", "hinweis": m.note,
            })


def write_registry_cleanup(cleanup, items, prop: str, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["qid", "wikidata_label", prop, "art", "aktion", "hinweis"])
        for c in cleanup:
            w.writerow([c.qid, items[c.qid].label if c.qid in items else "", c.value, c.kind, c.action or "", c.note])


def write_registry_delete_qs(cleanup, prop: str, country: str, date: str, out_dir: Path | None = None) -> list[Path]:
    """Löschvorschläge: ungültige Werte → …_loeschen_…, im Register fehlende Werte → …_loeschen_pruefen_…"""
    out_dir = out_dir or config.OUT_DIR
    paths = []
    for kind, label in (("ungueltig", "loeschen"), ("nicht_im_register", "loeschen_pruefen")):
        for old in out_dir.glob(f"quickstatements_{prop}_{label}_{country}_{date}_part*.qs"):
            old.unlink()
        groups, seen = [], set()
        for c in cleanup:
            line = f"-{c.qid}|{prop}|{_qs_string(c.value)}"
            if c.kind == kind and c.action == "delete" and line not in seen:
                seen.add(line)
                groups.append([line])
        for i, lines in enumerate(split_parts(groups), 1):
            p = out_dir / f"quickstatements_{prop}_{label}_{country}_{date}_part{i:02d}.qs"
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            paths.append(p)
    return paths


def write_registry_summary(s: dict, path: Path) -> None:
    L = [f"# Registerabgleich {REGISTRY_NAMES[s['source']]} ↔ Wikidata ({s['prop']}) – {s['date']}", "",
         f"Land: {s['country']} · Kandidaten-Pool: {s['pool']} Wikidata-Items ({'P31/P279*' if s.get('subclasses') else 'direkte P31'}: {', '.join(s['types'])})", "",
         "| Status | Anzahl | Bedeutung |", "|---|---|---|"]
    meaning = {
        "vorhanden": "Wert steht bereits an genau einem Item",
        "vorhanden_pruefen": "Wert vorhanden, aber Name passt kaum – prüfen",
        "kandidat_stark": "Item ohne Wert mit gleichem Namen UND gleicher Website → Kandidaten-QS",
        "kandidat_schwach": "nur Name, oder Website + ähnlicher Name, oder ähnlicher Name + < 1 km → nur CSV",
        "konflikt": "starker Kandidat hat schon einen anderen Wert",
        "mehrdeutig": "mehrere Items bzw. konkurrierende Einträge",
        "kein_treffer": "kein Kandidat im Pool",
    }
    L += [f"| `{st}` | {s['counts'].get(st, 0)} | {meaning[st]} |" for st in s["statuses"]]
    L += ["", f"Register-Einträge gesamt: {s['records']}",
          "", "## Bereinigung vorhandener Wikidata-Werte", "",
          "| Art | Löschvorschlag (QS) | nur Bericht (hat Quellen/Qualifier) |", "|---|---|---|",
          f"| ungültiges Format | {s['cleanup'].get(('ungueltig', 'delete'), 0)} | {s['cleanup'].get(('ungueltig', 'bericht'), 0)} |",
          f"| nicht im Register | {s['cleanup'].get(('nicht_im_register', 'delete'), 0)} | {s['cleanup'].get(('nicht_im_register', 'bericht'), 0)} |",
          "",
          "`…_loeschen_…` = ungültige Werte; `…_loeschen_pruefen_…` = Werte, die im Register fehlen "
          "(können aufgelöste Einrichtungen sein – vor dem Löschen prüfen).", "",
          "## QuickStatements (nur nach Handprüfung einspielen!)", ""]
    L += [f"- `{f}`" for f in s["qs_files"]] or ["- keine"]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# --- Städteabgleich (Europeana-Orte) ----------------------------------------------
def write_city_report(rows: list[dict], path: Path) -> None:
    cols = ["land", "qid", "wikidata_label", "europeana_id", "europeana_label", "europeana_url", "status",
            "P7704_vorschlag", "P7704_wikidata", "aktion", "hinweis"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def write_city_summary(s: dict, path: Path) -> None:
    lines = [
        f"# Städteabgleich Europeana-Orte ↔ Wikidata ({s['country']}, {s['date']})",
        "",
        f"- Grundmenge: P31 {s['class']} (Großstadt) oder Stadt (Q515) mit ≥ {s['min_population']:_} Einwohnern "
        f"({'P31/P279*' if s['subclasses'] else 'nur direkte P31'}), ohne aufgelöste (P576); "
        f"{s['countries']} Länder: **{s['cities']} Städte**".replace("_", "."),
        "- Zuordnung ausschließlich über sameAs der Europeana-Orte (`/entity/resolve`), kein Namensabgleich.",
        "- Fundstelle in den QuickStatements: S248 Europeana (Q234110) + S813 Abrufdatum.",
        "",
        "| Status | Anzahl |",
        "|---|---:|",
    ]
    lines += [f"| {k} | {v} |" for k, v in s["status"].items()]
    if s.get("failed"):
        lines += ["", "**Nicht abrufbar (SPARQL-Fehler):** " + ", ".join(s["failed"])]
    if len(s.get("per_country", {})) > 1:
        cols = ["fehlt_in_wikidata", "identisch", "formatfehler", "konflikt", "mehrdeutig", "kein_treffer"]
        lines += ["", "## Nach Land", "", "| Land | Städte | " + " | ".join(cols) + " |",
                  "|---|---:|" + "---:|" * len(cols)]
        for land, c in s["per_country"].items():
            lines.append(f"| {land} | {sum(c.values())} | " + " | ".join(str(c.get(x, 0)) for x in cols) + " |")
    lines += ["", "## QuickStatements", ""]
    if s["files"]:
        lines += [f"- `{p}`" for p in s["files"]]
    else:
        lines.append("- keine")
    if s.get("duplicates"):
        lines += ["", "## Europeana-Dubletten (mehrere Orte mit derselben QID)", ""]
        lines += [f"- {d}" for d in s["duplicates"]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
