"""Abgleich von Registern ohne gemeinsamen Schlüssel (ISIL-Verzeichnis, Museen in Österreich).

Klassifikation je Register-Eintrag:
  vorhanden          Wert steht bereits an genau einem Item (exakter Schlüssel)
  vorhanden_pruefen  … aber der Name passt kaum zum Item → Bericht
  mehrdeutig         Wert an mehreren Items, oder mehrere/konkurrierende Kandidaten
  kandidat_stark     ein Item ohne diesen Wert mit GLEICHEM Namen UND GLEICHER Website
                     → QuickStatement in eigener Kandidaten-Datei (Handprüfung!)
  konflikt           starker Kandidat hat bereits einen anderen Wert dieser Property → Bericht
  kandidat_schwach   nur Name gleich, oder Website gleich + Name ähnlich, oder Name ähnlich + < 1 km
                     → nur CSV
  kein_treffer

Zusätzlich: Wikidata-Werte, die im Register nicht (mehr) vorkommen → Bericht.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from .models import WikidataItem
from .normalize import compare_key
from .sources.registry import RegistryRecord

VORHANDEN = "vorhanden"
VORHANDEN_PRUEFEN = "vorhanden_pruefen"
MEHRDEUTIG = "mehrdeutig"
STARK = "kandidat_stark"
KONFLIKT = "konflikt"
SCHWACH = "kandidat_schwach"
KEIN_TREFFER = "kein_treffer"
STATUSES = [VORHANDEN, VORHANDEN_PRUEFEN, STARK, SCHWACH, KONFLIKT, MEHRDEUTIG, KEIN_TREFFER]

WEAK_NAME_RATIO = 0.85
NEAR_KM = 1.0


# --- Normalisierung ---------------------------------------------------------------
def norm_name(s: str) -> str:
    """Vergleichsform eines Namens: Kleinschreibung, ß→ss, &→und, ohne Satzzeichen."""
    s = unicodedata.normalize("NFC", s or "").casefold().replace("ß", "ss").replace("&", " und ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_TRACKING = re.compile(r"^(?:utm_\w+|fbclid|gclid|sid|sessionid|phpsessid|jsessionid)$", re.I)


def norm_url(u: str) -> str:
    """Vergleichsform einer Website: ohne Schema, www., Port, Fragment, Index-Datei, Schluss-/.

    Die Query bleibt erhalten (sortiert, ohne Tracking-Parameter), weil sie bei CMS-Seiten
    (z. B. zusatzseite.aspx?id=…) die eigentliche Seite bestimmt.
    """
    u = (u or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)
    u = u.split("#")[0]
    u, _, query = u.partition("?")
    host, _, path = u.partition("/")
    host = re.sub(r":\d+$", "", host)
    host = re.sub(r"^www\d?\.", "", host)
    path = re.sub(r"(?:^|/)(?:index|start|home)\.(?:html?|php|aspx?)$", "", path).strip("/")
    params = sorted(p for p in query.split("&") if p and not _TRACKING.match(p.split("=")[0]))
    base = f"{host}/{path}" if path else host
    return f"{base}?{'&'.join(params)}" if params else base


def km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


# --- Zusatzdaten der Wikidata-Items ----------------------------------------------------
@dataclass
class Extras:
    websites: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    coords: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)


@dataclass
class RegistryMatch:
    record: RegistryRecord
    status: str
    qids: list[str] = field(default_factory=list)
    note: str = ""
    score: float | None = None


class Pool:
    """Index über die Kandidaten-Items (Namen, Websites, Koordinaten)."""

    def __init__(self, items: dict[str, WikidataItem], extras: dict[str, Extras]) -> None:
        self.items = items
        self.extras = extras
        self.by_name: dict[str, set[str]] = defaultdict(set)
        self.by_url: dict[str, set[str]] = defaultdict(set)
        self.grid: dict[tuple[int, int], set[str]] = defaultdict(set)
        self.names: dict[str, set[str]] = {}
        for q, it in items.items():
            ex = extras.get(q, Extras())
            names = {norm_name(n) for n in list(it.labels.values()) + ex.aliases} - {""}
            self.names[q] = names
            for n in names:
                self.by_name[n].add(q)
            for w in ex.websites:
                if norm_url(w):
                    self.by_url[norm_url(w)].add(q)
            for lat, lon in ex.coords:
                self.grid[(int(lat * 50), int(lon * 50))].add(q)

    def near(self, lat: float, lon: float, radius_km: float = NEAR_KM) -> set[str]:
        """Items im Umkreis (Raster ~1,3–2,2 km, danach exakte Distanz)."""
        gx, gy = int(lat * 50), int(lon * 50)
        cands = set().union(*(self.grid.get((gx + dx, gy + dy), set()) for dx in (-1, 0, 1) for dy in (-1, 0, 1)))
        return {q for q in cands if any(km(lat, lon, a, b) <= radius_km for a, b in self.extras[q].coords)}

    def name_ratio(self, name: str, qid: str) -> float:
        return max((name_similarity(name, n) for n in self.names.get(qid, ())), default=0.0)


def name_similarity(a: str, b: str) -> float:
    """Ähnlichkeit normalisierter Namen; ist der kürzere (≥ 2 Wörter) als Wortfolge im längeren
    enthalten (z. B. 'weltmuseum wien' in 'weltmuseum wien neue burg'), gilt mindestens 0,9."""
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    short, long_ = sorted((a, b), key=len)
    if len(short.split()) >= 2 and f" {short} " in f" {long_} ":
        ratio = max(ratio, 0.9)
    return ratio


def record_names(r: RegistryRecord) -> set[str]:
    return {norm_name(n) for n in [r.name, *r.alt_names]} - {""}


def classify_registry(
    records: list[RegistryRecord],
    pool: Pool,
    value_index: dict[str, set[str]],  # Vergleichsschlüssel → QIDs (ganz Wikidata)
    item_values: dict[str, list[str]],  # QID → vorhandene Werte der Property (Pool-Items)
) -> list[RegistryMatch]:
    prop = records[0].prop if records else "P791"
    out: list[RegistryMatch] = []
    strong_by_item: dict[str, list[RegistryMatch]] = defaultdict(list)

    for r in records:
        names = record_names(r)
        main = norm_name(r.name)
        urls = {norm_url(w) for w in r.websites} - {""}

        # 1. Wert schon in Wikidata?
        have = value_index.get(compare_key(prop, r.value), set())
        if len(have) > 1:
            out.append(RegistryMatch(r, MEHRDEUTIG, sorted(have), "Wert an mehreren Items"))
            continue
        if have:
            q = next(iter(have))
            ratio = pool.name_ratio(main, q) if q in pool.items else None
            if ratio is not None and ratio < 0.5:
                out.append(RegistryMatch(r, VORHANDEN_PRUEFEN, [q], f"Name passt kaum zum Item ({ratio:.2f})", ratio))
            else:
                out.append(RegistryMatch(r, VORHANDEN, [q], score=ratio))
            continue

        # 2. starke Kandidaten: gleicher Name UND gleiche Website
        by_name = set().union(*(pool.by_name.get(n, set()) for n in names)) if names else set()
        by_url = set().union(*(pool.by_url.get(u, set()) for u in urls)) if urls else set()
        strong = by_name & by_url
        if len(strong) > 1:
            out.append(RegistryMatch(r, MEHRDEUTIG, sorted(strong), "mehrere Items mit gleichem Namen und gleicher Website"))
            continue
        if strong:
            q = next(iter(strong))
            other = [v for v in item_values.get(q, []) if compare_key(prop, v) != compare_key(prop, r.value)]
            if other:
                out.append(RegistryMatch(r, KONFLIKT, [q], f"Item hat bereits {prop} = {', '.join(other)}"))
            else:
                same_names = sorted(names & pool.names.get(q, set()))
                same_urls = sorted(urls & {norm_url(w) for w in pool.extras.get(q, Extras()).websites})
                via = "Hauptname" if main in same_names else "Kurzform/Alias"
                m = RegistryMatch(r, STARK, [q], f"Name ({via}: {same_names[0]}) und Website ({same_urls[0]}) gleich", 1.0)
                strong_by_item[q].append(m)
                out.append(m)
            continue

        # 3. schwache Kandidaten (nur Bericht)
        weak: dict[str, tuple[float, str]] = {}
        for q in by_name:
            weak[q] = (1.0, "Name gleich, Website abweichend/fehlt")
        for q in by_url:
            ratio = pool.name_ratio(main, q)
            if ratio >= 0.6:
                weak.setdefault(q, (ratio, f"Website gleich, Name ähnlich ({ratio:.2f})"))
        if r.lat is not None and r.lon is not None:
            for q in pool.near(r.lat, r.lon):
                ratio = pool.name_ratio(main, q)
                if ratio >= WEAK_NAME_RATIO:
                    weak.setdefault(q, (ratio, f"Name ähnlich ({ratio:.2f}), < {NEAR_KM:g} km"))
        if weak:
            best = sorted(weak.items(), key=lambda kv: -kv[1][0])
            out.append(RegistryMatch(r, SCHWACH, [q for q, _ in best], "; ".join(f"{q}: {n}" for q, (_, n) in best),
                                     best[0][1][0]))
        else:
            out.append(RegistryMatch(r, KEIN_TREFFER))

    # Ein Item darf nur einen starken Kandidaten bekommen (z. B. Museum + Bibliothek gleichen Namens)
    for q, ms in strong_by_item.items():
        if len(ms) > 1:
            for m in ms:
                m.status = MEHRDEUTIG
                m.note = f"{q} ist starker Kandidat für mehrere Einträge: {', '.join(x.record.value for x in ms)}"
    return out


UNGUELTIG = "ungueltig"
NICHT_IM_REGISTER = "nicht_im_register"


@dataclass
class Cleanup:
    """Bereinigungsvorschlag für einen Wikidata-Wert."""

    qid: str
    value: str
    kind: str  # ungueltig | nicht_im_register
    action: str | None  # "delete" oder None (nur Bericht)
    note: str = ""


def registry_cleanup(
    statements: list[dict], registry_keys: set[str], prop: str, prefix: str | None = None
) -> list[Cleanup]:
    """Ungültige Werte und Werte, die im Register fehlen → Löschvorschläge.

    Wie bei Korrekturen wird nur vorgeschlagen, wenn die Aussage weder Quellen noch
    Qualifier hat; sonst nur Bericht.
    """
    from .normalize import normalize

    out = []
    for s in statements:
        v = s["value"]
        if prefix and not v.upper().startswith(prefix.upper()):
            continue
        norm = normalize(prop, v)
        if norm is None:
            kind, note = UNGUELTIG, "ungültiges Format"
        elif compare_key(prop, norm) not in registry_keys:
            kind, note = NICHT_IM_REGISTER, "nicht im Register (aufgelöst, Tippfehler oder anderes Register?)"
        else:
            continue
        if s.get("has_refs") or s.get("has_qualifiers"):
            out.append(Cleanup(s["qid"], v, kind, None, note + "; Aussage hat Quellen/Qualifier – manuell prüfen"))
        else:
            out.append(Cleanup(s["qid"], v, kind, "delete", note + "; Löschung vorgeschlagen"))
    return sorted(out, key=lambda c: (c.kind, c.qid, c.value))


def wikidata_not_in_registry(
    value_rows: list[tuple[str, str]], registry_keys: set[str], prop: str, prefix: str | None = None
) -> list[tuple[str, str]]:
    """(QID, Wert)-Paare aus Wikidata, deren Wert im Register fehlt (Präfix z. B. 'DE-')."""
    out = []
    for q, v in value_rows:
        if prefix and not v.upper().startswith(prefix.upper()):
            continue
        if compare_key(prop, v) not in registry_keys:
            out.append((q, v))
    return sorted(out)
