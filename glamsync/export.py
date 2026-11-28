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

BLOCK_SIZE = 500  # Zeilen je QS-Datei
QS_EXT = ".txt"
ACTION_LABELS = {"new": "neu", "fix": "korrektur", "delete": "loeschen"}
PROP_NAMES = {"P7704": "Europeana entity", "P791": "ISIL", "P227": "GND", "P214": "VIAF", "P213": "ISNI"}


def today() -> str:
    return dt.date.today().isoformat()


# --- Dateinamen in out/ -----------------------------------------------------------------
# Datum vorn, danach die Property (bei QuickStatements immer, bei Berichten, die zu genau einer
# Property gehören), dann der Rest: So stehen die Dateien eines Laufs und einer Property im
# Ordner beieinander. QuickStatements: <datum>_<P>_<…>_partNN.txt, Berichte: <datum>_[<P>_]<name>.<ext>
def qs_stem(date: str, prop: str, *parts: str) -> str:
    """Stamm einer QS-Datei ohne _partNN.txt, z. B. 2026-11-28_P856_Q183_neu."""
    return "_".join([date, prop, *[x for x in parts if x]])


def out_path(date: str, name: str, ext: str, prop: str = "", out_dir: Path | None = None) -> Path:
    """Berichtsdatei <datum>_[<P>_]<name><ext> in out/, z. B. 2026-11-28_P856_webseiten_Q183.csv."""
    return (out_dir or config.OUT_DIR) / ("_".join(x for x in (date, prop, name) if x) + ext)


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
             out_dir: Path | None = None, tag: str = "", label: str | None = None) -> list[Path]:
    """Schreibt QS-Dateien in Teilen à max. BLOCK_SIZE Zeilen (<datum>_<P>_…_part01.txt usw.).

    `tag` unterscheidet weitere Abgleiche im Dateinamen, z. B. 'staedte_Q183';
    `label` ersetzt die Aktionsbezeichnung (z. B. 'loeschen_pruefen').
    """
    date = date or today()
    out_dir = out_dir or config.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    label = label or ACTION_LABELS[action]
    stem = qs_stem(date, prop, tag, label)
    for old in out_dir.glob(f"{stem}_part*{QS_EXT}"):
        old.unlink()
    paths = []
    for i, lines in enumerate(split_parts(qs_commands(findings, prop, action, date)), 1):
        p = out_dir / f"{stem}_part{i:02d}{QS_EXT}"
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
    stem = qs_stem(date, prop, "kandidaten", country)
    for old in out_dir.glob(f"{stem}_part*{QS_EXT}"):
        old.unlink()
    groups = [[f"{m.qids[0]}|{prop}|{_qs_string(m.record.value)}{_registry_reference(m.record.source, m.record.url, date)}"]
              for m in matches if m.status == STARK]
    paths = []
    for i, lines in enumerate(split_parts(groups), 1):
        p = out_dir / f"{stem}_part{i:02d}{QS_EXT}"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


def write_registry_sure_qs(matches, prop: str, source: str, country: str, date: str,
                           out_dir: Path | None = None) -> list[Path]:
    """Sichere Kandidaten (Ort gleich, Label sehr ähnlich) → <datum>_<P>_<quelle>_<land>_neu_partNN.txt."""
    from .registry import SICHER

    groups = [[f"{m.qids[0]}|{prop}|{_qs_string(m.record.value)}{_registry_reference(m.record.source, m.record.url, date)}"]
              for m in matches if m.status == SICHER]
    return write_qs_groups(groups, prop, f"{source}_{country}", "neu", date, out_dir)


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


