"""GND: lobid-Prüfung einzelner Personen-GND und Einlesen des DNB-Dumps (Körperschaften).

1. lobid-gnd (https://lobid.org/gnd), ohne Schlüssel: Vor einem P227-Vorschlag für Personen
   wird geprüft, ob die GND existiert, eine Person (DifferentiatedPerson) beschreibt und nicht
   umgelenkt wurde.

2. DNB-Gesamtabzüge (https://data.dnb.de/opendata/, CC0), lokal unter GND_DUMP_PATH:
   - authorities-gnd-koerperschaft_lds_<datum>.jsonld.gz  (Körperschaften)
   - authorities-gnd-sachbegriff_lds_<datum>.jsonld.gz    (Typ über die Sachbegriff-Hierarchie)
   - authorities-gnd-geografikum_lds_<datum>.jsonld.gz    (Ortsnamen, Wikidata-QID, Koordinaten)
   Jeder Dump ist EIN eingerücktes JSON-Array flacher JSON-LD-Knoten ("[ [ {…}, {…} ] ]").
   Die Knoten werden einzeln aus einem Puffer gelesen (json.JSONDecoder.raw_decode), defekte
   Knoten/Sätze werden übersprungen, gezählt und geloggt. Ergebnis ist eine SQLite-Tabelle
   mit allen Museen, Bibliotheken und Archiven (alle Länder).
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import requests

from ..http import CachedClient
from ..normalize import gnd_checksum_ok, normalize_gnd, qid_from_uri

log = logging.getLogger(__name__)

LOBID = "https://lobid.org/gnd"


# =============================================================================
# 1. lobid-Prüfung (Personen)
# =============================================================================
def _compact(d: dict) -> dict:
    return {"id": normalize_gnd(d.get("id") or "") or d.get("gndIdentifier"),
            "types": d.get("type") or [], "name": d.get("preferredName") or ""}


def gnd_info(client: CachedClient, gnd: str) -> dict | None:
    """{id, types, name}; None, wenn die GND nicht existiert; {"error": …}, wenn lobid scheitert.

    lobid antwortet auf unbekannte IDs teils mit HTTP 500 statt 404 – das gilt als
    „nicht prüfbar“, sodass kein Vorschlag entsteht.
    """
    try:
        return client.get_json(f"{LOBID}/{gnd}.json", allow_404=True, transform=_compact, cache_tag=":compact")
    except requests.HTTPError as exc:
        return {"error": exc.response.status_code if exc.response is not None else "?"}


def gnd_problem(gnd: str, info: dict | None) -> str | None:
    """None = GND ist eine gültige, aktuelle Personen-GND; sonst Begründung."""
    if info is None:
        return "GND existiert nicht (lobid 404)"
    if "error" in info:
        return f"GND nicht prüfbar (lobid HTTP {info['error']})"
    if info.get("id") and info["id"] != gnd:
        return f"GND umgelenkt auf {info['id']}"
    if "DifferentiatedPerson" not in info.get("types", []):
        return f"keine Person in der GND ({', '.join(info.get('types', [])) or 'unbekannt'})"
    return None


# =============================================================================
# 2. DNB-Dump
# =============================================================================
GND = "https://d-nb.info/gnd/"
GNDO = "https://d-nb.info/standards/elementset/gnd#"
SAMEAS = "http://www.w3.org/2002/07/owl#sameAs"
DEPRECATED = "https://d-nb.info/standards/elementset/dnb#deprecatedUri"
GEOMETRY = "http://www.opengis.net/ont/geosparql#hasGeometry"
WKT = "http://www.opengis.net/ont/geosparql#asWKT"

# Typfilter: Ein Satz gilt als Körperschaft, wenn er mindestens einen dieser @type-Werte hat
# und keinen der ausgeschlossenen. Gebietskörperschaften, Kongresse, Bauwerke, fiktive
# Körperschaften usw. sind keine Einrichtungen im Sinne des Abgleichs.
CORPORATE_TYPES = frozenset({
    "CorporateBody", "OrganOfCorporateBody", "Company", "MusicalCorporateBody",
    "ReligiousCorporateBody", "ReligiousAdministrativeUnit", "ProjectOrProgram",
})
EXCLUDED_TYPES = frozenset({
    "FictiveCorporateBody", "TerritorialCorporateBodyOrAdministrativeUnit", "AdministrativeUnit",
    "ConferenceOrEvent", "SeriesOfConferenceOrEvent", "BuildingOrMemorial", "PlaceOrGeographicName",
    "DifferentiatedPerson", "Family", "Work", "Manuscript", "Collection",
})

# Wurzel-Sachbegriffe: werden per exaktem Namen im Sachbegriff-Dump gesucht (nicht vorgegeben).
# Am 21.11.2026: Museum 4040795-0, Bibliothek 4006439-6, Archiv 4002859-8.
ROOT_NAMES = {"museum": "Museum", "library": "Bibliothek", "archive": "Archiv"}
# „ist Unterbegriff von“ (ohne broaderTermPartitive = „ist Teil von“)
BROADER_SUBJECT = (GNDO + "broaderTermGeneral", GNDO + "broaderTermGeneric")

# geographicAreaCode → Land im Sinne des Abgleichs. Bedeutung laut GND-Vokabular (lobid, 21.11.2026):
#   XA-DE = Deutschland, XA-DDDE = Deutschland (DDR), XA-DXDE = Deutschland, Deutsches Reich (bis 1945).
# DDR und Deutsches Reich zählen bei --country Q183 mit, bekommen aber eigene Ausgabedateien
# (Einrichtungen des Deutschen Reichs können heute außerhalb Deutschlands liegen, z. B. Königsberg).
AREA_COUNTRY = (("XA-DE", "DE"), ("XA-DDDE", "DDR"), ("XA-DXDE", "DR"), ("XA-AT", "AT"), ("XA-CH", "CH"))
COUNTRY_GROUPS = {"Q183": ("DE", "DDR", "DR"), "Q40": ("AT",), "Q39": ("CH",)}


# --- Parser -------------------------------------------------------------------
def iter_nodes(path: Path, stats: Counter, chunk: int = 1 << 20, max_node: int = 8 << 20) -> Iterator[dict]:
    """Liefert die JSON-LD-Knoten eines Dumps einzeln (streamend).

    Defekte Knoten werden übersprungen und in `stats` gezählt; danach wird am nächsten
    Knotenanfang (Zeile "}, {" in Spalte 0) weitergelesen.
    """
    dec = json.JSONDecoder()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        buf, eof = "", False
        while True:
            i, n = 0, len(buf)
            while i < n and buf[i] in " \t\r\n[],":
                i += 1
            buf = buf[i:]
            if not buf:
                if eof:
                    return
                data = fh.read(chunk)
                eof = not data
                buf = data
                continue
            if buf[0] != "{":
                stats["defekt_knoten"] += 1
                log.warning("Unerwartetes Zeichen im Dump übersprungen: %r", buf[:80])
                buf = _resync(buf)
                continue
            try:
                obj, end = dec.raw_decode(buf)
            except json.JSONDecodeError:
                if not eof and len(buf) < max_node:
                    data = fh.read(chunk)
                    eof = not data
                    buf += data
                    continue
                stats["defekt_knoten"] += 1
                log.warning("Defekter Knoten übersprungen: %r", buf[:120])
                buf = _resync(buf)
                continue
            buf = buf[end:]
            if isinstance(obj, dict):
                yield obj
            else:
                stats["defekt_knoten"] += 1


def _resync(buf: str) -> str:
    j = buf.find("\n}, {", 1)
    return buf[j + 4 :] if j >= 0 else ""


def _ids(node: dict, pred: str) -> list[str]:
    return [v["@id"] for v in node.get(pred, []) if isinstance(v, dict) and isinstance(v.get("@id"), str)]


def _vals(node: dict, pred: str) -> list[str]:
    return [str(v["@value"]) for v in node.get(pred, []) if isinstance(v, dict) and "@value" in v]


def _short(uri: str) -> str:
    return uri.split("#", 1)[-1] if "#" in uri else uri.rstrip("/").rsplit("/", 1)[-1]


def _gnd(uri: str) -> str:
    return normalize_gnd(uri) or _short(uri)


def is_record(node: dict) -> bool:
    nid = node.get("@id", "")
    return isinstance(nid, str) and nid.startswith(GND) and not nid.endswith("/about")


def is_corporate(types: Iterable[str]) -> bool:
    """Typfilter: mindestens ein Körperschaftstyp, kein ausgeschlossener Typ."""
    ts = set(types)
    return bool(ts & CORPORATE_TYPES) and not ts & EXCLUDED_TYPES


def country_of(area_codes: Iterable[str]) -> str:
    """'XA-DE-BY' → 'DE', 'XA-DDDE' → 'DDR', 'XA-DXDE' → 'DR'; Vorrang in dieser Reihenfolge; sonst ''."""
    codes = list(area_codes)
    for prefix, land in AREA_COUNTRY:
        if any(c == prefix or c.startswith(prefix + "-") for c in codes):
            return land
    return ""


_WKT_RE = re.compile(r"Point\s*\(\s*([+-]?[\d.]+)\s+([+-]?[\d.]+)\s*\)", re.I)


def parse_wkt(value: str) -> tuple[float, float] | None:
    """'Point ( +009.993682 +053.551086 )' → (lat, lon)."""
    m = _WKT_RE.search(value or "")
    return (float(m.group(2)), float(m.group(1))) if m else None


# --- Hilfsdumps -----------------------------------------------------------------
def load_subjects(path: Path, stats: Counter | None = None) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Sachbegriffe: GND → Name, GND → direkte Oberbegriffe (allgemein/generisch)."""
    stats = stats if stats is not None else Counter()
    labels: dict[str, str] = {}
    parents: dict[str, set[str]] = defaultdict(set)
    for node in iter_nodes(path, stats):
        if not is_record(node):
            continue
        g = _gnd(node["@id"])
        labels[g] = (_vals(node, GNDO + "preferredNameForTheSubjectHeading") or ["?"])[0]
        for p in BROADER_SUBJECT:
            parents[g].update(_gnd(u) for u in _ids(node, p))
    return labels, parents


