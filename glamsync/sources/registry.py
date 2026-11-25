"""Register ohne Wikidata-Verknüpfung: Deutsche ISIL-Agentur (P791) und Museen in Österreich (P7917).

Beide Quellen enthalten weder QID noch GND. Die Zuordnung zu Wikidata erfolgt daher
über Name und Website (siehe glamsync/registry.py).

- ISIL: JSON-API https://isil.staatsbibliothek-berlin.de/api/org.jsonld?q=isil=<Präfix>-*
  (max. 100 Datensätze je Seite, CC0). Enthält nur aktive Sätze.
- Museen in Österreich: WordPress-API https://museen-in-oesterreich.at/wp-json/wp/v2/museum
  (ACF-Felder murenr, institution, ort, website, lat, long).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from ..http import CachedClient
from ..normalize import normalize_isil

log = logging.getLogger(__name__)

ISIL_API = "https://isil.staatsbibliothek-berlin.de/api/org.jsonld"
MURE_API = "https://museen-in-oesterreich.at/wp-json/wp/v2/museum"
# Schweizer ISIL-Verzeichnis (Nationalbibliothek, opendata.swiss, täglich aktualisiert)
ISIL_CH_CSV = "https://www.isil.nb.admin.ch/opendata/latest.csv"
ISIL_CH_DATASET = "https://opendata.swiss/de/dataset/schweizer-isil-verzeichnis"


@dataclass
class RegistryRecord:
    source: str  # "isil" | "mure"
    prop: str  # P791 | P7917
    value: str  # ISIL bzw. M-Nummer
    name: str
    alt_names: list[str] = field(default_factory=list)
    city: str = ""
    country: str = ""
    websites: list[str] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --- Deutsche ISIL-Agentur ------------------------------------------------------
def _subfields(fields: list) -> list[dict[str, str]]:
    """PICA-JSON: Liste von Feldwiederholungen → Liste von {Unterfeld: Wert}."""
    out = []
    for occ in fields or []:
        d: dict[str, str] = {}
        for sf in occ:
            if isinstance(sf, dict):
                for k, v in sf.items():
                    d.setdefault(k, v)
        out.append(d)
    return out


def _isil_record(m: dict) -> RegistryRecord | None:
    isil = normalize_isil(m.get("identifier"))
    if not isil:
        return None
    data = m.get("data") or {}
    names = [d.get("a", "") for d in _subfields(data.get("029@"))]
    websites = [d["u"] for d in _subfields(data.get("009Q")) if d.get("u") and d.get("z", "A") == "A"]
    lat = lon = None
    country = ""
    for d in _subfields(data.get("032P")):
        country = country or d.get("d", "")
        if "k" in d and "l" in d:
            try:
                lon, lat = float(d["k"]), float(d["l"])
                break
            except ValueError:
                pass
    return RegistryRecord(
        source="isil", prop="P791", value=isil, name=m.get("name", "").strip(),
        alt_names=[n for n in names if n], city=m.get("location", ""), country=country,
        websites=websites, lat=lat, lon=lon, url=f"https://w3id.org/isil/{isil}",
    )


def fetch_isil(client: CachedClient, prefix: str) -> list[RegistryRecord]:
    """Alle aktiven ISIL mit Präfix (z. B. 'DE', 'AT')."""
    records: list[RegistryRecord] = []
    page = 1
    while True:
        d = client.get_json(ISIL_API, params={"q": f"isil={prefix}-*", "size": 100, "page": page})
        members = d.get("member") or []
        records += [r for r in (_isil_record(m) for m in members) if r]
        pages = (d.get("view") or {}).get("numberOfPages") or 1
        if page % 20 == 0:
            log.info("ISIL %s: Seite %d/%d", prefix, page, pages)
        if not members or page >= pages:
            break
        page += 1
    log.info("ISIL %s: %d Datensätze", prefix, len(records))
    return records


def fetch_isil_ch(client: CachedClient, include_inactive: bool = False) -> list[RegistryRecord]:
    """Aktive Einträge des Schweizer ISIL-Verzeichnisses (CSV, ~2.500 Zeilen, davon ~1.900 aktiv).

    Die Berliner ISIL-Datenbank führt nur wenige CH-ISIL; die Schweizer Liste ist vollständig und
    enthält Namen in allen Landessprachen, Ort und Website (keine Koordinaten)."""
    import csv
    import io

    resp = client.session.get(ISIL_CH_CSV, timeout=120)
    resp.raise_for_status()
    rows = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
    records = []
    for r in rows:
        isil = normalize_isil(r.get("ISIL"))
        if not isil or (r.get("Status") != "aktiv" and not include_inactive):
            continue
        names = [r.get(k, "").strip() for k in ("Name-DE", "Name-FR", "Name-IT", "Name-RM", "Name-EN")]
        main = (r.get("Name-Original") or "").strip() or next((n for n in names if n), "")
        records.append(RegistryRecord(
            source="isil_ch", prop="P791", value=isil, name=main,
            alt_names=sorted({n for n in names if n and n != main}), city=(r.get("Ortschaft") or "").strip(),
            country="CH", websites=[w for w in [(r.get("Webseite") or "").strip()] if w], url=ISIL_CH_DATASET))
    log.info("ISIL CH (Nationalbibliothek): %d %s", len(records), "Einträge" if include_inactive else "aktive Einträge")
    return records


# --- Museen in Österreich -------------------------------------------------------
def _coord(v) -> float | None:
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else None
    except ValueError:
        return None


def fetch_mure(client: CachedClient) -> list[RegistryRecord]:
    records: list[RegistryRecord] = []
    page = 1
    while True:
        batch = client.get_json(MURE_API, params={"per_page": 100, "page": page}, allow_404=True)
        # WordPress liefert hinter der letzten Seite HTTP 400
        if not batch or not isinstance(batch, list):
            break
        for m in batch:
            acf = m.get("acf") or {}
            nr = (acf.get("murenr") or "").strip().upper()
            if not nr:
                continue
            records.append(RegistryRecord(
                source="mure", prop="P7917", value=nr, name=(acf.get("institution") or "").strip(),
                city=acf.get("ort") or "", country="AT",
                websites=[acf["website"]] if acf.get("website") else [],
                lat=_coord(acf.get("lat")), lon=_coord(acf.get("long")), url=m.get("link", ""),
            ))
        if len(batch) < 100:
            break
        page += 1
    log.info("Museen in Österreich: %d Datensätze", len(records))
    return records