def write_gnd_redirects(findings, items, path: Path) -> None:
    """P227-Aussagen auf umgelenkte/gelöschte GND (aktion: korrektur → QuickStatements, sonst Bericht)."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["status", "qid", "wikidata_label", "p227_alt", "p227_ziel", "rang", "quellen", "qualifikatoren",
                    "aktion", "hinweis"])
        for f in findings:
            w.writerow([f.status, f.qid, items[f.qid].label if f.qid in items else "", f.old, f.new or "",
                        f.rank, f.refs, f.quals, f.action, f.note])


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
        stem = qs_stem(date, prop, label, country)
        for old in out_dir.glob(f"{stem}_part*{QS_EXT}"):
            old.unlink()
        groups, seen = [], set()
        for c in cleanup:
            line = f"-{c.qid}|{prop}|{_qs_string(c.value)}"
            if c.kind == kind and c.action == "delete" and line not in seen:
                seen.add(line)
                groups.append([line])
        for i, lines in enumerate(split_parts(groups), 1):
            p = out_dir / f"{stem}_part{i:02d}{QS_EXT}"
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
        "kandidat_sicher": "genau ein Item am selben Ort, ohne Wert, Label sehr ähnlich (≥ 0,95) → QS neu",
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
        cols = ["fehlt_in_wikidata", "fehlt_ohne_objekte", "identisch", "formatfehler", "konflikt", "mehrdeutig", "kein_treffer"]
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


# --- Personenabgleich (Europeana-Agents) ------------------------------------------
def write_person_report(rows: list[dict], path: Path) -> None:
    cols = ["qid", "wikidata_label", "property", "status", "europeana_id", "europeana_label", "europeana_url",
            "vorschlag", "wikidata", "aktion", "hinweis"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def write_person_summary(s: dict, path: Path) -> None:
    def table(title: str, counts: dict) -> list[str]:
        out = ["", f"## {title}", "", "| Status | Anzahl |", "|---|---:|"]
        out += [f"| {k} | {v} |" for k, v in counts.items()] or ["| – | 0 |"]
        return out

    lines = [
        f"# Personenabgleich Europeana-Agents ↔ Wikidata ({s['date']})",
        "",
        f"- Europeana-Agents: **{s['agents']}**, zugeordnete Wikidata-Items: **{s['items']}**",
        f"- vorhandene agent-Werte (P7704) in Wikidata: {s['wikidata_values']}",
        "- Zuordnung über sameAs → QID; GND als Gegenprobe. Kein Namensabgleich.",
        f"- P227-Vorschläge nur nach Prüfung über lobid-gnd ({s['gnd_checked']} GND geprüft).",
        "- Fundstelle: S248 Europeana (Q234110) + S813 Abrufdatum.",
    ]
    lines += table("P7704 Europeana entity", s["p7704"])
    lines += table("P227 GND", s["p227"])
    lines += table("Bereinigung: Wikidata-Werte ohne Europeana-Agent", s["cleanup"])
    lines += ["", "## QuickStatements", ""] + ([f"- `{f}`" for f in s["files"]] or ["- keine"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- GND-Dump: Kandidatenliste, Mix'n'match ----------------------------------------
GND_TYPE_NAMES = {"museum": "Museum", "library": "Bibliothek", "archive": "Archiv"}
GND_STATUS_MEANING = {
    "vorhanden": "GND steht bereits an genau einem Item (P227)",
    "vorhanden_alt": "Wikidata führt eine umgelenkte (alte) GND dieser Einrichtung – Bericht",
    "kandidat_sicher": "genau ein Item ohne P227, Ort gleich, GND-Name dem Label sehr ähnlich (≥ 0,95) – QuickStatement",
    "kandidat_stark": "Item ohne P227 mit gleichem Namen UND gleichem Ort – nur CSV, Handprüfung",
    "kandidat_schwach": "Name gleich (anderer/unbekannter Ort), Name ähnlich + gleicher Ort, oder Website gleich – nur CSV",
    "konflikt": "starker Kandidat hat schon eine andere P227 – Bericht",
    "mehrdeutig": "GND an mehreren Items oder konkurrierende Kandidaten – Bericht",
    "kein_treffer": "kein Kandidat im Pool",
    "historisch": "abgeschlossener Zeitraum, kein Nachfolgesatz (aufgelöst) – keine Kandidatensuche, nicht in Mix'n'match",
    "vorgaenger": "abgeschlossener Zeitraum mit Nachfolgesatz (frühere Namensform) – ebenso",
    "gnd_umgelenkt": "GND nach dem Dump-Stand umgelenkt (Umlenkungsdatei) – weder Kandidat noch Mix'n'match",
    "gnd_geloescht": "GND nach dem Dump-Stand gelöscht (Umlenkungsdatei) – ebenso",
}
GND_ANNOTATION_MEANING = {
    "historisch_in_wikidata": "GND: aufgelöst (ohne Nachfolger); Item ohne P576",
    "zeitraum_abweichend": "genaues GND-Jahr weicht von P571 (Satz ohne Vorgänger) bzw. P576 (aufgelöst) ab",
    "vorgaenger_gnd": "Item trägt die GND einer früheren Namensform (Satz mit Nachfolger)",
    "p576_nur_in_wikidata": "GND ohne Ende, Item mit P576",
}
PERIOD_COLUMNS = ["gnd_zeitraum", "gnd_historisch", "wd_p571", "wd_p576", "anmerkung"]


def write_gnd_report(matches, items, path: Path, annotations: dict | None = None) -> None:
    """Kandidatenliste/Bericht: eine Zeile je GND-Einrichtung (Trennzeichen ;).

    annotations: GND → Spalten gnd_zeitraum … anmerkung (nur bei Status vorhanden gefüllt).
    """
    annotations = annotations or {}
    cols = ["status", "gnd", "name", "typ", "land", "ort", "oberbegriff", "zeitraum", "homepage",
            "gnd_url", "qids", "wikidata_label", "score", "hinweis"] + PERIOD_COLUMNS
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";")
        w.writeheader()
        for m in matches:
            b = m.body
            w.writerow({
                "status": m.status, "gnd": b.gnd, "name": b.name,
                "typ": ",".join(GND_TYPE_NAMES.get(t, t) for t in b.types), "land": b.country,
                "ort": ", ".join(b.place_names), "oberbegriff": ", ".join(b.broader),
                "zeitraum": f"{b.established}–{b.terminated}" if b.established or b.terminated else "",
                "homepage": " ".join(b.homepages), "gnd_url": f"https://d-nb.info/gnd/{b.gnd}",
                "qids": " ".join(m.qids),
                "wikidata_label": items[m.qids[0]].label if m.qids and m.qids[0] in items else "",
                "score": f"{m.score:.2f}" if m.score is not None else "", "hinweis": m.note,
                **annotations.get(b.gnd, {"gnd_zeitraum": b.period_raw, "gnd_historisch": b.period_status}),
            })


def write_gnd_format(findings, items, path: Path) -> None:
    """P227-Werte im Kandidaten-Pool mit Formatfehler oder falscher Prüfziffer (nur Bericht)."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["qid", "wikidata_label", "P227", "problem"])
        for q, v, problem in findings:
            w.writerow([q, items[q].label if q in items else "", v, problem])


