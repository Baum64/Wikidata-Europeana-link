"""Erkundung des GND-Dumps (Körperschaften) der DNB.

Aufruf:  python -m explore.explore_gnd [--path DUMP] [--limit N] [--wikidata-sample 300]

Gelesen werden lokale Dumps (Bezugsquelle und Download siehe README, Abschnitt „GND“):
  --path          Körperschaften   authorities-gnd-koerperschaft_lds_<datum>.jsonld.gz  (Standard: GND_DUMP_PATH)
  --subjects      Sachbegriffe     authorities-gnd-sachbegriff_lds_<datum>.jsonld.gz   (Standard: im selben Ordner)
  --places        Geografika       authorities-gnd-geografikum_lds_<datum>.jsonld.gz   (Standard: im selben Ordner)

Prüft:
 1. Aufbau des Dumps (Knotenarten, defekte Knoten)
 2. Typen (@type) und Anteile der Felder: GND-ID, Name, Ort, Land,
    Körperschaftstyp, ISIL, Wikidata, VIAF, ISNI
 3. Körperschaftstyp Museum/Bibliothek/Archiv über die Sachbegriff-Hierarchie
    (Oberbegriff instantiell → Sachbegriff → … → Museum/Bibliothek/Archiv) und Sachgruppen
 4. Teilmenge DE/AT/CH je Typ: Feldanteile, Ortsnamen
 5. Wikidata: Fundstellen-Item für P227 und P227-Lage einer Stichprobe verlinkter Items
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path

from glamsync.http import CachedClient
from glamsync.normalize import normalize_gnd
from glamsync.sources.gnd import iter_nodes  # Streaming-Parser (gemeinsam mit dem Tool)
from glamsync.sources.wikidata import Wikidata

log = logging.getLogger("explore_gnd")

GND = "https://d-nb.info/gnd/"
GNDO = "https://d-nb.info/standards/elementset/gnd#"
SAMEAS = "http://www.w3.org/2002/07/owl#sameAs"
SC = "https://d-nb.info/standards/vocab/gnd/gnd-sc#"
LOBID = "https://lobid.org/gnd"
# ISIL-förmige Literale (Länderkürzel, Bindestrich, Kennung), z. B. DE-1, DE-MUS-123456
ISIL_LITERAL_RE = re.compile(r"^[A-Z]{2}-[A-Za-z0-9:/\-]{1,13}$")
NAME_RE = re.compile(r"museum|bibliothek|bücherei|archiv", re.I)
# Wurzel-Sachbegriffe: werden per exaktem Namen im Sachbegriff-Dump gesucht, nicht vorgegeben
ROOT_NAMES = {"museum": "Museum", "library": "Bibliothek", "archive": "Archiv"}
# Hierarchie-Prädikate für „ist Unterbegriff von“ (ohne broaderTermPartitive = „Teil von“)
BROADER_SUBJECT = (GNDO + "broaderTermGeneral", GNDO + "broaderTermGeneric")
SC_LIBRARY, SC_ARCHIVE_MUSEUM = "6.7", "6.8"


def h(title: str) -> None:
    print(f"\n=== {title} ===")


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def pct(n: int, total: int) -> str:
    return f"{fmt(n):>9} ({100 * n / total:5.1f} %)" if total else f"{n:>9}"


def ids(node: dict, pred: str) -> list[str]:
    return [v["@id"] for v in node.get(pred, []) if isinstance(v, dict) and "@id" in v]


def vals(node: dict, pred: str) -> list[str]:
    return [str(v["@value"]) for v in node.get(pred, []) if isinstance(v, dict) and "@value" in v]


def short(uri: str) -> str:
    return uri.split("#", 1)[-1] if "#" in uri else uri.rsplit("/", 1)[-1]


def gid(uri: str) -> str:
    return normalize_gnd(uri) or short(uri)


def is_record(node: dict) -> bool:
    nid = node.get("@id", "")
    return nid.startswith(GND) and not nid.endswith("/about")


def compact(node: dict) -> dict:
    """Die für den Abgleich interessanten Felder eines Körperschafts-Knotens."""
    same = ids(node, SAMEAS)
    all_ids = [i for p in node if not p.startswith("@") for i in ids(node, p)]
    literals = [v for p in node if not p.startswith("@") for v in vals(node, p)]
    return {
        "uri": node["@id"],
        "gnd": (vals(node, GNDO + "gndIdentifier") or [None])[0],
        "types": [short(t) for t in node.get("@type", [])],
        "name": (vals(node, GNDO + "preferredNameForTheCorporateBody") or [None])[0],
        "places": [gid(p) for p in ids(node, GNDO + "placeOfBusiness")],
        "areas": [short(a) for a in ids(node, GNDO + "geographicAreaCode")],
        "broader": [gid(b) for b in ids(node, GNDO + "broaderTermInstantial")],
        "sc": [short(s) for s in ids(node, GNDO + "gndSubjectCategory")],
        "superior": ids(node, GNDO + "hierarchicalSuperiorOfTheCorporateBody"),
        "terminated": bool(vals(node, GNDO + "dateOfTermination")),
        "homepage": ids(node, GNDO + "homepage"),
        "wikidata": [short(u) for u in same if "wikidata.org/entity/" in u],
        "wikidata_other_pred": [p for p in node if p != SAMEAS and any("wikidata.org" in i for i in ids(node, p))],
        "viaf": [u for u in same if "viaf.org/viaf/" in u],
        "isni": [u for u in same if "isni.org/isni/" in u],
        "zdb_org": [u for u in all_ids if "zdb-services.de/resource/organisations/" in u],
        "isil_literals": [v for v in literals if ISIL_LITERAL_RE.match(v)],
        "isil_preds": [p for p in node if "isil" in p.lower()],
        "deprecated": vals(node, "https://d-nb.info/standards/elementset/dnb#deprecatedUri"),
    }


def country(rec: dict) -> str | None:
    for code in ("DE", "AT", "CH"):
        if any(a == f"XA-{code}" or a.startswith(f"XA-{code}-") for a in rec["areas"]):
            return code
    return None


# ---------------------------------------------------------------------------
def scan(path: Path, limit: int | None) -> tuple[list[dict], Counter, Counter]:
    """Liest den Körperschafts-Dump; liefert alle Datensätze (kompakt) und Zähler."""
    stats, preds = Counter(), Counter()
    records: list[dict] = []
    t0 = time.time()
    for node in iter_nodes(path, stats):
        nid = node.get("@id", "")
        if nid.startswith("_:"):
            stats["knoten_leer (Beziehungen u. ä.)"] += 1
            continue
        if nid.endswith("/about"):
            stats["knoten_about (Verwaltungsdaten)"] += 1
            continue
        if not nid.startswith(GND):
            stats["knoten_sonstige"] += 1
            continue
        stats["knoten_datensatz"] += 1
        preds.update(node.keys())
        try:
            records.append(compact(node))
        except Exception as exc:  # defekter Satz: zählen, loggen, weiter
            stats["defekt_datensatz"] += 1
            log.warning("Datensatz %s nicht auswertbar: %s", nid, exc)
        if len(records) % 200_000 == 0:
            log.info("%s Datensätze, %.0f s", fmt(len(records)), time.time() - t0)
        if limit and len(records) >= limit:
            break
    stats["dauer_s"] = int(time.time() - t0)
    return records, stats, preds


def load_subjects(path: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Sachbegriffe: GND → Name, GND → direkte Oberbegriffe (allgemein/generisch)."""
    labels: dict[str, str] = {}
    parents: dict[str, set[str]] = defaultdict(set)
    stats = Counter()
    for node in iter_nodes(path, stats):
        if not is_record(node):
            continue
        g = gid(node["@id"])
        labels[g] = (vals(node, GNDO + "preferredNameForTheSubjectHeading") or ["?"])[0]
        for p in BROADER_SUBJECT:
            parents[g].update(gid(u) for u in ids(node, p))
    if stats:
        print("Sachbegriff-Dump, Defekte:", dict(stats))
    return labels, parents


