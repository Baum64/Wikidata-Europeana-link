"""GND-Umlenkungen und -Löschungen aus der monatlichen DNB-Gesamtdatei.

Datei: authorities-gnd_umlenk_loesch_<datum>.jsonld.gz auf https://data.dnb.de/opendata/ (CC0),
lokal unter GND_REDIRECTS_PATH (sonst die neueste solche Datei neben dem GND-Dump).

Aufbau (siehe README „Zeitraum, Umlenkungen und Löschungen“): ein JSON-Array, meist ein Objekt
je Zeile; gelöschte Sätze stehen teils mit dem nächsten Objekt in derselben Zeile.
  Umlenkung:  {"@id": alte GND, owl:sameAs / dnb:canonicalUri: neue GND}
  Löschung:   {"@id": alte GND, "@type": [dnb:DeletedResource]}
Die Datei nennt weder Entitätstyp noch Datum. Jede Umlenkung hat genau ein Ziel; Ketten
(Ziel selbst umgelenkt) kommen vor und werden bis zum Endziel aufgelöst.

Gespeichert wird eine Tabelle redirect(old_id, new_id, kind) in derselben SQLite-Datei wie die
GND-Einrichtungen; defekte Objekte werden übersprungen, gezählt und geloggt.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..normalize import normalize_gnd

log = logging.getLogger(__name__)

GND = "https://d-nb.info/gnd/"
SAMEAS = "http://www.w3.org/2002/07/owl#sameAs"
CANONICAL = "https://d-nb.info/standards/elementset/dnb#canonicalUri"
DELETED = "https://d-nb.info/standards/elementset/dnb#DeletedResource"

REDIRECTED = "umgelenkt"
DELETED_KIND = "geloescht"
FILE_GLOB = "authorities-gnd_umlenk_loesch*.jsonld.gz"

_SKIP = " \n\r\t,[]"


@dataclass(frozen=True)
class Redirect:
    old_id: str
    new_id: str | None  # direktes Ziel; None bei Löschung
    kind: str  # umgelenkt | geloescht


def _id(uri: str) -> str:
    return normalize_gnd(uri) or uri.rsplit("/", 1)[-1]


def parse_object(o: dict) -> Redirect | None:
    """Ein JSON-LD-Objekt → Redirect; None, wenn es weder Umlenkung noch Löschung ist."""
    old = _id(o.get("@id", ""))
    if not old:
        return None
    if DELETED in (o.get("@type") or []):
        return Redirect(old, None, DELETED_KIND)
    targets = {_id(x["@id"]) for x in o.get(SAMEAS, []) if isinstance(x, dict) and x.get("@id")}
    targets |= {_id(x["@value"]) for x in o.get(CANONICAL, []) if isinstance(x, dict) and x.get("@value")}
    targets.discard(old)
    if len(targets) != 1:  # Aufspaltung (mehrere Ziele) kommt in der Datei nicht vor; nie automatisch
        return None if not targets else Redirect(old, "|".join(sorted(targets)), REDIRECTED)
    return Redirect(old, targets.pop(), REDIRECTED)


def iter_redirects(path: Path, stats: Counter, chunk: int = 1 << 20, max_obj: int = 1 << 16) -> Iterator[Redirect]:
    """Liest die Datei streamend (raw_decode); defekte Objekte werden übersprungen und gezählt."""
    dec = json.JSONDecoder()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        buf, eof = "", False
        while not eof:
            data = fh.read(chunk)
            eof = not data
            buf += data
            i = 0
            while True:
                while i < len(buf) and buf[i] in _SKIP:
                    i += 1
                if i >= len(buf):
                    buf = ""
                    break
                try:
                    o, j = dec.raw_decode(buf, i)
                except json.JSONDecodeError:
                    if not eof and len(buf) - i < max_obj:  # Objekt unvollständig: mehr lesen
                        buf = buf[i:]
                        break
                    # defekt (am Dateiende oder länger als jedes echte Objekt) → zum nächsten Objekt
                    nxt = buf.find('{"@id"', i + 1)
                    stats["defekt_objekt"] += 1
                    log.warning("Umlenkungsdatei: defektes Objekt bei Zeichen %d übersprungen", i)
                    if nxt < 0:
                        buf = ""
                        break
                    i = nxt
                    continue
                i = j
                r = parse_object(o) if isinstance(o, dict) else None
                if r is None:
                    stats["unbekanntes_objekt"] += 1
                    continue
                stats[r.kind] += 1
                yield r


def resolve(redirects: dict[str, Redirect], gnd: str) -> tuple[str | None, str]:
    """Endziel einer alten GND über Ketten: (neue GND oder None, Art).

    Endet die Kette in einer Löschung → (None, geloescht). Zyklen brechen ab (Art umgelenkt, letztes Ziel).
    """
    seen = {gnd}
    r = redirects.get(gnd)
    if r is None:
        return gnd, ""
    while r.kind == REDIRECTED and r.new_id and r.new_id in redirects and r.new_id not in seen:
        seen.add(r.new_id)
        r = redirects[r.new_id]
    return (None, DELETED_KIND) if r.kind == DELETED_KIND else (r.new_id, REDIRECTED)


def file_date(path: Path | None) -> str:
    """Stand-Datum aus dem Dateinamen (…_20261113.jsonld.gz → 2026-11-13)."""
    m = re.search(r"_(\d{4})(\d{2})(\d{2})\.", path.name if path else "")
    return "-".join(m.groups()) if m else ""


def find_file(dump: Path | None) -> Path | None:
    hits = sorted(dump.parent.glob(FILE_GLOB)) if dump else []
    return hits[-1] if hits else None


# --- SQLite ------------------------------------------------------------------------------
def build(path: Path, db: Path) -> dict:
    stats: Counter = Counter()
    rows = [(r.old_id, r.new_id, r.kind) for r in iter_redirects(path, stats)]
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE IF EXISTS redirect")
        con.execute("CREATE TABLE redirect (old_id TEXT PRIMARY KEY, new_id TEXT, kind TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany("INSERT OR REPLACE INTO redirect VALUES (?, ?, ?)", rows)
        meta = {"redirects": path.name, "redirects_mtime": str(path.stat().st_mtime),
                "redirects_built": time.strftime("%Y-%m-%d %H:%M"), "redirects_stats": json.dumps(dict(stats))}
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", meta.items())
        con.commit()
    finally:
        con.close()
    log.info("GND-Umlenkungen %s: %s", path.name, dict(stats))
    return dict(stats)


def ensure(path: Path, db: Path, rebuild: bool = False) -> dict:
    """Füllt die Tabelle redirect neu, wenn sie fehlt, die Datei neuer ist oder rebuild=True."""
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        meta = dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()
    if (not rebuild and meta.get("redirects") == path.name
            and float(meta.get("redirects_mtime", 0)) >= path.stat().st_mtime):
        return json.loads(meta.get("redirects_stats", "{}"))
    return build(path, db)


def load(db: Path) -> dict[str, Redirect]:
    con = sqlite3.connect(db)
    try:
        return {o: Redirect(o, n, k) for o, n, k in con.execute("SELECT old_id, new_id, kind FROM redirect")}
    finally:
        con.close()