# Spaltennamen wie auf der Importseite (Groß-/Kleinschreibung egal), Format wie die
# Mix'n'match-Testdatei: CSV mit Komma, eine Kopfzeile, Felder bei Bedarf in "…"
MNM_COLUMNS = ["id", "name", "description", "url", "type"]


def write_mnm(rows: dict, date: str, out_dir: Path | None = None, lands=None) -> list[tuple[Path, int]]:
    """<datum>_P227_mnm_gnd_<typ>_<land>.csv mit Kopfzeile id,name,description,url,type (Mix'n'match-Import)."""
    out_dir = out_dir or config.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # Nur eigene Dateien (je Typ) ersetzen; selbst zusammengestellte Dateien bleiben stehen
    for land in lands or {k[1] for k in rows}:
        for typ in GND_TYPE_NAMES:
            for ext in (".tsv", ".csv"):
                out_path(date, f"mnm_gnd_{typ}_{land}", ext, "P227", out_dir).unlink(missing_ok=True)
    paths = []
    for (typ, land), lines in sorted(rows.items()):
        p = out_path(date, f"mnm_gnd_{typ}_{land}", ".csv", "P227", out_dir)
        with p.open("w", newline="", encoding="utf-8") as fh:
            # Felder mit Komma oder Anführungszeichen werden in "…" gesetzt (Quote verdoppelt)
            w = csv.DictWriter(fh, fieldnames=MNM_COLUMNS, delimiter=",", lineterminator="\n")
            w.writeheader()
            w.writerows(sorted(lines, key=lambda r: r["id"]))
        paths.append((p, len(lines)))
    return paths


