"""Organisationsabgleich Europeana ↔ Wikidata, europaweit (ohne Netzzugriff, damit testbar).

P7704 `organisation/<n>` je Wikidata-Item. Zwei Stufen:

Stufe 1 – Europeana nennt eine Wikidata-QID (sameAs):
  identisch           Wikidata führt einen der Europeana-Werte dieses Items
  konflikt            Item führt einen anderen P7704-Wert (P7704 ist einwertig) – Bericht
  kein_treffer        QID existiert nicht (mehr)
  kein_organisation   Item ist keine Organisation (P31/P279* ≠ Q43229, z. B. Gebäude) – Bericht
  mehrdeutig          Dublette ohne Favorit, Wert hängt an anderem Item, mehrere QIDs
  abgelehnt_in_wikidata  derselbe Wert steht am Item mit Rang „veraltet“ – Bericht
  name_pruefen        sonst alles passend, aber kein Name gleich → QS auskommentiert (Handprüfung)
  fehlt_in_wikidata   Name gleich → QS
  fehlt_ohne_objekte  wie fehlt_in_wikidata, aber 0 Objekte in Europeana → eigene QS-Datei

Stufe 2 – Europeana nennt keine QID: Kandidaten über Website (P856) und GND/VIAF/ISNI:
  kandidat_stark      genau ein Organisations-Item mit gleicher Website bzw. gleicher Kennung UND
                      gleichem Namen → QS (Kandidaten-Datei)
  kandidat_schwach    genau ein Item mit gleicher Website/Kennung und ähnlichem Namen (≥ 0,85) oder
                      gleicher Domain und gleichem Namen → QS auskommentiert (Handprüfung)
  konflikt / mehrdeutig / abgelehnt_in_wikidata wie oben, kein_treffer sonst

„Gleicher Name“: nach Normalisierung (Kleinschreibung, ohne diakritische Zeichen und
Satzzeichen) identisch, verglichen werden alle Europeana-Namen (prefLabel aller Sprachen,
altLabel, Akronyme) mit allen Labels und Aliassen des Items. Namen unter 4 Zeichen zählen nicht.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .models import FEHLT, IDENTISCH, KEIN_TREFFER, KONFLIKT, MEHRDEUTIG, Finding, WikidataItem
from .normalize import normalize, normalize_gnd, normalize_isni, normalize_viaf, qid_from_uri
from .persons import ABGELEHNT, OHNE_OBJEKTE
from .registry import name_similarity, norm_url

KEIN_ORGANISATION = "kein_organisation"
NAME_PRUEFEN = "name_pruefen"
KANDIDAT_STARK = "kandidat_stark"
KANDIDAT_SCHWACH = "kandidat_schwach"
ORGANIZATION = "Q43229"
WEAK_RATIO = 0.85
MIN_NAME_LEN = 4
ID_PROPS = ("P227", "P214", "P213")  # Kennungen aus sameAs für Stufe 2
STRONG_VIA = {"website", "P227", "P214", "P213"}


# --- Europeana-Organisation ---------------------------------------------------------
@dataclass
class Org:
    num: str
    label: str
    names: list[str] = field(default_factory=list)
    homepages: list[str] = field(default_factory=list)
    country: str | None = None
    record_count: int = 0
    qids: list[str] = field(default_factory=list)
    ids: dict[str, list[str]] = field(default_factory=dict)
    pref: dict[str, str] = field(default_factory=dict)  # prefLabel je Sprache

    @property
    def value(self) -> str:
        return f"organisation/{self.num}"

    @property
    def url(self) -> str:
        return f"https://www.europeana.eu/de/collections/organisation/{self.num}"


def org_from_raw(o: dict) -> Org:
    """Rohdatensatz der Entity API (Suche, Typ organization) → Org."""
    num = o["id"].rsplit("/", 1)[-1]
    pl = o.get("prefLabel") or {}
    names = list(pl.values())
    for key in ("altLabel", "acronym"):
        for v in (o.get(key) or {}).values():
            names += v if isinstance(v, list) else [v]
    hp = o.get("homepage") or []
    same = o.get("sameAs") or []
    norm = {"P227": normalize_gnd, "P214": normalize_viaf, "P213": normalize_isni}
    pat = {"P227": "d-nb.info/gnd/", "P214": "viaf.org/viaf/", "P213": "isni"}
    return Org(
        num=num,
        label=pl.get("de") or pl.get("en") or next(iter(pl.values()), ""),
        names=list(dict.fromkeys(n for n in names if n)),
        homepages=[h for h in ([hp] if isinstance(hp, str) else hp) if h],
        country=((o.get("country") or {}).get("prefLabel") or {}).get("en"),
        record_count=int((o.get("isAggregatedBy") or {}).get("recordCount") or 0),
        qids=sorted({q for q in (qid_from_uri(s) for s in same if "wikidata.org" in s) if q}),
        ids={p: sorted({v for v in (norm[p](s) for s in same if pat[p] in s) if v}) for p in ID_PROPS},
        pref={k: v for k, v in pl.items() if isinstance(v, str) and v},
    )


# --- Namen ------------------------------------------------------------------------------
def norm_name(s: str) -> str:
    """Kleinschreibung, ohne diakritische Zeichen und Satzzeichen, ß→ss, &→and."""
    s = unicodedata.normalize("NFKD", (s or "").casefold().replace("ß", "ss").replace("&", " and "))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_set(names) -> set[str]:
    return {n for n in (norm_name(x) for x in names) if len(n) >= MIN_NAME_LEN}


def name_check(org: Org, wd_names) -> tuple[bool, float]:
    """(gleicher Name?, beste Ähnlichkeit) zwischen Europeana- und Wikidata-Namen."""
    a, b = name_set(org.names), name_set(wd_names)
    if a & b:
        return True, 1.0
    return False, max((name_similarity(x, y) for x in a for y in b), default=0.0)


# --- Websites ---------------------------------------------------------------------------
_IRI_BAD = re.compile(r"[\s<>\"{}|\\^`]")


def _split(url: str):
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    try:
        p = urlsplit(u)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if not host or "." not in host:
        return None
    return re.sub(r"^www\d?\.", "", host), p.path.rstrip("/"), p.query


def website_variants(url: str) -> tuple[set[str], set[str]]:
    """IRI-Varianten für die P856-Suche: (volle Adresse, nur Domain).

    Schema http/https, mit/ohne www., mit/ohne Schluss-Slash. Die Domain-Varianten
    entfallen, wenn die Adresse ohnehin nur aus der Domain besteht.
    """
    s = _split(url)
    if not s:
        return set(), set()
    host, path, query = s
    tail = path + (f"?{query}" if query else "")

    def forms(t: str) -> set[str]:
        out = set()
        for sch in ("http", "https"):
            for h in (host, "www." + host):
                out.add(f"{sch}://{h}{t}")
                if not query:
                    out.add(f"{sch}://{h}{t}/")
        return {v for v in out if not _IRI_BAD.search(v)}

    full = forms(tail)
    domain = forms("") - full if tail else set()
    return full, domain


def same_website(a: str, b: str) -> bool:
    return bool(norm_url(a)) and norm_url(a) == norm_url(b)


# --- Stufe 1: sameAs ------------------------------------------------------------------
def present_values(item: WikidataItem | None, deprecated: bool = False) -> set[str]:
    if item is None:
        return set()
    sts = item.values("P7704", include_deprecated=True)
    return {normalize("P7704", s.value) or s.value for s in sts if (s.rank == "deprecated") == deprecated}


def choose_org(orgs: list[Org]) -> tuple[Org | None, str]:
    """Europeana-Dubletten (mehrere Organisationen → dieselbe QID): eindeutig die meisten Objekte."""
    if len(orgs) == 1:
        return orgs[0], ""
    ranked = sorted(orgs, key=lambda o: o.record_count, reverse=True)
    detail = ", ".join(f"{o.value}: {o.record_count} Objekte" for o in ranked)
    if ranked[0].record_count > ranked[1].record_count:
        return ranked[0], f"Europeana-Dublette, gewählt nach Objekten ({detail})"
    return None, f"Europeana-Dublette ohne klaren Favoriten ({detail}) – manuell wählen"


def classify_org(qid: str, orgs: list[Org], item: WikidataItem | None, wd_names, is_org: bool,
                 value_owner: dict[str, set[str]]) -> tuple[Finding, Org | None]:
    """P7704-Befund für ein Item und die Europeana-Organisationen mit sameAs auf dieses Item."""
    ids = [o.value for o in orgs]
    present = present_values(item)
    f = Finding("P7704", "", qid, "europeana", ",".join(o.num for o in orgs), "|".join(ids), sorted(present))
    hit = [o for o in orgs if o.value in present]
    if hit:
        f.status, f.source_id, f.source_value = IDENTISCH, hit[0].num, hit[0].value
        if len(orgs) > 1:
            f.note = f"Europeana-Dublette: {', '.join(ids)}"
        return f, hit[0]
    if present:
        f.status = KONFLIKT
        f.note = f"Wikidata führt {', '.join(sorted(present))}, Europeana verknüpft {', '.join(ids)}"
        return f, None
    if item is None:
        f.status, f.note = KEIN_TREFFER, "QID existiert nicht (gelöscht?)"
        return f, None
    if not is_org:
        f.status, f.note = KEIN_ORGANISATION, f"P31: {', '.join(item.p31) or '–'} – keine Organisation, nur Bericht"
        return f, None
    org, note = choose_org(orgs)
    f.note = note
    if org is None:
        f.status = MEHRDEUTIG
        return f, None
    f.source_id, f.source_value = org.num, org.value
    others = value_owner.get(org.value, set()) - {qid}
    if others:
        f.status = MEHRDEUTIG
        f.note = _join(f.note, f"{org.value} hängt bereits an {', '.join(sorted(others))}")
        return f, None
    if org.value in present_values(item, deprecated=True):
        f.status = ABGELEHNT
        f.note = _join(f.note, f"{org.value} steht in Wikidata mit Rang „veraltet“")
        return f, None
    same, ratio = name_check(org, wd_names)
    f.action = "new"
    if same:
        f.status = FEHLT if org.record_count else OHNE_OBJEKTE
        if not org.record_count:
            f.note = _join(f.note, "Europeana: 0 verknüpfte Objekte")
    else:
        f.status = NAME_PRUEFEN
        f.note = _join(f.note, f"kein Name gleich (Ähnlichkeit {ratio:.2f})"
                       + ("; Europeana: 0 verknüpfte Objekte" if not org.record_count else ""))
    return f, org


def multi_qid(org: Org, items: dict[str, WikidataItem]) -> Finding:
    """Europeana nennt mehrere QIDs: identisch, wenn eine davon den Wert führt, sonst mehrdeutig."""
    f = Finding("P7704", MEHRDEUTIG, "", "europeana", org.num, org.value, [],
                note=f"sameAs nennt mehrere QIDs: {', '.join(org.qids)}")
    for q in org.qids:
        if org.value in present_values(items.get(q)):
            f.status, f.qid, f.note = IDENTISCH, q, f.note + f"; Wert steht an {q}"
    return f


# --- Stufe 2: Kandidaten ohne sameAs ------------------------------------------------------
@dataclass
class Candidate:
    qid: str
    via: set[str]  # website | domain | P227 | P214 | P213
    same_name: bool = False
    ratio: float = 0.0


def classify_candidate(org: Org, cands: dict[str, set[str]], items: dict[str, WikidataItem],
                       names: dict[str, list[str]], is_org: dict[str, bool],
                       claimed: dict[str, list[str]], value_owner: dict[str, set[str]]) -> Finding:
    """Befund für eine Organisation ohne sameAs. `cands` = QID → Fundwege.

    `claimed` = QID → Europeana-Organisationen, deren sameAs auf das Item zeigt (dann kein Kandidat).
    """
    f = Finding("P7704", KEIN_TREFFER, None, "europeana", org.num, org.value, [])
    owners = value_owner.get(org.value, set())
    if owners:
        f.status, f.qid = (IDENTISCH, next(iter(owners))) if len(owners) == 1 else (MEHRDEUTIG, None)
        f.note = f"Wikidata führt den Wert an {', '.join(sorted(owners))}; Europeana ohne sameAs"
        return f
    notes, pool = [], []
    for q, via in sorted(cands.items()):
        it = items.get(q)
        if it is None or not (it.labels or it.p31 or it.claims):
            continue
        if not is_org.get(q):
            notes.append(f"{q} keine Organisation")
            continue
        if claimed.get(q):
            notes.append(f"{q} ist sameAs-Ziel von {', '.join('organisation/' + n for n in claimed[q])}")
            continue
        same, ratio = name_check(org, names.get(q, []))
        pool.append(Candidate(q, set(via), same, ratio))
    strong = [c for c in pool if c.same_name and c.via & STRONG_VIA]
    weak = [c for c in pool if c not in strong and
            ((c.same_name and "domain" in c.via) or (c.ratio >= WEAK_RATIO and c.via & STRONG_VIA))]
    chosen, status = None, KEIN_TREFFER
    if len(strong) == 1:
        chosen, status = strong[0], KANDIDAT_STARK
    elif len(strong) > 1:
        status = MEHRDEUTIG
        notes.append(f"mehrere starke Kandidaten: {', '.join(c.qid for c in strong)}")
    elif len(weak) == 1:
        chosen, status = weak[0], KANDIDAT_SCHWACH
    elif len(weak) > 1:
        status = MEHRDEUTIG
        notes.append(f"mehrere schwache Kandidaten: {', '.join(c.qid for c in weak)}")
    elif pool:
        notes.append("nur Website/Kennung, Name passt nicht: "
                     + ", ".join(f"{c.qid} ({c.ratio:.2f})" for c in pool))
    f.status = status
    if chosen:
        item = items[chosen.qid]
        present = present_values(item)
        f.qid, f.wikidata_values = chosen.qid, sorted(present)
        via = ", ".join(sorted(chosen.via))
        notes.insert(0, f"über {via}" + ("" if chosen.same_name else f", Name ähnlich ({chosen.ratio:.2f})"))
        if present:
            f.status = KONFLIKT
            notes.append(f"Item führt bereits {', '.join(sorted(present))}")
        elif org.value in present_values(item, deprecated=True):
            f.status = ABGELEHNT
            notes.append(f"{org.value} steht in Wikidata mit Rang „veraltet“")
        else:
            f.action = "new"
            if not org.record_count:
                notes.append("Europeana: 0 verknüpfte Objekte")
    f.note = "; ".join(notes)
    return f


# --- QuickStatements ------------------------------------------------------------------
def qs_line(f: Finding, date: str) -> str:
    from .export import _qs_string, _reference

    return f"{f.qid}|P7704|{_qs_string(f.source_value)}{_reference('europeana', date)}"


def qs_groups(findings: list[Finding], date: str, orgs: dict[str, Org], labels: dict[str, str],
              commented: set[str]) -> list[list[str]]:
    """Befehle je Befund; Status in `commented` werden mit '#' auskommentiert und erhalten
    darüber eine Kommentarzeile mit beiden Namen und dem Hinweis."""
    groups = []
    for f in findings:
        if f.action != "new" or not f.qid or not f.source_value:
            continue
        line = qs_line(f, date)
        if f.status in commented:
            org = orgs.get(f.source_id)
            info = (f"# {f.source_value} „{_flat(org.label if org else '')}“ ↔ {f.qid} "
                    f"„{_flat(labels.get(f.qid, ''))}“ – {_flat(f.note)}")
            groups.append([info, "#" + line])
        else:
            groups.append([line])
    return groups


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _join(a: str, b: str) -> str:
    return "; ".join(x for x in (a, b) if x)

