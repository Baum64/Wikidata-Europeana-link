"""P856 (offizielle Website) für Museen, Bibliotheken und Archive ohne Website in Wikidata.

Ohne Netz und getestet. Kandidaten kommen nur aus Quellen, die über eine Kennung am Item hängen,
also ohne Namensabgleich: ISIL-Register (P791), GND (P227), Museen in Österreich (P7917),
Europeana (P7704) und DDB (über ISIL bzw. GND). Jede Adresse wird vorher abgerufen
(sources/linkcheck.py).

Status je Item (nur Items ohne gültige P856):
  neu                   erreichbar, keine Park-/Fehlerseite, an keinem anderen Item und nur für
                        dieses Item vorgeschlagen, Quellen widersprechen sich nicht, Name im
                        Seitentitel oder in der Adresse → QuickStatements
  pruefen               erreichbar, aber kein Namensnachweis (oder nur der Ortsname auf einer
                        Domain ohne Einrichtungswort, z. B. die Gemeinde-Startseite); oder Umzug
                        auf eine andere Domain mit Namensnachweis → QuickStatements auskommentiert
  umgeleitet            andere Domain ohne Namensnachweis → Bericht
  tot                   404/410, Domain unbekannt, Park- oder Fehlerseite → Bericht
  unklar                401/403/429/5xx, Zeitüberschreitung, Bot-Schutz → Bericht (später erneut)
  geteilt               Adresse ist P856 eines anderen Items oder Kandidat für mehrere Items → Bericht
  widerspruch           erreichbare Adressen auf verschiedenen Domains → Bericht
  aufgeloest            P576 am Item oder alle GND-Sätze des Items aufgelöst → Bericht
  veraltet_in_wikidata  die Adresse steht am Item schon mit Rang „veraltet“ → Bericht
  ungueltig             keine verwertbare Adresse (Leerzeichen, kaputte Umlaute, Datei, soziales Netzwerk)
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from .registry import _TRACKING, norm_url
from .sources.linkcheck import Check, with_scheme

PROPS = ("P856", "P227", "P791", "P7917", "P7704", "P576")

NEU = "neu"
PRUEFEN = "pruefen"
UMGELEITET = "umgeleitet"
TOT = "tot"
UNKLAR = "unklar"
GETEILT = "geteilt"
WIDERSPRUCH = "widerspruch"
AUFGELOEST = "aufgeloest"
VERALTET = "veraltet_in_wikidata"
UNGUELTIG = "ungueltig"
STATUSES = [NEU, PRUEFEN, UMGELEITET, TOT, UNKLAR, GETEILT, WIDERSPRUCH, AUFGELOEST, VERALTET, UNGUELTIG]
# Zustand einer einzelnen Adresse (zusätzlich zu den Status oben)
OK = "ok"
EXTERN = "extern"

# Rangfolge bei Gleichstand: Übereinstimmung mit vorhandenen P856 in der Erkundung (28.11.2026)
SOURCE_ORDER = ("europeana", "isil", "isil_ch", "mure", "gnd", "ddb")
SOURCE_NAMES = {"europeana": "Europeana", "isil": "ISIL-Register", "isil_ch": "ISIL-Verzeichnis CH",
                "mure": "Museen in Österreich", "gnd": "GND", "ddb": "DDB"}
# Fundstelle S248; ohne eigenes Item S854 auf den Eintrag
REF_ITEMS = {"isil": "Q28657655", "gnd": "Q36578", "europeana": "Q234110", "ddb": "Q621630"}

# P407 nur für Sprachen des Landes (sonst oft eine falsche Vorlage, z. B. lang="en-US")
LANG_QID = {"de": "Q188", "fr": "Q150", "it": "Q652", "rm": "Q13199", "en": "Q1860"}
COUNTRY_LANGS = {"Q183": {"de"}, "Q40": {"de"}, "Q39": {"de", "fr", "it", "rm"}}

SOCIAL = {"facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "linkedin.com", "tiktok.com",
          "xing.com", "wikipedia.org", "wikimedia.org", "google.com", "goo.gl"}
_SECOND_LEVEL = re.compile(r"\.(?:gv|ac|or|co)\.at$|\.(?:co|ac|org)\.uk$|\.com\.\w\w$")
_FILE = re.compile(r"\.(?:pdf|docx?|xlsx?|jpe?g|png|gif|zip)$", re.I)
_MOJIBAKE = re.compile(r"Ã.|Â|�")
_LANG_SEGMENT = re.compile(r"^(?:[a-z]{2}(?:[-_][a-z]{2})?)$", re.I)
_START_PAGE = re.compile(r"^(?:[a-z]{2}[-_])?(?:start|startseite|home|homepage|willkommen|welcome|accueil|index|"
                         r"default|main|portal|aktuell|aktuelles)(?:[-_][a-z]{2})?(?:[-_]\d+)?(?:\.\w{2,5})?$", re.I)
# Vorsilben von Gemeinde-Domains (stadt-spremberg.de, markt-xy.de)
_MUNICIPAL_PREFIX = re.compile(r"^(?:stadtgemeinde|marktgemeinde|verbandsgemeinde|samtgemeinde|gemeinde|stadt|markt|"
                               r"landkreis|kreis|amt|vg|commune|ville|comune|citta)")

# Namenswörter ohne Aussagekraft (gefaltet: ä→ae, ohne Akzente)
GENERIC = set("""
museum museen musee musees museo musei muzeum bibliothek bibliotheken bibliotheque biblioteca bibliothik
buecherei buechereien mediathek archiv archive archives archivio sammlung sammlungen galerie gallery galleria
ausstellung ausstellungen stadt gemeinde markt kreis landkreis bezirk verein vereins stiftung haus hauses
heimatmuseum heimatstube heimathaus heimatverein dorfmuseum stadtmuseum kreismuseum regionalmuseum
bezirksmuseum landesmuseum freilichtmuseum kunstmuseum volkskundemuseum naturkundemuseum kunsthalle kunstverein
stadtarchiv gemeindearchiv kreisarchiv landesarchiv staatsarchiv pfarrarchiv stiftsarchiv bistumsarchiv
stadtbibliothek stadtbuecherei gemeindebibliothek gemeindebuecherei kreisbibliothek landesbibliothek
staatsbibliothek zentralbibliothek fachbibliothek schulbibliothek pfarrbibliothek pfarrbuecherei
universitaetsbibliothek hochschulbibliothek universitaet hochschule fachhochschule institut instituts
schule gymnasium kirche kloster schloss burg oeffentliche katholische evangelische
deutsches deutsche deutscher deutschen oesterreichisches oesterreichische schweizer schweizerische
fuer ueber eine einer eines sowie beim with from national landes staatliche staatliches
startseite willkommen herzlich welcome home homepage aktuelles offizielle website seite
city town municipal library libraries public university college school house foundation society collection
collections historical history local regional state county district church abbey castle archivum
ville commune maison fondation societe municipale communale cantonale canton kantonal kantons
citta comune casa fondazione civico civica civici storico storica storia biblioteche
""".split())
# Wörter, an denen eine Domain als eigene Domain einer Einrichtung erkennbar ist (gefaltet, ohne Trennzeichen)
INSTITUTION = re.compile(r"museum|musee|museo|muzeum|bib|buecherei|archiv|galerie|gallery|sammlung|mediathek|"
                         r"kunsthalle")


@dataclass
class SourceUrl:
    source: str  # europeana | isil | isil_ch | mure | gnd | ddb
    key: str  # Kennung in der Quelle (ISIL, GND, M-Nummer, organisation/…, DDB-ID)
    url: str  # Adresse wie in der Quelle
    ref_url: str = ""  # Eintrag in der Quelle (für S854 und den Bericht)
    name: str = ""


@dataclass
class ItemInfo:
    qid: str
    label: str
    names: list[str] = field(default_factory=list)  # Labels aller Sprachen und Namen aus den Quellen
    places: list[str] = field(default_factory=list)  # Ortsnamen aus den Quellen
    dissolved: str = ""  # Grund, falls aufgelöst
    deprecated: list[str] = field(default_factory=list)  # P856 mit Rang „veraltet“
    sources: list[SourceUrl] = field(default_factory=list)


@dataclass
class UrlFinding:
    source: SourceUrl
    state: str
    value: str = ""  # Vorschlagsform (bei ok/extern)
    note: str = ""
    check: Check | None = None

    def describe(self) -> str:
        c = self.check
        tail = f" → {self.value}" if self.value and norm_url(self.value) != norm_url(self.source.url) else ""
        return f"{self.source.source}: {self.source.url.strip()}{tail} [{self.state}{': ' + self.note if self.note else ''}]" + \
            (f" „{c.title[:60]}“" if c and c.title else "")


@dataclass
class Proposal:
    qid: str
    label: str
    status: str
    value: str = ""
    lang: str = ""  # Sprachcode für P407 (leer = ohne)
    sources: list[SourceUrl] = field(default_factory=list)  # Fundstellen, in Reihenfolge
    evidence: str = ""  # titel | adresse
    title: str = ""
    note: str = ""
    urls: list[UrlFinding] = field(default_factory=list)


# --- Adressen ---------------------------------------------------------------------------
def host_of(u: str) -> str:
    try:
        return (urlsplit(with_scheme(u)).hostname or "").lower()
    except ValueError:
        return ""


def registrable(host: str) -> str:
    """Domain ohne Subdomains: museum.krumbach.de → krumbach.de, landesmuseum.ktn.gv.at → ktn.gv.at."""
    host = re.sub(r"^www\d?\.", "", (host or "").lower().rstrip("."))
    parts = host.split(".")
    n = 3 if _SECOND_LEVEL.search(host) else 2
    return ".".join(parts[-n:])


def url_problem(u: str) -> str:
    """Grund, warum eine Quell-Adresse keine Website ist (leer = verwendbar)."""
    u = (u or "").strip()
    if not u:
        return "leer"
    if re.search(r"\s", u):
        return "Leerzeichen in der Adresse"
    if _MOJIBAKE.search(u):
        return "kaputte Umlaute"
    if re.search(r"[\"<>{}|\\^`]", u):
        return "unzulässige Zeichen"
    if re.match(r"(?i)(?:mailto|tel):", u):
        return "keine Website"
    m = re.match(r"^([a-z][a-z0-9+.-]*)://", u, re.I)
    if m and m.group(1).lower() not in ("http", "https"):
        return f"Schema {m.group(1)}"
    try:
        parts = urlsplit(with_scheme(u))
        host = (parts.hostname or "").lower()
    except ValueError:
        return "nicht lesbar"
    if "@" in parts.netloc:
        return "E-Mail statt Website"
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return "keine Domain"
    if "." not in host or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", ascii_host):
        return "keine Domain"
    if registrable(host) in SOCIAL:
        return "soziales Netzwerk bzw. Wikipedia"
    if _FILE.search(parts.path):
        return "Datei statt Website"
    return ""


def _path_segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def is_bare(u: str) -> bool:
    """Nur die Domain (auch …/index.html, …/start.php)."""
    n = norm_url(u)
    return "?" not in n and "/" not in n


def _shallow(path: str) -> bool:
    """Pfad besteht nur aus Sprachkürzel und/oder Startseite (/de/, /start, /de/home.html)."""
    return all(_LANG_SEGMENT.match(s) or _START_PAGE.match(s) for s in _path_segments(path))


def _clean(parts) -> str:
    netloc = parts.hostname or ""
    if parts.port and parts.port not in (80, 443):
        netloc += f":{parts.port}"
    path = re.sub(r";jsessionid=[^/?#]*", "", parts.path, flags=re.I) or "/"
    query = "&".join(p for p in parts.query.split("&") if p and not _TRACKING.match(p.split("=")[0]))
    return urlunsplit((parts.scheme.lower(), netloc.lower(), path, query, ""))


def proposed_value(original: str, final: str) -> str:
    """Vorschlagsform: Zieladresse ohne Tracking/Session/Fragment. War die Quelle nur eine Domain
    und führt die Umleitung auf dieselbe Domain oder nur auf eine Start-/Sprachseite, bleibt es
    die Startseite (Schema und Host wie am Ziel)."""
    f = urlsplit(final or with_scheme(original))
    if is_bare(original) and (registrable(f.hostname or "") == registrable(host_of(original)) or _shallow(f.path)):
        return _clean(f._replace(path="/", query=""))
    return _clean(f)


def url_state(c: Check) -> tuple[str, str]:
    """Zustand einer geprüften Adresse: ok | extern | tot | unklar | ungueltig, mit Hinweis."""
    if c.error:
        return {"dns": (TOT, "Domain unbekannt"), "ungueltig": (UNGUELTIG, "nicht abrufbar"),
                "timeout": (UNKLAR, "Zeitüberschreitung"), "ssl": (UNKLAR, "Zertifikat/SSL"),
                "umleitungen": (UNKLAR, "Umleitungsschleife")}.get(c.error, (UNKLAR, "Verbindungsfehler"))
    st = c.status or 0
    if st in (404, 410):
        return TOT, f"HTTP {st}"
    if st >= 400 or st < 200:
        return UNKLAR, f"HTTP {st}"
    if st >= 300:
        return UNKLAR, f"HTTP {st} ohne Ziel"
    if c.parked:
        return TOT, "Park-/Platzhalterseite"
    if c.soft404:
        return TOT, "Fehlerseite (HTTP 200)"
    if c.challenge:
        return UNKLAR, "Bot-Schutz"
    if registrable(host_of(c.url)) != registrable(host_of(c.final)):
        return EXTERN, f"umgeleitet auf {host_of(c.final)}"
    return OK, ""


# --- Namensnachweis -----------------------------------------------------------------------
_FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe", "œ": "oe"})


def fold(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").casefold().translate(_FOLD)
    s = "".join(ch for ch in unicodedata.normalize("NFD", s) if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def tokens(names) -> set[str]:
    return {w for n in names for w in fold(n).split() if len(w) >= 4 and w not in GENERIC and not w.isdigit()}


def _compact(s: str) -> str:
    return fold(s).replace(" ", "")


def municipal_homepage(value: str, places) -> bool:
    """Startseite einer Domain, die nur aus dem Ortsnamen besteht (weinsberg.de, stadt-spremberg.de),
    ohne Einrichtungswort im Host (museum.krumbach.de zählt nicht)."""
    if not is_bare(value):
        return False
    host = host_of(value)
    if INSTITUTION.search(_compact(host)):
        return False
    keys = set()
    for p in places:
        for x in (p, re.split(r"\s*[(/,]", p, maxsplit=1)[0]):
            if len(_compact(x)) >= 3:
                keys.add(_compact(x))
    label = _compact(registrable(host).split(".")[0])
    return label in keys or _MUNICIPAL_PREFIX.sub("", label) in keys


def evidence(names, places, check: Check | None, value: str) -> tuple[str, str]:
    """('titel' | 'adresse' | '', Hinweis): Kommt ein kennzeichnendes Namenswort (oder der ganze Name,
    z. B. „Schloss Hof“) im Seitentitel bzw. in der Adresse vor? Ein Ortsname allein reicht nicht auf
    der Startseite einer Domain, die nur der Ortsname ist (meist die Website der Gemeinde)."""
    place_words = {w for p in places for w in fold(p).split() if len(w) >= 3}
    title = f" {fold(check.title)} " if check and check.title else ""
    addr = _compact(re.sub(r"^https?://(?:www\d?\.)?", "", value or ""))
    name_tok = tokens(names)
    hit_title = {w for w in name_tok if w in title}
    hit_addr = {w for w in name_tok if len(w) >= 5 and w in addr}
    for n in names:  # ganzer Name als Wortfolge (für Namen aus kurzen oder allgemeinen Wörtern)
        words = fold(n).split()
        core = {w for w in words if w not in GENERIC}
        if len(words) < 2 or not core:
            continue
        if title and f" {' '.join(words)} " in title:
            hit_title |= core
        if len("".join(words)) >= 8 and "".join(words) in addr:
            hit_addr |= core
    hits = hit_title | hit_addr
    if not hits:
        return "", "Name weder im Seitentitel noch in der Adresse"
    if hits <= place_words and municipal_homepage(value, places):
        return "", f"nur Ortsname ({', '.join(sorted(hits))}) auf der Startseite {host_of(value)} – Gemeindeseite?"
    return ("titel" if hit_title else "adresse"), ""


# --- Klassifikation -------------------------------------------------------------------------
def _order(source: str) -> int:
    return SOURCE_ORDER.index(source) if source in SOURCE_ORDER else len(SOURCE_ORDER)


def _lang(check: Check | None, country: str) -> str:
    code = (check.lang if check else "").split("-")[0].lower()
    return code if code in COUNTRY_LANGS.get(country, set()) and code in LANG_QID else ""


def url_findings(info: ItemInfo, checks: dict[str, Check], owners: dict[str, set[str]],
                 shared: dict[str, set[str]]) -> list[UrlFinding]:
    """Zustand jeder Quell-Adresse des Items. `owners`/`shared`: norm_url → QIDs (P856 in Wikidata
    bzw. Items dieses Laufs mit derselben Quell-Adresse)."""
    dep = {norm_url(u) for u in info.deprecated} - {""}
    out = []
    for s in info.sources:
        prob = url_problem(s.url)
        if prob:
            out.append(UrlFinding(s, UNGUELTIG, note=prob))
            continue
        c = checks.get(s.url.strip())
        if c is None:
            out.append(UrlFinding(s, UNKLAR, note="nicht geprüft"))
            continue
        state, note = url_state(c)
        value = proposed_value(s.url, c.final) if state in (OK, EXTERN) else ""
        keys = {norm_url(s.url), norm_url(value)} - {""}
        if keys & dep:
            state, note = VERALTET, "steht am Item mit Rang „veraltet“"
        elif state in (OK, EXTERN):
            others = set().union(*(owners.get(k, set()) for k in keys)) - {info.qid}
            peers = shared.get(norm_url(s.url), set()) - {info.qid}
            if others:
                state, note = GETEILT, "P856 von " + ", ".join(sorted(others)[:5])
            elif peers:
                state, note = GETEILT, "auch Quell-Adresse von " + ", ".join(sorted(peers)[:5])
        out.append(UrlFinding(s, state, value, note, c))
    return out


def classify_item(info: ItemInfo, checks: dict[str, Check], owners: dict[str, set[str]],
                  shared: dict[str, set[str]], country: str) -> Proposal:
    p = Proposal(info.qid, info.label, "")
    p.urls = url_findings(info, checks, owners, shared)
    if info.dissolved:
        p.status, p.note = AUFGELOEST, info.dissolved
        return p

    def choose(fs: list[UrlFinding], status_ok: str) -> Proposal:
        by_value: dict[str, list[UrlFinding]] = defaultdict(list)
        for f in fs:
            by_value[norm_url(f.value)].append(f)
        best = max(by_value.values(), key=lambda g: (len({f.source.source for f in g}),
                                                    -min(_order(f.source.source) for f in g)))
        best.sort(key=lambda f: _order(f.source.source))
        first = best[0]
        p.value, p.title = first.value, first.check.title if first.check else ""
        seen, p.sources = set(), []
        for f in best:
            if f.source.source not in seen:
                seen.add(f.source.source)
                p.sources.append(f.source)
        ev, why = evidence(info.names, info.places, first.check, first.value)
        p.evidence, p.lang = ev, _lang(first.check, country)
        p.status = status_ok if ev else PRUEFEN
        notes = [why] if why else []
        if status_ok == PRUEFEN:
            notes.insert(0, f"umgezogen: {first.source.url.strip()} → {first.value}")
        others = [f for f in p.urls if all(f is not b for b in best)]
        notes += [f.describe() for f in others]
        p.note = "; ".join(notes)
        return p

    usable = [f for f in p.urls if f.state == OK]
    if usable:
        domains = {registrable(host_of(f.value)) for f in usable}
        if len(domains) > 1:
            p.status, p.note = WIDERSPRUCH, "; ".join(f.describe() for f in p.urls)
            return p
        return choose(usable, NEU)
    moved = [f for f in p.urls if f.state == EXTERN]
    if moved:
        proven = [f for f in moved if evidence(info.names, info.places, f.check, f.value)[0]]
        if proven and len({registrable(host_of(f.value)) for f in proven}) == 1:
            return choose(proven, PRUEFEN)
        p.status, p.note = UMGELEITET, "; ".join(f.describe() for f in p.urls)
        return p
    states = {f.state for f in p.urls}
    p.status = next((s for s in (UNKLAR, GETEILT, VERALTET, TOT, UNGUELTIG) if s in states), UNGUELTIG)
    p.note = "; ".join(f.describe() for f in p.urls)
    return p


def resolve_collisions(proposals: list[Proposal]) -> int:
    """Gleicher Vorschlag für mehrere Items (z. B. Dach-Website) → alle `geteilt`."""
    by_value: dict[str, list[Proposal]] = defaultdict(list)
    for p in proposals:
        if p.status in (NEU, PRUEFEN):
            by_value[norm_url(p.value)].append(p)
    n = 0
    for ps in by_value.values():
        if len(ps) > 1:
            for p in ps:
                others = ", ".join(sorted(x.qid for x in ps if x is not p)[:5])
                p.status, p.note = GETEILT, f"Vorschlag {p.value} auch für {others}" + (f"; {p.note}" if p.note else "")
                n += 1
    return n


# --- QuickStatements ---------------------------------------------------------------------
def reference(s: SourceUrl, date: str) -> str:
    if s.source in REF_ITEMS:
        return f"\tS248\t{REF_ITEMS[s.source]}\tS813\t+{date}T00:00:00Z/11"
    return f'\tS854\t"{s.ref_url}"\tS813\t+{date}T00:00:00Z/11'


def commands(p: Proposal, date: str) -> list[str]:
    """Erste Zeile mit P407 und der ersten Fundstelle; jede weitere Quelle als eigene Zeile mit
    demselben Wert (QuickStatements ergänzt dann die Fundstelle an derselben Aussage).
    `pruefen`: Hinweiszeile und Befehle mit `#` auskommentiert."""
    lang = f"\tP407\t{LANG_QID[p.lang]}" if p.lang else ""
    first, *rest = p.sources
    lines = [f'{p.qid}\tP856\t"{p.value}"{lang}{reference(first, date)}']
    lines += [f'{p.qid}\tP856\t"{p.value}"{reference(s, date)}' for s in rest]
    if p.status == PRUEFEN:
        src = ", ".join(f"{SOURCE_NAMES.get(s.source, s.source)} {s.key}" for s in p.sources)
        hint = f"# {p.qid} „{p.label}“ ← {src}" + (f" – Titel „{p.title[:80]}“" if p.title else "") + \
               (f" – {p.note}" if p.note else "")
        return [hint.replace("\n", " ")] + ["#" + x for x in lines]
    return lines
