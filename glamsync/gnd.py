"""Abgleich GND-Körperschaften (Museen, Bibliotheken, Archive) ↔ Wikidata über Name und Ort.

Ohne Netz und getestet. Für P227 entstehen KEINE QuickStatements: Die Wikidata-Links der GND sind
Rückverweise auf vorhandene P227-Aussagen, und die GND enthält keine ISIL (siehe README).
Ergebnis sind eine Kandidatenliste zur Handprüfung und die Mix'n'match-Dateien.
Zusätzlich (Befehl `labels`): deutsche Labels, Beschreibungen und Aliase für Items, deren P227
schon auf eine Einrichtung der Tabelle zeigt (QuickStatements Lde/Dde/Ade).

Status je GND-Einrichtung:
  vorhanden         GND steht schon an genau einem Item (P227)
  vorhanden_alt     eine umgelenkte (alte) GND dieser Einrichtung steht an einem Item → Bericht
  mehrdeutig        GND an mehreren Items, mehrere starke Kandidaten, oder ein Item ist starker
                    Kandidat für mehrere GND-Einrichtungen → Bericht
  kandidat_stark    genau ein Item mit GLEICHEM Namen UND GLEICHEM Ort, ohne P227 → CSV (Handprüfung)
  konflikt          der starke Kandidat hat schon eine andere P227 → Bericht
  kandidat_schwach  Name gleich, Ort abweichend/unbekannt; Name ähnlich + Ort gleich;
                    oder Website gleich + Name ähnlich → CSV
  kein_treffer

Mix'n'match: alle Einrichtungen, deren GND (aktuell oder alt) in Wikidata nicht vorkommt.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from .lang import foreign_reason, prime, words_of
from .models import WikidataItem
from .normalize import check, gnd_checksum_ok
from .registry import Pool, norm_name, norm_url
from .sources.gnd import GndBody

VORHANDEN = "vorhanden"
VORHANDEN_ALT = "vorhanden_alt"
MEHRDEUTIG = "mehrdeutig"
STARK = "kandidat_stark"
SICHER = "kandidat_sicher"  # Ort gleich und Name sehr ähnlich zum Label → QuickStatement, nicht in Mix'n'match
KONFLIKT = "konflikt"
SCHWACH = "kandidat_schwach"
KEIN_TREFFER = "kein_treffer"
HISTORISCH = "historisch"  # abgeschlossen, ohne Nachfolger → keine Kandidatensuche, nicht in Mix'n'match
VORGAENGER = "vorgaenger"  # abgeschlossen, mit Nachfolgesatz (frühere Namensform) → ebenso
GND_UMGELENKT = "gnd_umgelenkt"  # die GND selbst ist nach dem Dump-Stand umgelenkt worden
GND_GELOESCHT = "gnd_geloescht"  # … bzw. gelöscht
STATUSES = [VORHANDEN, VORHANDEN_ALT, SICHER, STARK, SCHWACH, KONFLIKT, MEHRDEUTIG, KEIN_TREFFER,
            HISTORISCH, VORGAENGER, GND_UMGELENKT, GND_GELOESCHT]
NOT_FOR_MNM = {GND_UMGELENKT, GND_GELOESCHT, SICHER}

# Quelle für Datums- und Korrektur-QuickStatements: GND (Q36578, über P227 → P9073 ermittelt)
GND_ITEM = "Q36578"

SURE_LABEL_RATIO = 0.95  # Ähnlichkeit GND-Name ↔ Item-Label (kompakt) für kandidat_sicher
PLACE_KM = 10.0  # GND-Orte sind Ortsmittelpunkte; Einrichtungen liegen meist im Umkreis
WEAK_NAME_RATIO = 0.85
WEB_NAME_RATIO = 0.6

# Mix'n'match: Standardtyp je Datei (Spalte „type“); dieselben Klassen wie im Wikidata-Pool
MNM_TYPE = {"museum": "Q33506", "library": "Q7075", "archive": "Q166118"}
MNM_DESC_MAX = 255
COUNTRY_NOTE = {"DDR": "DDR", "DR": "Deutsches Reich"}


# --- Namen ------------------------------------------------------------------------
_PAREN_TAIL = re.compile(r"\s*\(([^()]*(?:\([^()]*\))?[^()]*)\)\s*$")


def body_names(b: GndBody) -> tuple[set[str], set[str]]:
    """Vergleichsnamen einer GND-Einrichtung → (eigenständige Namen, ortsabhängige Namen).

    Eigenständig: Haupt-/Varianten-/Kurzform, jeweils auch mit angehängtem Ort
    („Stadtarchiv (Oberursel (Taunus))“ → „stadtarchiv oberursel taunus“, „stadtarchiv oberursel“).
    Ortsabhängig: der Name ohne Orts-Klammer („stadtarchiv“). Er zählt nur, wenn auch der Ort passt.
    """
    exact: set[str] = set()
    generic: set[str] = set()
    places = b.place_names
    for n in [b.name, *b.variants]:
        exact.add(norm_name(n))
        m = _PAREN_TAIL.search(n)
        base = n[: m.start()].strip() if m else n
        if m:
            generic.add(norm_name(base))
        for p in places:
            p_short = _PAREN_TAIL.sub("", p).strip()
            for place in {p, p_short}:
                if norm_name(place) and norm_name(place) not in norm_name(base):
                    exact.add(norm_name(f"{base} {place}"))
    return exact - {""}, generic - {""} - exact


def best_ratio(a: set[str], b: set[str], floor: float = 0.0) -> float:
    """Höchste Ähnlichkeit zweier Namensmengen (wie registry.name_similarity, mit Schnellfilter)."""
    best = 0.0
    for x in a:
        for y in b:
            short, long_ = sorted((x, y), key=len)
            if len(short.split()) >= 2 and f" {short} " in f" {long_} ":
                best = max(best, 0.9)
            sm = difflib.SequenceMatcher(None, x, y)
            if sm.real_quick_ratio() < floor or sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
                continue
            best = max(best, sm.ratio())
    return best


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


# --- Pool mit Orten -------------------------------------------------------------------
class GndPool:
    """Registry-Pool (Namen, Websites, Koordinaten) plus Orte der Items (P131 1–2 Stufen, P276, P159)."""

    def __init__(self, pool: Pool, locations: dict[str, list[tuple[str, str]]]) -> None:
        self.pool = pool
        self.loc_qids: dict[str, set[str]] = defaultdict(set)
        self.loc_names: dict[str, set[str]] = defaultdict(set)
        self.by_place_qid: dict[str, set[str]] = defaultdict(set)
        self.by_place_name: dict[str, set[str]] = defaultdict(set)
        self.grid: dict[tuple[int, int], set[str]] = defaultdict(set)
        for q, locs in locations.items():
            if q not in pool.items:
                continue
            for pq, label in locs:
                if pq:
                    self.loc_qids[q].add(pq)
                    self.by_place_qid[pq].add(q)
                if norm_name(label):
                    self.loc_names[q].add(norm_name(label))
                    self.by_place_name[norm_name(label)].add(q)
        for q, ex in pool.extras.items():
            if q in pool.items:
                for lat, lon in ex.coords:
                    self.grid[(int(lat * 10), int(lon * 10))].add(q)

    def place_match(self, b: GndBody, q: str) -> str | None:
        """Grund, warum Ort der GND-Einrichtung und Item übereinstimmen, sonst None."""
        for p in b.places:
            if p.get("qid") and p["qid"] in self.loc_qids.get(q, ()):
                return f"Ort-QID {p['qid']}"
        names = {norm_name(n) for n in b.place_names} - {""}
        if names & self.loc_names.get(q, set()):
            return "Ortsname"
        ex = self.pool.extras.get(q)
        for p in b.places:
            if p.get("lat") is None or ex is None:
                continue
            for lat, lon in ex.coords:
                d = km(p["lat"], p["lon"], lat, lon)
                if d <= PLACE_KM:
                    return f"{d:.1f} km vom Ort"
        return None

    def items_at(self, b: GndBody) -> set[str]:
        """Items am selben Ort (über QID, Ortsnamen oder Koordinaten-Raster) – Vorauswahl."""
        out: set[str] = set()
        for p in b.places:
            if p.get("qid"):
                out |= self.by_place_qid.get(p["qid"], set())
            if norm_name(p.get("name", "")):
                out |= self.by_place_name.get(norm_name(p["name"]), set())
            if p.get("lat") is not None:
                gx, gy = int(p["lat"] * 10), int(p["lon"] * 10)
                for dx in (-1, 0, 1):
                    for dy in (-2, -1, 0, 1, 2):
                        out |= self.grid.get((gx + dx, gy + dy), set())
        return out


# --- Klassifikation -------------------------------------------------------------------
@dataclass
class GndMatch:
    body: GndBody
    status: str
    qids: list[str] = field(default_factory=list)
    note: str = ""
    score: float | None = None
    in_wikidata: bool = False  # GND (aktuell oder alt) steht bereits in Wikidata

    def to_dict(self) -> dict:
        return {"body": self.body.to_dict(), "status": self.status, "qids": self.qids, "note": self.note,
                "score": self.score, "in_wikidata": self.in_wikidata}

    @classmethod
    def from_dict(cls, d: dict) -> "GndMatch":
        return cls(GndBody(**d["body"]), d["status"], d["qids"], d["note"], d["score"], d["in_wikidata"])


def classify_gnd(
    bodies: list[GndBody],
    gp: GndPool,
    value_index: dict[str, set[str]],  # P227-Wert → QIDs (ganz Wikidata)
    item_values: dict[str, list[str]],  # QID → vorhandene P227-Werte (Pool-Items)
    redirects: dict | None = None,  # alte GND → Redirect (sources/gnd_redirects.py)
    include_historical: bool = False,
) -> list[GndMatch]:
    """Status je GND-Einrichtung (siehe Modulkopf).

    Nach der Prüfung „schon in Wikidata?“ gilt: Ist die GND inzwischen umgelenkt/gelöscht, gibt es
    weder Kandidaten noch Mix'n'match. Historische Einrichtungen und frühere Namensformen
    (period_status) werden ohne include_historical ebenfalls nicht gesucht.
    """
    redirects = redirects or {}
    pool = gp.pool
    out: list[GndMatch] = []
    strong_by_item: dict[str, list[GndMatch]] = defaultdict(list)

    for b in bodies:
        # 1. GND schon in Wikidata?
        have = value_index.get(b.gnd, set())
        if len(have) > 1:
            out.append(GndMatch(b, MEHRDEUTIG, sorted(have), "GND an mehreren Items", in_wikidata=True))
            continue
        if have:
            out.append(GndMatch(b, VORHANDEN, sorted(have), in_wikidata=True))
            continue
        old = {o: value_index[o] for o in b.old_ids if o in value_index}
        if old:
            qs = sorted(set().union(*old.values()))
            out.append(GndMatch(b, VORHANDEN_ALT, qs, "Wikidata führt umgelenkte GND "
                                + ", ".join(f"{o} ({', '.join(sorted(v))})" for o, v in old.items())
                                + f"; aktuell: {b.gnd}", in_wikidata=True))
            continue
        r = redirects.get(b.gnd)
        if r is not None:
            if r.kind == "geloescht":
                out.append(GndMatch(b, GND_GELOESCHT, note="GND laut Umlenkungsdatei gelöscht"))
            else:
                out.append(GndMatch(b, GND_UMGELENKT, note=f"GND laut Umlenkungsdatei umgelenkt auf {r.new_id}"))
            continue
        if not include_historical and b.period_status:
            out.append(GndMatch(b, HISTORISCH if b.period_status == "historisch" else VORGAENGER,
                                note=f"Zeitraum {b.period_raw}" + (f"; Nachfolger: {', '.join(b.successors)}"
                                                                   if b.successors else "")))
            continue

        # 2. starke Kandidaten: gleicher Name UND gleicher Ort.
        # Items, deren P227 die GND der übergeordneten Körperschaft ist, sind die Mutter-Einrichtung
        # (z. B. Staatsbibliothek zu Berlin für ihre Ostasienabteilung) und scheiden aus.
        parent = {s["gnd"] for s in b.superior}
        ok = lambda q: not parent & set(item_values.get(q, ()))  # noqa: E731
        exact, generic = body_names(b)
        by_exact = {q for n in exact for q in pool.by_name.get(n, ()) if ok(q)}
        by_generic = {q for n in generic for q in pool.by_name.get(n, ()) if ok(q)}
        strong = {q: why for q in by_exact | by_generic if (why := gp.place_match(b, q))}
        if len(strong) > 1:
            out.append(GndMatch(b, MEHRDEUTIG, sorted(strong), "mehrere Items mit gleichem Namen am selben Ort"))
            continue
        if strong:
            q, why = next(iter(strong.items()))
            other = item_values.get(q, [])
            if other:
                out.append(GndMatch(b, KONFLIKT, [q], f"Item hat bereits P227 = {', '.join(other)} "
                                    "(Vorgänger, Untereinheit oder GND-Dublette?)"))
            else:
                same = sorted((exact | generic) & pool.names.get(q, set()))
                via = "Hauptname" if norm_name(b.name) in same else "Variante/Ort ergänzt"
                m = GndMatch(b, STARK, [q], f"Name gleich ({via}: {same[0]}), Ort gleich ({why})", 1.0)
                strong_by_item[q].append(m)
                out.append(m)
            continue

        # 3. schwache Kandidaten (nur Bericht)
        weak: dict[str, tuple[float, str]] = {}
        for q in by_exact:
            weak[q] = (1.0, "Name gleich, Ort abweichend/unbekannt")
        names = exact | generic
        for q in gp.items_at(b):
            if q in weak or not ok(q) or not gp.place_match(b, q):
                continue
            r = best_ratio(names, pool.names.get(q, set()), WEAK_NAME_RATIO)
            if r >= WEAK_NAME_RATIO:
                weak[q] = (r, f"Name ähnlich ({r:.2f}), Ort gleich")
        urls = {norm_url(u) for u in b.homepages} - {""}
        for u in urls:
            for q in pool.by_url.get(u, set()):
                if not ok(q):
                    continue
                r = best_ratio(names, pool.names.get(q, set()), WEB_NAME_RATIO)
                if q not in weak and r >= WEB_NAME_RATIO:
                    weak[q] = (r, f"Website gleich, Name ähnlich ({r:.2f})")
        if weak:
            best = sorted(weak.items(), key=lambda kv: (-kv[1][0], kv[0]))
            notes = [f"{q}: {n}" + (f" [hat P227 {', '.join(item_values[q])}]" if item_values.get(q) else "")
                     for q, (_, n) in best]
            out.append(GndMatch(b, SCHWACH, [q for q, _ in best], "; ".join(notes), best[0][1][0]))
        else:
            out.append(GndMatch(b, KEIN_TREFFER))

    # Ein Item darf nur einen starken Kandidaten bekommen
    for q, ms in strong_by_item.items():
        if len(ms) > 1:
            for m in ms:
                m.status = MEHRDEUTIG
                m.note = f"{q} ist starker Kandidat für mehrere GND: {', '.join(x.body.gnd for x in ms)}"
    return out


def p227_format_findings(items: dict[str, WikidataItem]) -> list[tuple[str, str, str]]:
    """(QID, Wert, Problem) für P227-Werte im Pool mit Formatfehler oder falscher Prüfziffer – nur Bericht."""
    out = []
    for q, it in items.items():
        for st in it.values("P227"):
            c = check("P227", st.value)
            if c.problem:
                out.append((q, st.value, c.problem))
            elif not gnd_checksum_ok(c.normalized or ""):
                out.append((q, st.value, "Prüfziffer falsch"))
    return sorted(out)


# --- Mix'n'match --------------------------------------------------------------------
def _year(s: str) -> str:
    m = re.search(r"\d{4}", s or "")
    return m.group(0) if m else (s or "")


def mnm_description(b: GndBody) -> str:
    """Beschreibung für Mix'n'match: Ort; Oberbegriff; Zeitraum; übergeordnete Körperschaft (≤ 255 Zeichen).

    Eine ISIL gibt es in der GND nicht (siehe README), daher fehlt sie hier.
    """
    parts = []
    if b.place_names:
        parts.append(", ".join(dict.fromkeys(b.place_names)))
    if b.broader:
        parts.append(", ".join(dict.fromkeys(b.broader)))
    if b.established and b.terminated:
        parts.append(f"{_year(b.established)}–{_year(b.terminated)}")
    elif b.established:
        parts.append(f"gegr. {_year(b.established)}")
    elif b.terminated:
        parts.append(f"bis {_year(b.terminated)}")
    elif b.period_raw:
        parts.append(b.period_raw)
    if b.period_status:
        parts.append("aufgelöst" if b.period_status == "historisch" else "früherer Name")
    if b.country in COUNTRY_NOTE:
        parts.append(COUNTRY_NOTE[b.country])
    sup = [s.get("name") or s["gnd"] for s in b.superior]
    if sup:
        parts.append("Teil von: " + ", ".join(sup))
    desc = "; ".join(parts)
    return desc if len(desc) <= MNM_DESC_MAX else desc[: MNM_DESC_MAX - 1].rstrip() + "…"


def mnm_rows(matches: list[GndMatch], include_historical: bool = False) -> dict[tuple[str, str], list[dict[str, str]]]:
    """(typ, land) → Zeilen {id, name, description, url, type}; nur GND, die nicht in Wikidata stehen.

    Einrichtungen mit mehreren Typen (z. B. Bibliothek + Archiv) erscheinen in jeder Typ-Datei.
    Nie: umgelenkte/gelöschte GND. Historische Einrichtungen und frühere Namensformen nur mit
    include_historical (die Beschreibung nennt dann Zeitraum und „aufgelöst“/„früherer Name“).
    """
    out: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for m in matches:
        b = m.body
        if m.in_wikidata or m.status in NOT_FOR_MNM or (b.period_status and not include_historical):
            continue
        for t in b.types:
            if t in MNM_TYPE:
                out[(t, b.country or "ohne_land")].append(
                    {"id": b.gnd, "name": _clean(b.name), "description": _clean(mnm_description(b)),
                     "url": f"https://d-nb.info/gnd/{b.gnd}", "type": MNM_TYPE[t]})
    return dict(out)


def _clean(s: str) -> str:
    """Tabs/Zeilenumbrüche entfernen (eine Zeile je Eintrag)."""
    return re.sub(r"[\t\r\n]+", " ", s or "").strip()


# --- Deutsche Labels, Beschreibungen und Aliase aus der GND ---------------------------
# Für Items, deren P227 auf eine Einrichtung der Tabelle zeigt. Vorhandene deutsche Werte
# werden nie überschrieben; ein „mul“-Label gilt als vorhandenes Label.
TERM_MAX = 250  # Wikidata-Grenze für Label, Beschreibung und Alias
ALIAS_MIN = 4  # Kürzel mit 2–3 Zeichen („HM“, „ULB“) sind zu zahlreich und nicht eindeutig


def _single_word(v: str) -> bool:
    """Einwortform ohne Leerzeichen und Bindestrich („Bistumsarchiv“, „HStAS“) – als Alias zu unscharf.
    Bindestrich-Namen („Städel-Museum“) gelten als Name und bleiben."""
    return " " not in v and "-" not in v
TYPE_WORD = {"museum": "Museum", "library": "Bibliothek", "archive": "Archiv"}
_GLAM_ENDINGS = ("museum", "museen", "bibliothek", "bücherei", "archiv", "sammlung", "galerie", "thek")
_NEUTER = ("museum", "archiv")
_FEMININE = ("bibliothek", "bücherei", "sammlung", "galerie", "thek")
_ADJECTIVE = re.compile(r"^[A-ZÄÖÜ][a-zäöüß]+(e|es|er|en)$")


def _term_ok(v: str) -> bool:
    """Für QuickStatements geeignet: nicht leer, ≤ 250 Zeichen, ohne Anführungszeichen/Tab/Zeilenumbruch."""
    return bool(v) and len(v) <= TERM_MAX and not any(c in v for c in '"\t\n\r')


def de_label(b: GndBody) -> str:
    """GND-Name als Label; eine Orts-Klammer am Ende wird angehängt („Stadtarchiv (Kelsterbach)“ →
    „Stadtarchiv Kelsterbach“). Andere Klammern bleiben stehen."""
    m = _PAREN_TAIL.search(b.name)
    if m:
        inner, base = m.group(1).strip(), b.name[: m.start()].strip()
        places = {p for n in b.place_names for p in (n, _PAREN_TAIL.sub("", n).strip())}
        if base and inner in places:
            # „Forum … in Dortmund (Dortmund)“ → Ort nicht doppelt anhängen
            return base if f" {inner.casefold()}" in f" {base.casefold()}" else f"{base} {inner}"
    return b.name


def _kind(b: GndBody) -> str:
    """Oberbegriff für die Beschreibung: erster GND-Oberbegriff, der eine GLAM-Einrichtung benennt,
    sonst der Grundtyp (Museum/Bibliothek/Archiv). Ein führendes Adjektiv wird kleingeschrieben."""
    kind = next((k for k in b.broader if k.casefold().endswith(_GLAM_ENDINGS)), "")
    if not kind:
        kind = next((TYPE_WORD[t] for t in b.types if t in TYPE_WORD), "")
    words = kind.split()
    if len(words) > 1 and _ADJECTIVE.match(words[0]):
        words[0] = words[0][0].lower() + words[0][1:]
    return " ".join(words)


def de_description(b: GndBody, former: bool = False) -> str:
    """„Heimatmuseum in Weißenhorn“, „wissenschaftliche Bibliothek in München“; mit `former`
    „ehemaliges …“/„ehemalige …“ (nur wenn das Geschlecht aus der Endung eindeutig ist).
    Ohne Ort oder Oberbegriff: leer (eine Beschreibung nur aus dem Typ wäre zu unscharf)."""
    kind = _kind(b)
    if not kind or not b.place_names:
        return ""
    if former:
        last = kind.split()[-1].casefold()
        if last.endswith(_NEUTER):
            kind = "ehemaliges " + kind
        elif last.endswith(_FEMININE):
            kind = "ehemalige " + kind
    desc = f"{kind} in {b.place_names[0]}"
    return desc if _term_ok(desc) else ""


# „Stiftung X. Bibliothek“, „… AG. Archiv“, „… e.V.. Archiv“ (nicht „St. Anna“, „Dr. Ing.“)
_HIERARCHY = re.compile(r"(?:\w{4,}|\b[A-ZÄÖÜ]{2,}|\.)\. [A-ZÄÖÜ0-9]")
# Wörter, die eine fremdsprachige Namensform verraten (deutsche Aliase sollen deutsch sein)
_FOREIGN = re.compile(r"\b(?:library|university|foundation|musée|musee|bibliothèque|archives|museo|biblioteca|"
                      r"biblioteka|knihovna|muzeum|of|the|de la|du|des archives)\b", re.I)


def de_aliases(b: GndBody) -> list[str]:
    """Hauptname und Varianten der GND als Alias-Kandidaten, in Label-Form.

    Eine Orts-Klammer wird angehängt („Heimatmuseum (Oettingen i. Bay.)“ → „Heimatmuseum Oettingen
    i. Bay.“). Weg fallen: übrige Klammerformen (Zeiträume, Übersetzungen, GND-Zusätze),
    Hierarchieformen „Träger. Einrichtung“, erkennbar fremdsprachige Formen, Kürzel mit bis zu 3 Zeichen und Einwortformen
    (ohne Leerzeichen und Bindestrich).
    """
    out: dict[str, str] = {}
    for v in [b.name, *b.variants]:
        v = de_label(GndBody(gnd=b.gnd, name=v.strip(), places=b.places))
        if len(v) < ALIAS_MIN or _single_word(v) or not _term_ok(v) or "(" in v or _HIERARCHY.search(v) or _FOREIGN.search(v):
            continue
        out.setdefault(v.casefold(), v)
    return list(out.values())


@dataclass
class TermProposal:
    """Vorschläge für ein Item: neues Label, neue Beschreibung, neue Aliase (jeweils deutsch)."""

    qid: str
    gnd: list[str]
    label_now: str = ""  # vorhandenes de- bzw. mul-Label
    description_now: str = ""
    label: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def has_changes(self) -> bool:
        return bool(self.label or self.description or self.aliases)


def term_proposals(terms: dict[str, dict], bodies: dict[str, GndBody]) -> list[TermProposal]:
    """terms: QID → {gnd: set, human: bool, glam: bool, lde, lmul, dde: str, aliases: set} (live aus Wikidata).

    - Nur GND aus der Tabelle, die an genau EINEM Item stehen.
    - Nur Items, die in Wikidata ein Museum/eine Bibliothek/ein Archiv sind (glam); andere und
      Menschen (Q5) bekommen nichts, nur einen Hinweis (P227 prüfen).
    - Label nur ohne de- und ohne mul-Label; Beschreibung nur ohne de-Beschreibung.
    - Aliase: alle GND-Namen, die weder Label noch schon Alias (de/mul) sind.
    - Ergäbe sich dieselbe Kombination aus Label und Beschreibung für mehrere Items, entfällt die
      neue Beschreibung (Wikidata verlangt sie je Sprache eindeutig).
    """
    owners: dict[str, set[str]] = defaultdict(set)
    for q, t in terms.items():
        for g in t["gnd"]:
            owners[g].add(q)
    out: list[TermProposal] = []
    for q in sorted(terms):
        t = terms[q]
        gnds = sorted(g for g in t["gnd"] if g in bodies)
        if not gnds:
            continue
        p = TermProposal(q, gnds, label_now=t.get("lde") or t.get("lmul") or "", description_now=t.get("dde") or "")
        shared = [g for g in gnds if len(owners[g]) > 1]
        if shared:
            p.note = f"GND an mehreren Items: {', '.join(shared)}"
            gnds = [g for g in gnds if g not in shared]
        if t.get("human"):
            p.note = "Item ist ein Mensch (Q5) – P227 prüfen"
            gnds = []
        elif not t.get("glam", True):
            p.note = "P31 ist kein Museum/Bibliothek/Archiv – nichts vorgeschlagen, P227 prüfen"
            gnds = []
        if not gnds:
            out.append(p)
            continue
        bs = sorted((bodies[g] for g in gnds), key=lambda b: (bool(b.terminated), b.gnd))
        main = bs[0]
        if not p.label_now:
            label = de_label(main)
            p.label = label if _term_ok(label) else ""
        if not p.description_now:
            p.description = de_description(main, former=all(b.terminated for b in bs))
        current = p.label_now or p.label
        if p.description and p.description.casefold() == current.casefold():
            p.description = ""
        taken = {current.casefold(), (t.get("lde") or "").casefold(), (t.get("lmul") or "").casefold()}
        taken |= {a.casefold() for a in t.get("aliases", ())}
        aliases: dict[str, str] = {}
        for b in bs:
            for a in de_aliases(b):
                if a.casefold() not in taken:
                    aliases.setdefault(a.casefold(), a)
        p.aliases = list(aliases.values())
        out.append(p)

    # Fremdsprachige Aliase ohne deutsches Wort („German Historical Museum“) – Prüfung stapelweise
    prime(words_of(a for p in out for a in p.aliases))
    for p in out:
        p.aliases = [a for a in p.aliases if not foreign_reason(a)]

    # Eindeutigkeit Label + Beschreibung innerhalb des Laufs
    pairs: dict[tuple[str, str], list[TermProposal]] = defaultdict(list)
    for p in out:
        desc = p.description or p.description_now
        if desc:
            pairs[((p.label_now or p.label).casefold(), desc.casefold())].append(p)
    for group in pairs.values():
        if len(group) > 1:
            for p in group:
                if p.description:
                    p.description = ""
                    p.note = "; ".join(filter(None, [p.note, "Label + Beschreibung nicht eindeutig: "
                                                     + ", ".join(x.qid for x in group if x is not p)]))
    return out


def term_commands(proposals: list[TermProposal], kind: str) -> list[list[str]]:
    """QuickStatements je Item (eine Gruppe pro Item): kind = 'Lde' | 'Dde' | 'Ade'."""
    groups = []
    for p in proposals:
        values = {"Lde": [p.label] if p.label else [], "Dde": [p.description] if p.description else [],
                  "Ade": p.aliases}[kind]
        if values:
            groups.append([f'{p.qid}\t{kind}\t"{v}"' for v in values])
    return groups



# --- Zeitraum: Anmerkungen bei vorhandenen GND-IDs, Datumsvorschläge ----------------------
HISTORISCH_IN_WD = "historisch_in_wikidata"  # GND: aufgelöst; Item ohne P576
ZEITRAUM_ABWEICHEND = "zeitraum_abweichend"  # genaues GND-Jahr ≠ P571/P576
VORGAENGER_GND = "vorgaenger_gnd"  # Item trägt die GND einer früheren Namensform
P576_NUR_WD = "p576_nur_in_wikidata"  # GND ohne Ende, Item mit P576 (Dump älter oder GND unvollständig)
ANNOTATIONS = [HISTORISCH_IN_WD, ZEITRAUM_ABWEICHEND, VORGAENGER_GND, P576_NUR_WD]


def period_annotation(b: GndBody, p571: set[int], p576: set[int]) -> dict[str, str]:
    """Spalten gnd_zeitraum, gnd_historisch, wd_p571, wd_p576, anmerkung für ein Item mit dieser GND.

    Beginn wird nur verglichen, wenn der Satz keinen Vorgänger hat (sonst ist es meist das Jahr der
    Umbenennung); Ende nur bei historischen Einrichtungen (sonst meist Umbenennung).
    """
    notes = []
    if b.period_status == "historisch" and not p576:
        notes.append(HISTORISCH_IN_WD)
    if b.period_status == "vorgaenger":
        notes.append(VORGAENGER_GND)
    if not b.closed and p576:
        notes.append(P576_NUR_WD)
    differs = (b.start_year is not None and not b.predecessors and p571 and b.start_year not in p571) or (
        b.historical and b.end_year is not None and p576 and b.end_year not in p576)
    if differs:
        notes.append(ZEITRAUM_ABWEICHEND)
    return {"gnd_zeitraum": b.period_raw, "gnd_historisch": b.period_status,
            "wd_p571": ", ".join(map(str, sorted(p571))), "wd_p576": ", ".join(map(str, sorted(p576))),
            "anmerkung": ", ".join(notes)}


@dataclass
class QualifierProposal:
    """Zeitraum des GND-Satzes als Qualifikator an der P227-Aussage (P580/P582)."""

    qid: str
    gnd: str
    start: int | None = None  # P580 Startzeitpunkt
    end: int | None = None  # P582 Endzeitpunkt


def qualifier_proposals(matches: list[GndMatch], statements: dict[tuple[str, str], list[dict]],
                        current_year: int) -> list[QualifierProposal]:
    """P580/P582 an P227 (Option --propose-dates): Der Zeitraum gilt dem GND-Satz (Identifier bzw.
    Namensform), nicht der Einrichtung. Deshalb gelten auch Enden früherer Namensformen.

    statements: (QID, GND) → [{rank, has_p580, has_p582}] der P227-Aussagen mit diesem Wert (live).
    - nur Status vorhanden (GND an genau einem Item) und genau EINE nicht-deprecated Aussage;
    - nur genaues Jahr (ein Wert, kein „ca.“/Jahrhundert/19XX/kombiniertes Feld); keine Vorschläge
      bei mehreren Beginn-/Endwerten (Wiedereröffnung), Beginn nicht nach Ende, nichts in der Zukunft;
    - nie ergänzen, wenn die Aussage den Qualifikator schon hat (gleich welcher Wert) → keine Dopplung.
    """
    out = []
    for m in matches:
        if m.status != VORHANDEN or len(m.qids) != 1:
            continue
        b, q = m.body, m.qids[0]
        sts = [st for st in statements.get((q, b.gnd), []) if "deprecated" not in st["rank"].lower()]
        if len(sts) != 1 or " / " in b.period_raw:
            continue
        start, end = b.start_year, b.end_year
        if (start and end and start > end) or (start and start > current_year) or (end and end > current_year):
            continue
        p = QualifierProposal(q, b.gnd, start if start and not sts[0]["has_p580"] else None,
                              end if end and not sts[0]["has_p582"] else None)
        if p.start or p.end:
            out.append(p)
    return out


def qualifier_commands(proposals: list[QualifierProposal]) -> list[list[str]]:
    """Eine Zeile je Aussage: gleicher P227-Wert → QuickStatements ergänzt die vorhandene Aussage."""
    out = []
    for p in proposals:
        quals = (f"\tP580\t+{p.start:04d}-00-00T00:00:00Z/9" if p.start else "") + \
                (f"\tP582\t+{p.end:04d}-00-00T00:00:00Z/9" if p.end else "")
        out.append([f'{p.qid}\tP227\t"{p.gnd}"{quals}'])
    return out


# --- Umgelenkte/gelöschte P227-Werte in Wikidata ------------------------------------------
P227_UMGELENKT = "p227_umgelenkt"
P227_GELOESCHT = "p227_geloescht"


@dataclass
class RedirectFinding:
    qid: str
    old: str
    new: str | None  # Endziel (Ketten aufgelöst); None bei Löschung
    status: str  # p227_umgelenkt | p227_geloescht
    rank: str
    refs: int
    quals: int
    action: str = ""  # korrektur | bericht
    note: str = ""
    new_present: bool = False  # Endziel steht schon am Item


def p227_redirect_findings(statements: list[dict], redirects: dict, owners: dict[str, list[str]]) -> list[RedirectFinding]:
    """statements: [{qid, value, rank, refs, quals}] (P227-Aussagen, live). owners: GND → QIDs (P227).

    - deprecated: schon erledigt → übergangen;
    - gelöscht → nur Bericht;
    - umgelenkt → Korrektur (alte Aussage entfernen, Endziel mit Quelle ergänzen, falls es fehlt) NUR,
      wenn die alte Aussage weder Quellen noch Qualifikatoren hat und das Endziel an keinem anderen
      Item steht; sonst Bericht. Aufspaltungen (mehrere Ziele) nie automatisch.
    """
    from .sources.gnd_redirects import resolve

    out = []
    for st in statements:
        old = st["value"]
        if old not in redirects or "deprecated" in st["rank"].lower():
            continue
        new, kind = resolve(redirects, old)
        f = RedirectFinding(st["qid"], old, new, P227_GELOESCHT if kind == "geloescht" else P227_UMGELENKT,
                            st["rank"].rsplit("#", 1)[-1], int(st["refs"]), int(st["quals"]), action="bericht")
        if f.status == P227_GELOESCHT:
            f.note = "GND gelöscht – von Hand prüfen (Aussage deprecaten oder entfernen)"
        elif new and "|" in new:
            f.note = "Aufspaltung auf mehrere GND – nie automatisch"
        else:
            others = sorted(set(owners.get(new, [])) - {f.qid})
            f.new_present = f.qid in owners.get(new, [])
            if others:
                f.note = f"Ziel {new} steht an anderem Item: {', '.join(others)}"
            elif f.refs or f.quals:
                f.note = "alte Aussage hat Quellen/Qualifikatoren – von Hand deprecaten"
            else:
                f.action = "korrektur"
                f.note = "Ziel steht schon am Item – alte Aussage entfernen" if f.new_present else \
                    "alte Aussage durch Ziel ersetzen"
        out.append(f)
    return out


def redirect_commands(findings: list[RedirectFinding], date: str) -> list[list[str]]:
    """Korrektur-QuickStatements je Aussage: alte P227 entfernen, Ziel (falls fehlend) mit Quelle ergänzen."""
    ref = f"\tS248\t{GND_ITEM}\tS813\t+{date}T00:00:00Z/11"
    groups = []
    for f in findings:
        if f.action != "korrektur":
            continue
        g = [f'-{f.qid}\tP227\t"{f.old}"']
        if not f.new_present:
            g.append(f'{f.qid}\tP227\t"{f.new}"{ref}')
        groups.append(g)
    return groups


# --- Sichere Kandidaten → P227-QuickStatements ------------------------------------------
def _compact(s: str) -> str:
    """Vergleichsform: ohne Akzente/Umlaut-Punkte, Groß-/Kleinschreibung, Satz- und Leerzeichen."""
    s = s.replace("ß", "ss").replace("’", "'")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return "".join(c for c in s.casefold() if c.isalnum())


def _forms(names: set[str], places: set[str]) -> set[str]:
    """Kompaktformen mit und ohne Ortsnamen bzw. Orts-Klammer („Heimatmuseum (Davos)“)."""
    out = set()
    pl = sorted({_compact(p) for p in places} - {""}, key=len, reverse=True)
    for n in names:
        for v in (n, _PAREN_TAIL.sub("", n)):
            c = _compact(v)
            if not c:
                continue
            out.add(c)
            for p in pl:
                if p in c and c.replace(p, ""):
                    out.add(c.replace(p, ""))
    return out


def label_similarity(b: GndBody, item: WikidataItem) -> float:
    """Höchste Ähnlichkeit zwischen GND-Namen (Haupt- und Variantennamen) und den Labels des Items
    (alle Sprachen, ohne Aliase), jeweils auch ohne den Ortsnamen der GND-Einrichtung."""
    places = set(b.place_names) | {p.get("name", "") for p in b.places}
    return names_similarity([b.name, *b.variants], places, item)


def names_similarity(names: Iterable[str], places: Iterable[str], item: WikidataItem) -> float:
    """Höchste Ähnlichkeit (kompakt) zwischen Namen und Labels des Items, auch ohne Ortsnamen."""
    places = set(places)
    gnd = _forms(set(names), places)
    labels = _forms(set(item.labels.values()), places)
    best = 0.0
    for a in gnd:
        for c in labels:
            r = 1.0 if a == c else difflib.SequenceMatcher(None, a, c).ratio()
            best = max(best, r)
    return best


def mark_sure(matches: list[GndMatch], gp: GndPool, item_values: dict[str, list[str]]) -> int:
    """Starke/schwache Kandidaten mit genau EINEM sicheren Item auf SICHER setzen.

    Sicher: Ort gleich (place_match), Item ohne P227, ein GND-Name ist einem Label sehr ähnlich
    (≥ SURE_LABEL_RATIO, Aliase zählen nicht – sie führen sonst auf Gebäude-Items aus Denkmallisten).
    Zielen mehrere GND auf dasselbe Item, bleibt es bei den bisherigen Status (→ Mix'n'match).
    """
    chosen: dict[str, list[tuple[GndMatch, str, float]]] = defaultdict(list)
    for m in matches:
        if m.status not in (STARK, SCHWACH):
            continue
        ok = []
        for q in m.qids:
            it = gp.pool.items.get(q)
            if it is None or item_values.get(q) or not (why := gp.place_match(m.body, q)):
                continue
            r = label_similarity(m.body, it)
            if r >= SURE_LABEL_RATIO:
                ok.append((q, r, why))
        if len(ok) == 1:
            q, r, why = ok[0]
            chosen[q].append((m, why, r))
    n = 0
    for q, ms in chosen.items():
        if len(ms) != 1:
            continue
        m, why, r = ms[0]
        m.status, m.qids, m.score = SICHER, [q], round(r, 3)
        m.note = f"Label sehr ähnlich ({r:.2f}), Ort gleich ({why}) → QuickStatement"
        n += 1
    return n


def p227_commands(matches: list[GndMatch], date: str) -> list[list[str]]:
    """P227 für sichere Kandidaten, Quelle GND (S248) + Abrufdatum."""
    ref = f"\tS248\t{GND_ITEM}\tS813\t+{date}T00:00:00Z/11"
    return [[f'{m.qids[0]}\tP227\t"{m.body.gnd}"{ref}'] for m in matches if m.status == SICHER]