def load_places(path: Path, wanted: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    stats = Counter()
    for node in iter_nodes(path, stats):
        if is_record(node) and (g := gid(node["@id"])) in wanted:
            out[g] = (vals(node, GNDO + "preferredNameForThePlaceOrGeographicName") or ["?"])[0]
    if stats:
        print("Geografikum-Dump, Defekte:", dict(stats))
    return out


def subject_types(labels: dict[str, str], parents: dict[str, set[str]]) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Wurzeln per exaktem Namen; liefert {typ: wurzel-gnd} und {sachbegriff: {typen}}."""
    roots: dict[str, str] = {}
    for typ, name in ROOT_NAMES.items():
        hits = [g for g, l in labels.items() if l == name]
        if len(hits) != 1:
            raise SystemExit(f"Wurzelbegriff {name!r} nicht eindeutig im Sachbegriff-Dump: {hits}")
        roots[typ] = hits[0]
    root_type = {g: t for t, g in roots.items()}
    memo: dict[str, set[str]] = {}

    def types_of(g: str, seen: frozenset = frozenset()) -> set[str]:
        if g in memo:
            return memo[g]
        if g in seen:  # Zyklus
            return set()
        res = {root_type[g]} if g in root_type else set()
        for p in parents.get(g, ()):
            res |= types_of(p, seen | {g})
        memo[g] = res
        return res

    return roots, {g: t for g in labels if (t := types_of(g))}


# ---------------------------------------------------------------------------
def report_general(records: list[dict], stats: Counter, preds: Counter) -> None:
    total = len(records)
    h("1. Aufbau")
    for k, v in sorted(stats.items()):
        print(f"{k:<40} {fmt(v):>12}")

    h("2a. Typen (@type, Mehrfachnennung möglich)")
    for t, n in Counter(t for r in records for t in r["types"]).most_common():
        print(f"{t:<45} {pct(n, total)}")

    h("2b. Prädikate in Datensatz-Knoten")
    for p, n in preds.most_common():
        print(f"{short(p) if p.startswith(GNDO) else p:<60} {pct(n, total)}")

    h("2c. Feldanteile (alle Körperschaften)")
    field_table(records)
    gnd_mismatch = [r for r in records if normalize_gnd(r["gnd"]) != normalize_gnd(r["uri"])]
    bad_format = [r for r in records if not normalize_gnd(r["gnd"])]
    print(f"\ngndIdentifier ≠ @id: {len(gnd_mismatch)}; GND-ID ohne gültiges Format: {len(bad_format)}")
    for r in bad_format[:5]:
        print("   ", r["gnd"], r["name"])
    x_check = Counter(r["gnd"][-1] == "X" for r in records if r["gnd"])
    print(f"GND-IDs mit Prüfziffer X: {fmt(x_check[True])}")
    print("ISIL-Prädikate:", sorted({p for r in records for p in r["isil_preds"]}) or "keine")
    lit = Counter(v for r in records for v in r["isil_literals"])
    print(f"ISIL-förmige Literale: {sum(lit.values())} in {sum(1 for r in records if r['isil_literals'])} "
          f"Datensätzen (Namensteile, keine ISIL); Beispiele: {lit.most_common(6)}")
    print(f"ZDB-Organisationslinks im Datensatz-Knoten (nicht /about): {sum(1 for r in records if r['zdb_org'])}")
    other = Counter(p for r in records for p in r["wikidata_other_pred"])
    print("Wikidata-Links außerhalb owl:sameAs:", {short(k): v for k, v in other.items()} or "keine")

    h("2d. Länder (geographicAreaCode)")
    for code in ("DE", "AT", "CH"):
        print(f"XA-{code}*: {pct(sum(1 for r in records if country(r) == code), total)}")
    print(f"XA-DDDE (DDR): {pct(sum(1 for r in records if 'XA-DDDE' in r['areas']), total)}")
    print(f"XA-DXDE (Deutsches Reich): {pct(sum(1 for r in records if 'XA-DXDE' in r['areas']), total)}")
    print("ohne Ländercode:", pct(sum(1 for r in records if not r["areas"]), total))
    print("Häufigste Codes:", Counter(a for r in records for a in r["areas"]).most_common(12))


def report_types(records: list[dict], subj_labels: dict[str, str], roots: dict[str, str],
                 subj_types: dict[str, set[str]], top: int) -> dict[str, list[dict]]:
    h("3a. Wurzelbegriffe und Unterbegriffe (Sachbegriff-Dump)")
    per_type = Counter(t for ts in subj_types.values() for t in ts)
    for typ, g in roots.items():
        print(f"{typ:<8} {g:<12} {subj_labels[g]:<12} {fmt(per_type[typ]):>6} Sachbegriffe inkl. Unterbegriffe")
    print(f"Sachbegriffe unter mehreren Wurzeln: {sum(1 for ts in subj_types.values() if len(ts) > 1)}"
          f"  z. B. {[subj_labels[g] for g, ts in subj_types.items() if len(ts) > 1][:8]}")

    for r in records:
        r["glam"] = sorted({t for b in r["broader"] for t in subj_types.get(b, ())})

    h("3b. Körperschaften je Typ und Land (über Oberbegriff instantiell)")
    print(f"{'':<10}{'DE':>8}{'AT':>8}{'CH':>8}{'gesamt':>9}")
    for typ in ROOT_NAMES:
        c = Counter(country(r) for r in records if typ in r["glam"])
        print(f"{typ:<10}{fmt(c['DE']):>8}{fmt(c['AT']):>8}{fmt(c['CH']):>8}{fmt(sum(c.values())):>9}")
    multi = [r for r in records if len(r["glam"]) > 1]
    print(f"mehrere Typen: {fmt(len(multi))}, z. B. {[(r['name'][:40], r['glam']) for r in multi[:4]]}")

    de = [r for r in records if country(r) == "DE"]
    for typ in ROOT_NAMES:
        h(f"3c. Häufigste Oberbegriffe für {typ} (DE), Top {top}")
        c = Counter(b for r in de for b in r["broader"] if typ in subj_types.get(b, ()))
        print(", ".join(f"{subj_labels.get(b, b)} {n}" for b, n in c.most_common(top)))

    h("3d. Gegenprobe DE: Sachgruppe 6.7 (Bibliothek …) / 6.8 (Archiv. Museum) und Name")
    glam_de = {r["gnd"] for r in de if r["glam"]}
    by_sc = [r for r in de if {SC_LIBRARY, SC_ARCHIVE_MUSEUM} & set(r["sc"])]
    by_name = [r for r in de if r["name"] and NAME_RE.search(r["name"])]
    print(f"DE gesamt {fmt(len(de))}; Oberbegriff-Hierarchie {fmt(len(glam_de))}; "
          f"Sachgruppe 6.7/6.8 {fmt(len(by_sc))} (davon auch Hierarchie {fmt(sum(r['gnd'] in glam_de for r in by_sc))}); "
          f"Name {fmt(len(by_name))} (davon auch Hierarchie {fmt(sum(r['gnd'] in glam_de for r in by_name))})")
    sc_only = [r for r in by_sc if r["gnd"] not in glam_de]
    print(f"Sachgruppe 6.7/6.8 ohne passenden Oberbegriff ({fmt(len(sc_only))}), deren Oberbegriffe:",
          Counter(subj_labels.get(b, b) for r in sc_only for b in r["broader"]).most_common(15),
          f"; ohne Oberbegriff: {fmt(sum(1 for r in sc_only if not r['broader']))}")
    print("   Beispiele:", [r["name"][:50] for r in sc_only[:10]])
    name_only = [r for r in by_name if r["gnd"] not in glam_de]
    print(f"Name passt, kein passender Oberbegriff ({fmt(len(name_only))}), deren Oberbegriffe:",
          Counter(subj_labels.get(b, b) for r in name_only for b in r["broader"]).most_common(15))
    print("   Beispiele:", [r["name"][:50] for r in name_only[:10]])
    return {typ: [r for r in records if typ in r["glam"]] for typ in ROOT_NAMES}


def report_subsets(by_type: dict[str, list[dict]], place_labels: dict[str, str]) -> None:
    for code in ("DE", "AT", "CH"):
        for typ, rs in by_type.items():
            sub = [r for r in rs if country(r) == code]
            if code != "DE":
                print(f"\n{code} {typ}: {fmt(len(sub))}, Wikidata {pct(sum(1 for r in sub if r['wikidata']), len(sub))}")
                continue
            h(f"4. {code}, {typ}: Feldanteile")
            field_table(sub)
            with_place = [r for r in sub if r["places"]]
            resolved = [r for r in with_place if all(p in place_labels for p in r["places"])]
            print(f"   {'Ortsname auflösbar (Geografikum-Dump)':<40} {pct(len(resolved), len(with_place))} der Sätze mit Ort")
            print("   Beispiele mit Wikidata:")
            for r in [r for r in sub if r["wikidata"]][:4]:
                print(f"      {r['gnd']:<12} {r['name'][:45]:<45} {r['wikidata'][0]:<11} "
                      f"Ort={[place_labels.get(p, p) for p in r['places']]}")


def field_table(rs: list[dict]) -> None:
    n = len(rs)
    if not n:
        print("   (leer)")
        return
    rows = [
        ("GND-ID (gndIdentifier)", lambda r: r["gnd"]),
        ("Name (preferredName)", lambda r: r["name"]),
        ("Ort (placeOfBusiness, GND-URI)", lambda r: r["places"]),
        ("Land (geographicAreaCode)", lambda r: r["areas"]),
        ("Oberbegriff (broaderTermInstantial)", lambda r: r["broader"]),
        ("Sachgruppe (gndSubjectCategory)", lambda r: r["sc"]),
        ("übergeordnete Körperschaft", lambda r: r["superior"]),
        ("aufgelöst (dateOfTermination)", lambda r: r["terminated"]),
        ("Homepage", lambda r: r["homepage"]),
        ("ISIL (Feld)", lambda r: r["isil_preds"]),
        ("Wikidata (owl:sameAs)", lambda r: r["wikidata"]),
        ("  davon mehr als eine QID", lambda r: len(r["wikidata"]) > 1),
        ("VIAF (owl:sameAs)", lambda r: r["viaf"]),
        ("  davon mehr als eine", lambda r: len(r["viaf"]) > 1),
        ("ISNI (owl:sameAs)", lambda r: r["isni"]),
        ("alte GND-URIs (deprecatedUri)", lambda r: r["deprecated"]),
    ]
    print(f"   {'Feld':<40} von {fmt(n)}")
    for label, f in rows:
        print(f"   {label:<40} {pct(sum(1 for r in rs if f(r)), n)}")


def wikidata_check(client: CachedClient, glam: list[dict], sample: int) -> None:
    wd = Wikidata(client)
    h("5a. Fundstellen-Item für P227 (per SPARQL)")
    for prop, what in (("P9073", "anwendbarer „angegeben in“-Wert"), ("P1629", "Item zu dieser Eigenschaft")):
        rows = wd.query(f"""SELECT ?x ?l WHERE {{ wd:P227 wdt:{prop} ?x .
  OPTIONAL {{ ?x rdfs:label ?l FILTER(LANG(?l)="de") }} }}""")
        print(f"P227 → {prop} ({what}):", [(short(r["x"]), r.get("l")) for r in rows])
    rows = wd.query("""SELECT ?src ?l (COUNT(*) AS ?n) WHERE {
  VALUES ?typ { wd:Q33506 wd:Q7075 wd:Q166118 }
  ?item wdt:P31 ?typ ; wdt:P17 wd:Q183 ; p:P227 ?st .
  ?st prov:wasDerivedFrom/pr:P248 ?src .
  OPTIONAL { ?src rdfs:label ?l FILTER(LANG(?l)="de") }
} GROUP BY ?src ?l ORDER BY DESC(?n) LIMIT 8""")
    print("Häufigste S248 an P227 bei Museen/Bibliotheken/Archiven in DE (direkte P31):")
    for r in rows:
        print(f"   {short(r['src']):<12} {r.get('l', ''):<50} {r['n']}")

    with_wd = [r for r in glam if country(r) == "DE" and len(r["wikidata"]) == 1]
    random.seed(1)
    picks = random.sample(with_wd, min(sample, len(with_wd)))
    h(f"5b. P227-Lage verlinkter Items (Zufallsstichprobe {len(picks)} von {fmt(len(with_wd))}, DE, Museum/Bibliothek/Archiv)")
    qid_of = {r["wikidata"][0]: r["gnd"] for r in picks}
    p227: dict[str, set[str]] = {q: set() for q in qid_of}
    exists: set[str] = set()
    q_list = sorted(qid_of)
    for i in range(0, len(q_list), 150):
        chunk = q_list[i : i + 150]
        res = wd.query(f"""SELECT ?item ?gnd WHERE {{ VALUES ?item {{ {" ".join("wd:" + q for q in chunk)} }}
  ?item wikibase:sitelinks [] . OPTIONAL {{ ?item wdt:P227 ?gnd }} }}""")
        for row in res:
            q = short(row["item"])
            exists.add(q)
            if "gnd" in row:
                p227[q].add(row["gnd"])
    c, examples = Counter(), defaultdict(list)
    for q, g in qid_of.items():
        if q not in exists:
            k = "QID existiert nicht/Weiterleitung"
        elif not p227[q]:
            k = "P227 fehlt"
        elif g in p227[q]:
            k = "P227 identisch"
        else:
            k = "P227 anderer Wert"
        c[k] += 1
        examples[k].append(f"{q}={g}")
    missing = [g for q, g in qid_of.items() if q in exists and not p227[q]]
    elsewhere = wd.lookup("P227", missing) if missing else {}
    for k, n in c.most_common():
        print(f"   {k:<38} {pct(n, len(qid_of))}   z. B. {examples[k][:3]}")
    print(f"   davon P227 fehlt, GND aber an anderem Item: {len(elsewhere)}  {list(elsewhere.items())[:3]}")


def sibling(path: Path, kind: str) -> Path | None:
    hits = sorted(path.parent.glob(f"authorities-gnd-{kind}_lds*.jsonld.gz"))
    return hits[-1] if hits else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", default=os.environ.get("GND_DUMP_PATH"), help="Körperschafts-Dump; Standard: GND_DUMP_PATH")
    ap.add_argument("--subjects", help="Sachbegriff-Dump (Standard: im selben Ordner)")
    ap.add_argument("--places", help="Geografikum-Dump (Standard: im selben Ordner)")
    ap.add_argument("--limit", type=int, default=None, help="nur die ersten N Datensätze (Stichprobe)")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--wikidata-sample", type=int, default=300, help="0 = ohne Wikidata-Abfragen")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if not a.path or not Path(a.path).is_file():
        raise SystemExit("Dump nicht gefunden: --path angeben oder GND_DUMP_PATH setzen (siehe README).")
    path = Path(a.path)
    subjects = Path(a.subjects) if a.subjects else sibling(path, "sachbegriff")
    places = Path(a.places) if a.places else sibling(path, "geografikum")
    if not subjects or not places:
        raise SystemExit("Sachbegriff- bzw. Geografikum-Dump fehlt (siehe README).")
    print(f"Dumps: {path.name} ({path.stat().st_size / 1e6:.0f} MB), {subjects.name}, {places.name}")

    records, stats, preds = scan(path, a.limit)
    report_general(records, stats, preds)
    subj_labels, parents = load_subjects(subjects)
    roots, subj_types = subject_types(subj_labels, parents)
    by_type = report_types(records, subj_labels, roots, subj_types, a.top)
    wanted = {p for rs in by_type.values() for r in rs for p in r["places"]}
    report_subsets(by_type, load_places(places, wanted))
    if a.wikidata_sample:
        glam = [r for rs in by_type.values() for r in rs]
        wikidata_check(CachedClient(), list({r["gnd"]: r for r in glam}.values()), a.wikidata_sample)


if __name__ == "__main__":
    main()