def subject_types(labels: dict[str, str], parents: dict[str, set[str]]) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Wurzeln per exaktem Namen → ({typ: wurzel-gnd}, {sachbegriff: {typen}}) über die Hierarchie."""
    roots: dict[str, str] = {}
    for typ, name in ROOT_NAMES.items():
        hits = [g for g, l in labels.items() if l == name]
        if len(hits) != 1:
            raise ValueError(f"Wurzelbegriff {name!r} nicht eindeutig im Sachbegriff-Dump: {hits}")
        roots[typ] = hits[0]
    root_type = {g: t for t, g in roots.items()}
    memo: dict[str, frozenset[str]] = {}

    def types_of(g: str, path: frozenset[str]) -> frozenset[str]:
        if g in memo:
            return memo[g]
        if g in path:  # Zyklus in der Hierarchie
            return frozenset()
        res = {root_type[g]} if g in root_type else set()
        for p in parents.get(g, ()):
            res |= types_of(p, path | {g})
        memo[g] = frozenset(res)
        return memo[g]

    return roots, {g: set(t) for g in labels if (t := types_of(g, frozenset()))}


@dataclass
class Place:
    gnd: str
    name: str
    qid: str | None = None
    lat: float | None = None
    lon: float | None = None


def load_places(path: Path, stats: Counter | None = None) -> dict[str, Place]:
    """Geografika: GND → Place (Name, Wikidata-QID, Koordinaten aus den leeren Geometrie-Knoten)."""
    stats = stats if stats is not None else Counter()
    geometry: dict[str, tuple[float, float]] = {}
    out: dict[str, Place] = {}
    pending: dict[str, str] = {}  # Geometrie-Knoten, die erst nach dem Ort kommen
    for node in iter_nodes(path, stats):
        nid = node.get("@id", "")
        if isinstance(nid, str) and nid.startswith("_:"):
            for w in _vals(node, WKT):
                if (c := parse_wkt(w)) is not None:
                    geometry[nid] = c
                    if nid in pending:
                        p = out[pending.pop(nid)]
                        p.lat, p.lon = c
            continue
        if not is_record(node):
            continue
        g = _gnd(nid)
        qids = [q for u in _ids(node, SAMEAS) if "wikidata.org/entity/" in u and (q := qid_from_uri(u))]
        p = Place(g, (_vals(node, GNDO + "preferredNameForThePlaceOrGeographicName") or [""])[0],
                  qids[0] if len(qids) == 1 else None)
        for geo in _ids(node, GEOMETRY):
            if geo in geometry:
                p.lat, p.lon = geometry[geo]
            else:
                pending[geo] = g
        out[g] = p
    return out


# --- Körperschaften ---------------------------------------------------------------
# --- Zeitraum ---------------------------------------------------------------------------
# Formate im Dump (siehe README „Zeitraum, Umlenkungen und Löschungen“): „1606“, „15.09.1883“,
# „XX.05.1952“, „19XX“, „ca. 1972-2002“, „19. Jh.“ … Ein genaues Jahr gibt es nur bei genau EINEM
# Wert in Form eines drei- oder vierstelligen Jahres oder eines Datums mit ausgeschriebenem Jahr.
# Das kombinierte Feld dateOfEstablishmentAndTermination ist Freitext und gilt immer als unscharf.
_EXACT_YEAR = re.compile(r"(?:(?:\d{1,2}|XX)\.(?:\d{1,2}|XX)\.)?(\d{3,4})")
_RANGE_END = re.compile(r"\d\s*-\s*\S|\bbis\b\s*\S")


def exact_year(values: list[str]) -> int | None:
    """Jahr, falls genau ein Wert mit eindeutigem Jahr vorliegt („1606“, „15.09.1883“, „XX.05.1952“)."""
    if len(values) != 1:
        return None
    m = _EXACT_YEAR.fullmatch(values[0].strip())
    return int(m.group(1)) if m else None


def parse_period(est: list[str], term: list[str], comb: list[str]) -> dict:
    """Zeitangaben → {start_year, end_year, period_raw, closed}.

    closed: Ende vorhanden (dateOfTermination) oder ein Bereich mit Ende im kombinierten Feld
    („ca. 1972-2002“, „… bis …“). Ein fehlendes Ende beweist NICHT, dass die Einrichtung besteht.
    """
    est, term, comb = ([v.strip() for v in vs if v and v.strip()] for vs in (est, term, comb))
    raw = []
    if est or term:
        raw.append(f"{' / '.join(est)}–{' / '.join(term)}")
    raw += comb
    return {"start_year": exact_year(est), "end_year": exact_year(term), "period_raw": "; ".join(raw),
            "closed": bool(term) or any(_RANGE_END.search(c) for c in comb)}


def _nfc(v):
    """Texte (auch in Listen/Dicts) in Unicode-NFC."""
    if isinstance(v, str):
        return unicodedata.normalize("NFC", v)
    if isinstance(v, list):
        return [_nfc(x) for x in v]
    if isinstance(v, dict):
        return {k: _nfc(x) for k, x in v.items()}
    return v


@dataclass
class GndBody:
    """Eine Körperschaft (Museum/Bibliothek/Archiv) aus dem GND-Dump, normalisiert."""

    gnd: str
    name: str
    variants: list[str] = field(default_factory=list)  # Variant- und Kurzformen
    kinds: list[str] = field(default_factory=list)  # @type, z. B. CorporateBody
    types: list[str] = field(default_factory=list)  # museum | library | archive
    broader: list[str] = field(default_factory=list)  # Namen der Oberbegriffe, z. B. Kunstmuseum
    country: str = ""  # DE | DDR | DR (Deutsches Reich) | AT | CH | ''
    areas: list[str] = field(default_factory=list)
    places: list[dict] = field(default_factory=list)  # asdict(Place)
    homepages: list[str] = field(default_factory=list)
    wikidata: list[str] = field(default_factory=list)
    viaf: list[str] = field(default_factory=list)
    isni: list[str] = field(default_factory=list)
    established: str = ""
    terminated: str = ""
    superior: list[dict] = field(default_factory=list)  # [{gnd, name}]
    old_ids: list[str] = field(default_factory=list)  # zusammengeführte (umgelenkte) GND-IDs
    # Zeitraum (siehe parse_period); start/end nur bei genauem Jahr
    start_year: int | None = None
    end_year: int | None = None
    period_raw: str = ""
    closed: bool = False
    predecessors: list[str] = field(default_factory=list)  # gndo:precedingCorporateBody
    successors: list[str] = field(default_factory=list)  # Sätze, die diesen als Vorgänger nennen
    historical: bool = False  # abgeschlossen UND ohne Nachfolger (wirklich aufgelöst)

    def __post_init__(self) -> None:
        # Die DNB liefert Umlaute zerlegt (NFD: „a“ + Trema). Für Vergleich, Mix'n'match und
        # Wikidata wird alles in die übliche zusammengesetzte Form (NFC) gebracht.
        for f in fields(self):
            setattr(self, f.name, _nfc(getattr(self, f.name)))

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def place_names(self) -> list[str]:
        return [p["name"] for p in self.places if p.get("name")]

    @property
    def period_status(self) -> str:
        """„historisch“ (aufgelöst), „vorgaenger“ (frühere Namensform mit Nachfolgesatz) oder „“."""
        if not self.closed:
            return ""
        return "vorgaenger" if self.successors else "historisch"


def parse_body(node: dict, subj_types: dict[str, set[str]], subj_labels: dict[str, str],
               places: dict[str, Place]) -> GndBody | None:
    """Knoten → GndBody, falls Körperschaft (Typfilter) UND Museum/Bibliothek/Archiv; sonst None."""
    kinds = [_short(t) for t in node.get("@type", []) if isinstance(t, str)]
    if not is_corporate(kinds):
        return None
    broader = [_gnd(b) for b in _ids(node, GNDO + "broaderTermInstantial")]
    types = sorted({t for b in broader for t in subj_types.get(b, ())})
    if not types:
        return None
    gnd = normalize_gnd((_vals(node, GNDO + "gndIdentifier") or [""])[0]) or _gnd(node["@id"])
    same = _ids(node, SAMEAS)
    areas = [_short(a) for a in _ids(node, GNDO + "geographicAreaCode")]
    pl = [asdict(places.get(g) or Place(g, "")) for g in (_gnd(u) for u in _ids(node, GNDO + "placeOfBusiness"))]
    return GndBody(
        gnd=gnd,
        name=(_vals(node, GNDO + "preferredNameForTheCorporateBody") or [""])[0],
        variants=_vals(node, GNDO + "variantNameForTheCorporateBody")
        + _vals(node, GNDO + "abbreviatedNameForTheCorporateBody"),
        kinds=kinds,
        types=types,
        broader=[subj_labels.get(b, b) for b in broader if b in subj_types],
        country=country_of(areas),
        areas=areas,
        places=pl,
        homepages=_ids(node, GNDO + "homepage"),
        wikidata=[q for u in same if "wikidata.org/entity/" in u and (q := qid_from_uri(u))],
        viaf=[_short(u) for u in same if "viaf.org/viaf/" in u],
        isni=[_short(u) for u in same if "isni.org/isni/" in u],
        established=(_vals(node, GNDO + "dateOfEstablishment") or [""])[0],
        terminated=(_vals(node, GNDO + "dateOfTermination") or [""])[0],
        superior=[{"gnd": _gnd(u)} for u in _ids(node, GNDO + "hierarchicalSuperiorOfTheCorporateBody")],
        old_ids=sorted({g for u in _vals(node, DEPRECATED) if (g := normalize_gnd(u))} - {gnd}),
        predecessors=[_gnd(u) for u in _ids(node, GNDO + "precedingCorporateBody")],
        **parse_period(_vals(node, GNDO + "dateOfEstablishment"), _vals(node, GNDO + "dateOfTermination"),
                       _vals(node, GNDO + "dateOfEstablishmentAndTermination")),
    )


def sibling_dump(path: Path, kind: str) -> Path:
    hits = sorted(path.parent.glob(f"authorities-gnd-{kind}_lds*.jsonld.gz"))
    if not hits:
        raise FileNotFoundError(f"authorities-gnd-{kind}_lds_<datum>.jsonld.gz fehlt in {path.parent} (siehe README)")
    return hits[-1]


def read_bodies(path: Path, stats: Counter) -> Iterator[GndBody]:
    """Liest Körperschafts-, Sachbegriff- und Geografikum-Dump; liefert Museen/Bibliotheken/Archive."""
    subj_labels, parents = load_subjects(sibling_dump(path, "sachbegriff"), stats)
    roots, subj_types = subject_types(subj_labels, parents)
    stats.update({f"wurzel_{t}={g}": 1 for t, g in roots.items()})
    places = load_places(sibling_dump(path, "geografikum"), stats)
    names: dict[str, str] = {}
    successors: dict[str, set[str]] = defaultdict(set)  # Vorgänger → Nachfolger (über ALLE Körperschaften)
    bodies: list[GndBody] = []
    t0 = time.time()
    for node in iter_nodes(path, stats):
        if not is_record(node):
            continue
        stats["datensaetze"] += 1
        try:
            gid = _gnd(node["@id"])
            names[gid] = (_vals(node, GNDO + "preferredNameForTheCorporateBody") or [""])[0]
            for u in _ids(node, GNDO + "precedingCorporateBody"):
                successors[_gnd(u)].add(gid)
            body = parse_body(node, subj_types, subj_labels, places)
        except Exception as exc:  # defekter Satz: zählen, loggen, weiter
            stats["defekt_datensatz"] += 1
            log.warning("GND-Satz %s nicht auswertbar: %s", node.get("@id"), exc)
            continue
        if body is None:
            continue
        if not gnd_checksum_ok(body.gnd):
            stats["formatfehler_gnd_id"] += 1
            log.warning("GND-ID %s mit falscher Prüfziffer (%s)", body.gnd, body.name)
        bodies.append(body)
        if stats["datensaetze"] % 250_000 == 0:
            log.info("GND-Dump: %d Sätze, %.0f s", stats["datensaetze"], time.time() - t0)
    for b in bodies:  # Namen übergeordneter Körperschaften (können später im Dump stehen)
        for s in b.superior:
            s["name"] = names.get(s["gnd"], "")
        # Nachfolger: Die GND kennt nur „Vorgänger“; ein Ende mit Nachfolgesatz ist meist eine
        # Umbenennung/Umbildung, keine Auflösung.
        b.successors = sorted(successors.get(b.gnd, ()))
        b.historical = b.closed and not b.successors
        stats["abgeschlossen"] += b.closed
        stats["historisch"] += b.historical
    yield from bodies


# --- SQLite-Tabelle -------------------------------------------------------------------
TABLE_COLUMNS = [f for f in GndBody.__dataclass_fields__]
JSON_COLUMNS = {"variants", "kinds", "types", "broader", "areas", "places", "homepages", "wikidata", "viaf",
                "isni", "superior", "old_ids", "start_year", "end_year", "closed", "predecessors", "successors",
                "historical"}
# Bei geänderten Spalten wird die Tabelle beim nächsten Lauf neu gebaut
SCHEMA_VERSION = "2"


def build_table(dump: Path, db: Path) -> dict:
    """Dump → SQLite (Tabelle body + meta). Liefert die Zähler."""
    stats: Counter = Counter()
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    cols = ", ".join(f"{c} TEXT" + (" PRIMARY KEY" if c == "gnd" else "") for c in TABLE_COLUMNS)
    con.execute(f"CREATE TABLE body ({cols})")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    rows = []
    for b in read_bodies(dump, stats):
        d = b.to_dict()
        rows.append([json.dumps(d[c], ensure_ascii=False) if c in JSON_COLUMNS else d[c] for c in TABLE_COLUMNS])
    con.executemany(f"INSERT OR REPLACE INTO body VALUES ({', '.join('?' * len(TABLE_COLUMNS))})", rows)
    stats["einrichtungen"] = len(rows)
    meta = {"dump": dump.name, "dump_mtime": str(dump.stat().st_mtime), "built": time.strftime("%Y-%m-%d %H:%M"),
            "schema": SCHEMA_VERSION,
            "stats": json.dumps(dict(stats), ensure_ascii=False)}
    con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    con.commit()
    con.close()
    tmp.replace(db)
    log.info("GND-Tabelle %s: %d Einrichtungen", db, len(rows))
    return dict(stats)


def table_meta(db: Path) -> dict:
    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()
    meta["stats"] = json.loads(meta.get("stats", "{}"))
    return meta


def load_table(db: Path, countries: Iterable[str] | None = None) -> list[GndBody]:
    con = sqlite3.connect(db)
    try:
        sql = f"SELECT {', '.join(TABLE_COLUMNS)} FROM body"
        args: list[str] = []
        if countries:
            countries = list(countries)
            sql += f" WHERE country IN ({', '.join('?' * len(countries))})"
            args = countries
        out = []
        for row in con.execute(sql + " ORDER BY gnd", args):
            d = {c: (json.loads(v) if c in JSON_COLUMNS else v) for c, v in zip(TABLE_COLUMNS, row)}
            out.append(GndBody(**d))
        return out
    finally:
        con.close()


def ensure_table(dump: Path, db: Path, rebuild: bool = False) -> dict:
    """Baut die Tabelle neu, wenn sie fehlt, älter als der Dump ist, ein altes Schema hat oder rebuild=True."""
    if not rebuild and db.is_file():
        meta = table_meta(db)
        if (meta.get("dump") == dump.name and float(meta.get("dump_mtime", 0)) >= dump.stat().st_mtime
                and meta.get("schema") == SCHEMA_VERSION):
            return meta["stats"]
    log.info("Lese GND-Dump %s (dauert einige Minuten) …", dump.name)
    return build_table(dump, db)