def write_gnd_summary(s: dict, path: Path) -> None:
    ds = s["dump_stats"]
    L = [f"# GND-Körperschaften ↔ Wikidata (P227) – {s['date']}", "",
         f"Land: {s['country']} · Typen: {', '.join(s['types'])} · Kandidaten-Pool: {s['pool']} Wikidata-Items "
         f"({'P31/P279*' if s.get('subclasses') else 'direkte P31'})", ""]
    if s.get("limit"):
        L += [f"**Trockenlauf:** {s['records']} von {s['total']} Einrichtungen.", ""]
    L += ["## GND-Dump", "",
          f"- Datei: `{s['dump']}` (Tabelle erzeugt {s['built']})",
          f"- Datensätze: {ds.get('datensaetze', 0)}; davon Museen/Bibliotheken/Archive (alle Länder): "
          f"{ds.get('einrichtungen', 0)}",
          f"- defekte Knoten/Sätze: {ds.get('defekt_knoten', 0)}/{ds.get('defekt_datensatz', 0)}; "
          f"GND-IDs mit falscher Prüfziffer: {ds.get('formatfehler_gnd_id', 0)}", "",
          "| Typ / Land | Einrichtungen | davon mit Wikidata-Link in der GND |", "|---|---:|---:|"]
    L += [f"| {k} | {v[0]} | {v[1]} |" for k, v in s["groups"].items()]
    L += ["", "## Abgleich", "", "| Status | Anzahl | Bedeutung |", "|---|---:|---|"]
    L += [f"| `{st}` | {s['counts'].get(st, 0)} | {GND_STATUS_MEANING[st]} |" for st in s["statuses"]]
    L += ["", f"GND bereits in Wikidata (aktuell oder alt): {s['in_wikidata']} · "
          f"P227-Formatfehler im Pool (nur Bericht): {s['format']}", "",
          "Für fehlende P227 gibt es **keine QuickStatements**: Die Wikidata-Links der GND sind Rückverweise "
          "auf vorhandene P227, und die GND enthält keine ISIL. Starke Kandidaten nur nach Handprüfung übernehmen.", ""]
    per = s.get("period")
    if per:
        L += ["## Zeitraum", "",
              f"- Stand Dump: {s.get('dump_date') or '?'}",
              f"- historisch (abgeschlossen, ohne Nachfolger): {per['historisch']} · "
              f"frühere Namensform (mit Nachfolger): {per['vorgaenger']}"
              + (" – in Kandidatensuche und Mix'n'match **einbezogen** (--include-historical)"
                 if s.get("include_historical") else " – nicht in Kandidatenliste und Mix'n'match"), "",
              "Anmerkungen bei Items, die die GND schon tragen (nur Bericht, Spalte `anmerkung`):", "",
              "| Anmerkung | Items | Bedeutung |", "|---|---:|---|"]
        L += [f"| `{k}` | {per['annotations'].get(k, 0)} | {v} |" for k, v in GND_ANNOTATION_MEANING.items()]
        if s.get("propose_dates"):
            L += ["", f"Zeitraum als Qualifikator an P227 (--propose-dates, nur genaues Jahr, nur wenn der "
                  f"Qualifikator fehlt): {per['dates'].get('statements', 0)} Aussagen, "
                  f"P580 {per['dates'].get('P580', 0)}, P582 {per['dates'].get('P582', 0)}"]
        L += [""]
    rd = s.get("redirects") or {}
    L += ["## Umlenkungen und Löschungen", ""]
    if rd.get("file"):
        st = rd.get("stats", {})
        fc = rd.get("findings", {})
        L += [f"- Datei: `{rd['file']}` (Stand {rd.get('date') or '?'}): {st.get('umgelenkt', 0)} Umlenkungen, "
              f"{st.get('geloescht', 0)} Löschungen, defekt {st.get('defekt_objekt', 0)}",
              f"- GND der Tabelle nach dem Dump-Stand umgelenkt/gelöscht: {s['counts'].get('gnd_umgelenkt', 0)}/"
              f"{s['counts'].get('gnd_geloescht', 0)}",
              f"- P227 in Wikidata auf umgelenkte GND (nicht deprecated): Korrektur "
              f"{sum(n for (k, a), n in fc.items() if k == 'p227_umgelenkt' and a == 'korrektur')}, "
              f"nur Bericht {sum(n for (k, a), n in fc.items() if k == 'p227_umgelenkt' and a == 'bericht')}",
              f"- P227 auf gelöschte GND (nur Bericht): {sum(n for (k, _), n in fc.items() if k == 'p227_geloescht')}"]
    else:
        L += ["- Umlenkungsdatei nicht gefunden – Prüfung entfallen (GND_REDIRECTS_PATH)."]
    if s.get("qs"):
        L += ["", "QuickStatements:"] + [f"- `{f}`" for f in s["qs"]]
    L += ["",
          "## Mix'n'match (nur GND ohne P227 in Wikidata)", "", "| Datei | Einträge |", "|---|---:|"]
    L += [f"| `{f}` | {n} |" for f, n in s["mnm"].items()] or ["| – | 0 |"]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# --- GND: deutsche Labels, Beschreibungen, Aliase ---------------------------------
