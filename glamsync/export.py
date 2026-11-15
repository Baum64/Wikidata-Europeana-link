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
        if f.prop != prop or f.action != action or not f.qid or not f.source_value:
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
             out_dir: Path | None = None) -> list[Path]:
    """Schreibt QS-Dateien in Teilen à max. 50 Zeilen (…_part01.qs usw.)."""
    date = date or today()
    out_dir = out_dir or config.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    label = "neu" if action == "new" else "korrektur"
    for old in out_dir.glob(f"quickstatements_{prop}_{label}_{date}_part*.qs"):
        old.unlink()
    paths = []
    for i, lines in enumerate(split_parts(qs_commands(findings, prop, action, date)), 1):
        p = out_dir / f"quickstatements_{prop}_{label}_{date}_part{i:02d}.qs"
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
        L += ["## Wikidata-Grundmenge (direkte P31) – vorher → nachher-Potenzial", "",
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