TERM_KINDS = {"Lde": "Label", "Dde": "Beschreibung", "Ade": "Alias"}


def write_qs_groups(groups: list[list[str]], prop: str, tag: str, label: str, date: str,
                    out_dir: Path | None = None) -> list[Path]:
    """Fertige Befehlsgruppen als <datum>_<prop>_<tag>_<label>_partNN.txt (je ≤ BLOCK_SIZE Zeilen)."""
    out_dir = out_dir or config.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = qs_stem(date, prop, tag, label)
    for old in out_dir.glob(f"{stem}_part*{QS_EXT}"):
        old.unlink()
    paths = []
    for i, lines in enumerate(split_parts(groups), 1):
        p = out_dir / f"{stem}_part{i:02d}{QS_EXT}"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


def write_terms_report(proposals, path: Path) -> None:
    cols = ["qid", "gnd", "label_wikidata", "label_neu", "beschreibung_wikidata", "beschreibung_neu",
            "aliase_neu", "hinweis"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cols)
        for p in proposals:
            if p.has_changes or p.note:
                w.writerow([p.qid, "|".join(p.gnd), p.label_now, p.label, p.description_now, p.description,
                            " | ".join(p.aliases), p.note])


def write_label_report(proposals, path: Path, country_names: dict[str, str] | None = None) -> None:
    """Bericht zu `labels --source wikidata`: alle Items mit Vorschlag oder Hinweis."""
    names = country_names or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["qid", "land", "labels_wikidata", "mul_neu", "en_neu", "en_quelle", "europeana", "hinweis"])
        for p in proposals:
            w.writerow([p.qid, ", ".join(names.get(c, c) for c in p.countries),
                        " | ".join(f"{k}: {v}" for k, v in sorted(p.labels_now.items())), p.mul, p.en,
                        p.en_source, " ".join(f"organisation/{n}" for n in p.europeana), p.note])


# --- Organisationen: Europeana ↔ Wikidata europaweit -------------------------------
ORG_STATUS_MEANING = {
    "identisch": "Wikidata führt den Europeana-Wert",
    "fehlt_in_wikidata": "Organisation, Name gleich, mit Objekten → `…_neu_…`",
    "fehlt_ohne_objekte": "wie oben, aber 0 Objekte in Europeana → `…_ohne_objekte_…` (zunächst nicht einspielen)",
    "name_pruefen": "sonst passend, aber kein Name gleich → `…_pruefen_…`, auskommentiert (Handprüfung)",
    "kandidat_stark": "ohne sameAs: genau ein Item mit gleicher Website/Kennung UND gleichem Namen → `…_kandidaten_…`",
    "kandidat_schwach": "ohne sameAs: Website/Kennung + ähnlicher Name, oder Domain + gleicher Name → auskommentiert",
    "kein_organisation": "Item ist keine Organisation (Gebäude, Zeitschrift, Projekt …) oder ein Gebiet mit Einwohnerzahl – Bericht",
    "konflikt": "Item führt einen anderen P7704-Wert – Bericht",
    "mehrdeutig": "Dublette ohne Favorit, Wert an anderem Item, mehrere QIDs/Kandidaten – Bericht",
    "abgelehnt_in_wikidata": "derselbe Wert steht am Item mit Rang „veraltet“ – Bericht",
    "kein_treffer": "QID existiert nicht bzw. kein Kandidat",
}


def write_org_report(rows: list[dict], path: Path) -> None:
    cols = ["stufe", "land", "europeana_id", "europeana_label", "objekte", "homepage", "europeana_url", "qid",
            "wikidata_label", "status", "vorschlag", "wikidata", "datei", "hinweis"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def write_org_summary(s: dict, path: Path) -> None:
    def table(title: str, counts: dict) -> list[str]:
        out = ["", f"## {title}", "", "| Status | Anzahl | Bedeutung |", "|---|---:|---|"]
        out += [f"| {k} | {v} | {ORG_STATUS_MEANING.get(k, '')} |" for k, v in counts.items()] or ["| – | 0 | |"]
        return out

    L = [
        f"# Organisationen: Europeana ↔ Wikidata ({s['date']})",
        "",
        f"- Europeana-Organisationen: **{s['orgs']}**, davon mit Wikidata-sameAs {s['with_qid']} "
        f"({s['multi_qid']} mit mehreren QIDs), ohne sameAs {s['no_qid']}",
        f"- vorhandene `organisation/…`-Werte in Wikidata: {s['wikidata_values']}",
        "- Fundstelle aller Vorschläge: S248 Europeana (Q234110) + S813 Abrufdatum.",
        f"- Stufe 2 (Website/Kennung): {'durchsucht ' + str(s['no_qid_searched']) + ' Organisationen, Kandidaten für ' + str(s['with_candidates']) if s['candidates'] else 'nicht ausgeführt (--no-candidates)'}",
        f"- Klassenprüfung Organisation: {s['classes']} Klassen in `data/klassen_organisation.json`",
    ]
    L += table("Stufe 1: sameAs → QID", s["stage1"])
    L += table("Stufe 2: ohne sameAs", s["stage2"])
    L += ["", "## Formatprüfung vorhandener Werte", "", "| Befund/Aktion | Anzahl |", "|---|---:|"]
    L += [f"| {k} | {v} |" for k, v in s["wikidata"].items()] or ["| – | 0 |"]
    sts = ["fehlt_in_wikidata", "fehlt_ohne_objekte", "name_pruefen", "kandidat_stark", "kandidat_schwach"]
    L += ["", "## Vorschläge nach Land", "", "| Land | " + " | ".join(sts) + " |", "|---|" + "---:|" * len(sts)]
    L += [f"| {land} | " + " | ".join(str(c.get(x, 0)) for x in sts) + " |" for land, c in s["by_country"].items()]
    L += ["", "## QuickStatements", ""] + ([f"- `{f}`" for f in s["files"]] or ["- keine"])
    L += ["", "Mit `#` beginnende Zeilen (`…_pruefen_…`, schwache Kandidaten) sind auskommentiert: geprüfte",
          "Befehlszeilen entkommentieren, dann alle übrigen `#`-Zeilen entfernen (`grep -v '^#'`) und erst",
          "danach einspielen. QuickStatements dokumentiert `#`-Zeilen im V1-Format nicht als Kommentar."]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# --- Websites (P856) ------------------------------------------------------------------------
WEBSITE_STATUS_MEANING = {
    "neu": "erreichbar, an keinem anderen Item, Name im Seitentitel oder in der Adresse → `…_neu_…`",
    "pruefen": "erreichbar ohne Namensnachweis (oder nur Ortsname auf Gemeinde-Startseite), oder Umzug auf "
               "andere Domain mit Namensnachweis → `…_pruefen_…`, auskommentiert",
    "umgeleitet": "auf andere Domain umgeleitet, Name nicht im Titel – Bericht",
    "tot": "404/410, Domain unbekannt, Park- oder Fehlerseite – Bericht",
    "unklar": "401/403/429/5xx, Zeitüberschreitung, Bot-Schutz – Bericht, später erneut prüfen",
    "geteilt": "Adresse ist P856 eines anderen Items oder Kandidat für mehrere Items – Bericht",
    "widerspruch": "erreichbare Adressen auf verschiedenen Domains – Bericht",
    "aufgeloest": "P576 am Item oder GND-Satz aufgelöst – Bericht",
    "veraltet_in_wikidata": "Adresse steht am Item schon mit Rang „veraltet“ – Bericht",
    "ungueltig": "keine verwertbare Adresse (Leerzeichen, kaputte Umlaute, Datei, soziales Netzwerk) – Bericht",
}


def write_website_report(proposals, path: Path) -> None:
    """Eine Zeile je Item mit Quell-Adresse; `adressen` = Zustand jeder Quell-Adresse."""
    cols = ["status", "qid", "label", "wert", "sprache", "fundstellen", "nachweis", "seitentitel", "hinweis", "adressen"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cols)
        for p in proposals:
            w.writerow([p.status, p.qid, p.label, p.value, p.lang, " ".join(f"{s.source}:{s.key}" for s in p.sources),
                        p.evidence, p.title, p.note, " | ".join(f.describe() for f in p.urls)])


def write_website_summary(s: dict, path: Path) -> None:
    pct = lambda n, d: f"{n / d:.0%}" if d else "–"  # noqa: E731
    L = [
        f"# Websites (P856): {s['country']} ({s['date']})",
        "",
        f"- Pool (Museum/Bibliothek/Archiv): **{s['pool']}** Items, mit P856 {s['with_p856']} "
        f"({pct(s['with_p856'], s['pool'])}), ohne {s['without_p856']}",
        f"- ohne P856, aber mit Adresse in einer Quelle: **{s['items']}** Items, {s['urls']} geprüfte Adressen",
        "- Quell-Adressen: " + ", ".join(f"{k} {v}" for k, v in s["sources"].items()),
        f"- Vorschläge mit mehreren Quellen (je Quelle eine Fundstelle): {s['multi_source']}; "
        f"gleicher Vorschlag für mehrere Items → geteilt: {s['collisions']}",
        "- Namensnachweis: " + ", ".join(f"{k} {v}" for k, v in s["evidence"].items()),
        "- P407 (Sprache aus `<html lang>`, nur Landessprachen): " + ", ".join(f"{k} {v}" for k, v in s["p407"].items()),
        "",
        "## Status je Item",
        "",
        "| Status | Anzahl | Bedeutung |",
        "|---|---:|---|",
    ]
    L += [f"| {k} | {v} | {WEBSITE_STATUS_MEANING.get(k, '')} |" for k, v in s["status"].items()] or ["| – | 0 | |"]
    L += ["", "## Zustand der Quell-Adressen", "", "| Zustand | Anzahl |", "|---|---:|"]
    L += [f"| {k} | {v} |" for k, v in s["url_states"].items()]
    L += ["", "## Vorschläge nach erster Fundstelle", "", "| Quelle | neu | pruefen |", "|---|---:|---:|"]
    srcs = sorted(set(s["first_source"]["neu"]) | set(s["first_source"]["pruefen"]))
    L += [f"| {k} | {s['first_source']['neu'].get(k, 0)} | {s['first_source']['pruefen'].get(k, 0)} |" for k in srcs]
    L += ["", "## QuickStatements", ""] + ([f"- `{f}`" for f in s["files"]] or ["- keine"])
    L += ["", f"Bericht: `{s['report']}`.", "",
          "Mit `#` beginnende Zeilen (`…_pruefen_…`) sind auskommentiert: geprüfte Befehlszeilen entkommentieren,",
          "dann alle übrigen `#`-Zeilen entfernen (`grep -v '^#'`) und erst danach einspielen."]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
