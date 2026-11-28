"""Tests für den GND-Dump (Parser, Typfilter, Normalisierung) und den Abgleich nach Name + Ort."""

import csv
import gzip
import json
from collections import Counter

import pytest

from glamsync.export import write_mnm
from glamsync.gnd import (
    KEIN_TREFFER,
    KONFLIKT,
    MEHRDEUTIG,
    SCHWACH,
    STARK,
    VORHANDEN,
    GND_GELOESCHT,
    GND_UMGELENKT,
    HISTORISCH,
    VORGAENGER,
    qualifier_commands,
    qualifier_proposals,
    p227_redirect_findings,
    period_annotation,
    redirect_commands,
    VORHANDEN_ALT,
    GndMatch,
    GndPool,
    body_names,
    classify_gnd,
    de_aliases,
    de_description,
    de_label,
    mnm_description,
    mnm_rows,
    p227_format_findings,
    term_commands,
    term_proposals,
)
from glamsync.models import Statement, WikidataItem
from glamsync.normalize import gnd_checksum_ok, normalize_gnd, normalize_isil
from glamsync.registry import Extras, Pool
from glamsync.sources import gnd_redirects as rd
from glamsync.sources.gnd import (
    GndBody,
    exact_year,
    parse_period,
    build_table,
    country_of,
    is_corporate,
    iter_nodes,
    load_table,
    parse_wkt,
    subject_types,
)

GNDO = "https://d-nb.info/standards/elementset/gnd#"


# --- Normalisierung -----------------------------------------------------------------
@pytest.mark.parametrize("gnd", [
    "1009-1", "10098-5", "4040795-0", "4006439-6", "4002859-8", "5036103-X", "9819-X",  # alt (mit Bindestrich)
    "1147706824", "1132171555", "118540238",  # neu
])
def test_gnd_checksum_ok(gnd):
    assert gnd_checksum_ok(gnd)


@pytest.mark.parametrize("gnd", ["4040795-1", "5036103-0", "1147706825", "118540239", "", "abc", "4040795"])
def test_gnd_checksum_wrong(gnd):
    assert not gnd_checksum_ok(gnd)


@pytest.mark.parametrize("raw,expected", [
    ("https://d-nb.info/gnd/5036103-x", "5036103-X"),  # Prüfziffer x → X
    ("9819-x", "9819-X"),
    (" 1147706824 ", "1147706824"),
])
def test_normalize_gnd_x(raw, expected):
    assert normalize_gnd(raw) == expected


@pytest.mark.parametrize("raw,expected", [("de-mus-123456", "DE-MUS-123456"), ("de-Ks18", "DE-Ks18")])
def test_isil_prefix_upper(raw, expected):
    assert normalize_isil(raw) == expected


# --- Parser ---------------------------------------------------------------------------
def _dump(path, nodes, broken_after: int | None = None):
    parts = [json.dumps(n, indent=2) for n in nodes]
    if broken_after is not None:
        parts.insert(broken_after, '{\n  "@id" : "https://d-nb.info/gnd/kaputt",\n  "x" : [ {')
    text = "[ [ " + "\n}, ".join(p.rstrip("}").rstrip() for p in parts) + "\n} ] ]"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_iter_nodes_skips_broken(tmp_path):
    nodes = [{"@id": f"https://d-nb.info/gnd/{i}-1"} for i in range(3)]
    p = _dump(tmp_path / "d.jsonld.gz", nodes, broken_after=1)
    stats = Counter()
    got = [n["@id"] for n in iter_nodes(p, stats)]
    assert got == [n["@id"] for n in nodes]
    assert stats["defekt_knoten"] == 1


# --- Typfilter ------------------------------------------------------------------------
@pytest.mark.parametrize("types,ok", [
    (["CorporateBody"], True),
    (["OrganOfCorporateBody"], True),
    (["Company"], True),
    (["ConferenceOrEvent"], False),
    (["CorporateBody", "FictiveCorporateBody"], False),
    (["TerritorialCorporateBodyOrAdministrativeUnit"], False),
    (["BuildingOrMemorial"], False),
    ([], False),
])
def test_is_corporate(types, ok):
    assert is_corporate(types) is ok


@pytest.mark.parametrize("codes,land", [
    (["XA-DE-BY"], "DE"), (["XA-DE"], "DE"), (["XA-DDDE"], "DDR"), (["XA-DXDE"], "DR"),
    (["XA-DDDE", "XA-DE-SN"], "DE"), (["XA-DXDE", "XA-DDDE"], "DDR"),
    (["XA-AT-9"], "AT"), (["XA-CH"], "CH"), (["XA-FR"], ""), ([], ""), (["XA-DEX"], ""),
])
def test_country_of(codes, land):
    assert country_of(codes) == land


def test_subject_types_hierarchy():
    labels = {"m": "Museum", "b": "Bibliothek", "a": "Archiv", "km": "Kunstmuseum", "kh": "Kunsthalle",
              "ub": "Universitätsbibliothek", "x": "Verein", "z1": "Zyklus1", "z2": "Zyklus2"}
    parents = {"km": {"m"}, "kh": {"km"}, "ub": {"b"}, "z1": {"z2"}, "z2": {"z1", "a"}}
    roots, types = subject_types(labels, parents)
    assert roots == {"museum": "m", "library": "b", "archive": "a"}
    assert types["kh"] == {"museum"} and types["ub"] == {"library"}
    assert types["z1"] == {"archive"}  # Zyklus bricht nicht ab
    assert "x" not in types


def test_subject_types_root_must_be_unique():
    with pytest.raises(ValueError):
        subject_types({"m1": "Museum", "m2": "Museum", "b": "Bibliothek", "a": "Archiv"}, {})


def test_parse_wkt():
    assert parse_wkt("Point ( +009.993682 +053.551086 )") == (53.551086, 9.993682)
    assert parse_wkt("") is None


def _node(gnd, name, types=("CorporateBody",), broader=(), place=None, area="XA-DE-BW", **extra):
    n = {"@id": f"https://d-nb.info/gnd/{gnd}", "@type": [GNDO + t for t in types],
         GNDO + "gndIdentifier": [{"@value": gnd}],
         GNDO + "preferredNameForTheCorporateBody": [{"@value": name}],
         GNDO + "geographicAreaCode": [{"@id": f"https://d-nb.info/standards/vocab/gnd/geographic-area-code#{area}"}],
         GNDO + "broaderTermInstantial": [{"@id": f"https://d-nb.info/gnd/{b}"} for b in broader]}
    if place:
        n[GNDO + "placeOfBusiness"] = [{"@id": f"https://d-nb.info/gnd/{place}"}]
    n.update(extra)
    return n


def test_build_table_roundtrip(tmp_path):
    subj = [
        {"@id": "https://d-nb.info/gnd/4040795-0", GNDO + "preferredNameForTheSubjectHeading": [{"@value": "Museum"}]},
        {"@id": "https://d-nb.info/gnd/4006439-6", GNDO + "preferredNameForTheSubjectHeading": [{"@value": "Bibliothek"}]},
        {"@id": "https://d-nb.info/gnd/4002859-8", GNDO + "preferredNameForTheSubjectHeading": [{"@value": "Archiv"}]},
        {"@id": "https://d-nb.info/gnd/4336196-1", GNDO + "preferredNameForTheSubjectHeading": [{"@value": "Stadtarchiv"}],
         GNDO + "broaderTermGeneral": [{"@id": "https://d-nb.info/gnd/4002859-8"}]},
        {"@id": "https://d-nb.info/gnd/4062714-7", GNDO + "preferredNameForTheSubjectHeading": [{"@value": "Verein"}]},
    ]
    places = [
        {"@id": "_:g1", "http://www.opengis.net/ont/geosparql#asWKT": [{"@value": "Point ( +008.466 +049.488 )"}]},
        {"@id": "https://d-nb.info/gnd/4037372-1", GNDO + "preferredNameForThePlaceOrGeographicName": [{"@value": "Mannheim"}],
         "http://www.w3.org/2002/07/owl#sameAs": [{"@id": "http://www.wikidata.org/entity/Q2119"}],
         "http://www.opengis.net/ont/geosparql#hasGeometry": [{"@id": "_:g1"}]},
    ]
    bodies = [
        _node("10623-9", "Stadtarchiv Mannheim", broader=["4336196-1"], place="4037372-1",
              **{"https://d-nb.info/standards/elementset/dnb#deprecatedUri": [{"@value": "https://d-nb.info/gnd/2009424-3"}],
                 "http://www.w3.org/2002/07/owl#sameAs": [{"@id": "http://www.wikidata.org/entity/Q17592548"}]}),
        _node("1009-1", "Verein für Geschichte", broader=["4062714-7"]),  # kein GLAM-Typ
        _node("1022-4", "Archivtagung", types=("ConferenceOrEvent",), broader=["4336196-1"]),  # kein Körperschaftstyp
        {"@id": "https://d-nb.info/gnd/10623-9/about"},
    ]
    corp = _dump(tmp_path / "authorities-gnd-koerperschaft_lds_20261013.jsonld.gz", bodies, broken_after=2)
    _dump(tmp_path / "authorities-gnd-sachbegriff_lds_20261013.jsonld.gz", subj)
    _dump(tmp_path / "authorities-gnd-geografikum_lds_20261013.jsonld.gz", places)
    stats = build_table(corp, tmp_path / "t.sqlite")
    assert stats["einrichtungen"] == 1 and stats["defekt_knoten"] == 1
    (b,) = load_table(tmp_path / "t.sqlite", ["DE", "DDR"])
    assert b.gnd == "10623-9" and b.types == ["archive"] and b.broader == ["Stadtarchiv"]
    assert b.places == [{"gnd": "4037372-1", "name": "Mannheim", "qid": "Q2119", "lat": 49.488, "lon": 8.466}]
    assert b.wikidata == ["Q17592548"] and b.old_ids == ["2009424-3"] and b.country == "DE"
    assert load_table(tmp_path / "t.sqlite", ["AT"]) == []


# --- Abgleich ---------------------------------------------------------------------------
MANNHEIM = {"gnd": "4037372-1", "name": "Mannheim", "qid": "Q2119", "lat": 49.488, "lon": 8.466}
AUGSBURG = {"gnd": "4003614-5", "name": "Augsburg", "qid": "Q2749", "lat": 48.37, "lon": 10.89}


def body(gnd, name, places=(MANNHEIM,), **kw):
    return GndBody(gnd=gnd, name=name, types=kw.pop("types", ["archive"]), country=kw.pop("country", "DE"),
                   places=[dict(p) for p in places], **kw)


def item(qid, label, p227=()):
    return WikidataItem(qid, {"de": label}, claims={"P227": [Statement(v, f"{qid}$1", "normal", False, False) for v in p227]})


def make_pool(items, locations, coords=None, aliases=None, sites=None):
    items = {it.qid: it for it in items}
    extras = {q: Extras(websites=(sites or {}).get(q, []), aliases=(aliases or {}).get(q, []),
                        coords=(coords or {}).get(q, [])) for q in items}
    return GndPool(Pool(items, extras), locations)


def run(bodies, items, locations, value_index=None, redirects=None, include_historical=False, **kw):
    gp = make_pool(items, locations, **kw)
    item_values = {it.qid: [s.value for s in it.values("P227")] for it in items if it.values("P227")}
    return classify_gnd(bodies, gp, value_index or {}, item_values, redirects, include_historical)


def test_body_names_paren_place():
    exact, generic = body_names(body("1", "Stadtarchiv (Oberursel (Taunus))",
                                     places=[{"gnd": "x", "name": "Oberursel (Taunus)"}]))
    assert "stadtarchiv oberursel taunus" in exact and "stadtarchiv oberursel" in exact
    assert generic == {"stadtarchiv"}


def test_vorhanden_and_alt():
    b1, b2 = body("10623-9", "Stadtarchiv Mannheim"), body("10938-1", "Stadtarchiv Augsburg", old_ids=["2009424-3"])
    ms = run([b1, b2], [], {}, value_index={"10623-9": {"Q1"}, "2009424-3": {"Q2"}})
    assert [m.status for m in ms] == [VORHANDEN, VORHANDEN_ALT]
    assert all(m.in_wikidata for m in ms) and "2009424-3" in ms[1].note


def test_stark_via_place_qid_and_generic_name():
    b = body("10623-9", "Stadtarchiv (Mannheim)")
    (m,) = run([b], [item("Q5", "Stadtarchiv")], {"Q5": [("Q2119", "Mannheim")]})
    assert m.status == STARK and m.qids == ["Q5"] and "Ort-QID Q2119" in m.note


def test_stark_via_coordinates():
    b = body("10623-9", "Stadtarchiv Mannheim")
    (m,) = run([b], [item("Q5", "Stadtarchiv Mannheim")], {}, coords={"Q5": [(49.49, 8.47)]})
    assert m.status == STARK and "km vom Ort" in m.note


def test_generic_name_without_place_is_no_candidate():
    b = body("10623-9", "Stadtarchiv (Mannheim)")
    (m,) = run([b], [item("Q5", "Stadtarchiv")], {"Q5": [("Q2749", "Augsburg")]})
    assert m.status == KEIN_TREFFER


def test_schwach_same_name_other_place():
    b = body("10623-9", "Stadtarchiv Mannheim")
    (m,) = run([b], [item("Q5", "Stadtarchiv Mannheim")], {"Q5": [("Q2749", "Augsburg")]})
    assert m.status == SCHWACH and "Ort abweichend" in m.note


def test_schwach_similar_name_same_place():
    b = body("1067541-1", "Reiss-Engelhorn-Museen", types=["museum"])
    (m,) = run([b], [item("Q5", "Reiß-Engelhorn-Museum"), item("Q6", "Kunsthalle Mannheim")],
               {"Q5": [("Q2119", "Mannheim")], "Q6": [("Q2119", "Mannheim")]})
    assert m.status == SCHWACH and m.qids == ["Q5"] and "Ort gleich" in m.note


def test_parent_item_is_no_candidate():
    b = body("10004722-1", "Staatsbibliothek zu Berlin. Ostasienabteilung", types=["library"],
             places=[{"gnd": "4005728-8", "name": "Berlin", "qid": "Q64"}],
             superior=[{"gnd": "5036103-X", "name": "Staatsbibliothek zu Berlin"}])
    (m,) = run([b], [item("Q170109", "Staatsbibliothek zu Berlin", p227=["5036103-X"])], {"Q170109": [("Q64", "Berlin")]})
    assert m.status == KEIN_TREFFER


def test_konflikt_other_p227():
    b = body("10623-9", "Stadtarchiv Mannheim")
    (m,) = run([b], [item("Q5", "Stadtarchiv Mannheim", p227=["2009424-3"])], {"Q5": [("Q2119", "Mannheim")]})
    assert m.status == KONFLIKT and "2009424-3" in m.note


def test_mehrdeutig_two_items_and_one_item_two_bodies():
    b = body("10623-9", "Stadtarchiv Mannheim")
    (m,) = run([b], [item("Q5", "Stadtarchiv Mannheim"), item("Q6", "Stadtarchiv Mannheim")],
               {"Q5": [("Q2119", "")], "Q6": [("Q2119", "")]})
    assert m.status == MEHRDEUTIG and m.qids == ["Q5", "Q6"]
    b2 = body("10624-7", "Stadtarchiv Mannheim", types=["library"])
    ms = run([b, b2], [item("Q5", "Stadtarchiv Mannheim")], {"Q5": [("Q2119", "")]})
    assert [m.status for m in ms] == [MEHRDEUTIG, MEHRDEUTIG]


def test_mehrdeutig_value_on_two_items():
    (m,) = run([body("10623-9", "X")], [], {}, value_index={"10623-9": {"Q1", "Q2"}})
    assert m.status == MEHRDEUTIG and m.in_wikidata


def test_p227_format_findings():
    items = {"Q1": item("Q1", "a", ["4040795-1"]), "Q2": item("Q2", "b", ["https://d-nb.info/gnd/4040795-0"]),
             "Q3": item("Q3", "c", ["4040795-0"])}
    got = p227_format_findings(items)
    assert [(q, p) for q, _, p in got] == [("Q1", "Prüfziffer falsch"), ("Q2", "Formatfehler, korrekt: 4040795-0")]


# --- Mix'n'match -----------------------------------------------------------------------
def test_mnm_rows_and_description(tmp_path):
    b1 = body("10623-9", "Stadtarchiv Mannheim", broader=["Stadtarchiv"], established="1907",
              superior=[{"gnd": "1", "name": "Stadt Mannheim"}])
    b2 = body("9819-X", "Museum \"Alte Post\"", types=["library", "museum"], country="DDR", places=(AUGSBURG,),
              terminated="1990")
    b3 = body("10938-1", "Stadtarchiv Augsburg")
    ms = [GndMatch(b1, STARK, ["Q5"]), GndMatch(b2, KEIN_TREFFER), GndMatch(b3, VORHANDEN, ["Q7"], in_wikidata=True)]
    rows = mnm_rows(ms)
    assert set(rows) == {("archive", "DE"), ("library", "DDR"), ("museum", "DDR")}
    assert rows[("archive", "DE")] == [{"id": "10623-9", "name": "Stadtarchiv Mannheim", "type": "Q166118",
                                        "description": "Mannheim; Stadtarchiv; gegr. 1907; Teil von: Stadt Mannheim",
                                        "url": "https://d-nb.info/gnd/10623-9"}]
    assert rows[("museum", "DDR")][0]["description"] == "Augsburg; bis 1990; DDR"
    assert mnm_description(body("1", "x", places=(), country="DR")) == "Deutsches Reich"
    assert len(mnm_description(body("1", "x", broader=["y" * 400]))) == 255

    paths = write_mnm(rows, "2026-11-21", out_dir=tmp_path, lands=["DE", "DDR"])
    assert sorted(p.name for p, _ in paths) == ["2026-11-21_P227_mnm_gnd_archive_DE.csv", "2026-11-21_P227_mnm_gnd_library_DDR.csv",
                                               "2026-11-21_P227_mnm_gnd_museum_DDR.csv"]
    text = (tmp_path / "2026-11-21_P227_mnm_gnd_archive_DE.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "id,name,description,url,type"
    with (tmp_path / "2026-11-21_P227_mnm_gnd_museum_DDR.csv").open(encoding="utf-8") as fh:
        rows_back = list(csv.DictReader(fh))
    assert rows_back[0]["name"] == 'Museum "Alte Post"' and list(rows_back[0]) == ["id", "name", "description", "url", "type"]


def test_gnd_match_roundtrip():
    m = GndMatch(body("10623-9", "X"), SCHWACH, ["Q5"], "n", 0.9)
    assert GndMatch.from_dict(json.loads(json.dumps(m.to_dict()))) == m


# --- Deutsche Labels, Beschreibungen, Aliase ---------------------------------------------
def test_gndbody_nfc():
    b = GndBody(gnd="1", name="Archa\u0308ologisches Museum", variants=["Mu\u0308nster"],
                places=[{"name": "Mu\u0308nster"}])
    assert b.name == "Archäologisches Museum" and b.variants == ["Münster"] and b.place_names == ["Münster"]


def test_de_label_place_bracket():
    assert de_label(body("1", "Stadtarchiv (Mannheim)")) == "Stadtarchiv Mannheim"
    assert de_label(body("1", "Museum (1907-1990)")) == "Museum (1907-1990)"  # keine Orts-Klammer
    assert de_label(body("1", "Forum in Mannheim (Mannheim)")) == "Forum in Mannheim"
    oberursel = {"name": "Oberursel (Taunus)"}
    assert de_label(body("1", "Stadtarchiv (Oberursel (Taunus))", places=(oberursel,))) == "Stadtarchiv Oberursel (Taunus)"


@pytest.mark.parametrize("broader, types, former, expected", [
    (["Heimatmuseum"], ["museum"], False, "Heimatmuseum in Mannheim"),
    (["Wissenschaftliche Bibliothek"], ["library"], False, "wissenschaftliche Bibliothek in Mannheim"),
    (["Archäologisches Museum"], ["museum"], False, "archäologisches Museum in Mannheim"),
    (["Verein", "Stadtarchiv"], ["archive"], False, "Stadtarchiv in Mannheim"),
    ([], ["library"], False, "Bibliothek in Mannheim"),
    (["Historisches Museum"], ["museum"], True, "ehemaliges historisches Museum in Mannheim"),
    (["Öffentliche Bibliothek"], ["library"], True, "ehemalige öffentliche Bibliothek in Mannheim"),
])
def test_de_description(broader, types, former, expected):
    assert de_description(body("1", "X", broader=broader, types=types), former=former) == expected


def test_de_description_needs_place():
    assert de_description(body("1", "X", places=(), broader=["Museum"])) == ""


def test_de_aliases_filters():
    b = body("1", "Stadtarchiv (Mannheim)", variants=[
        "Archiv der Stadt Mannheim", "Stadt Mannheim. Stadtarchiv", "Stadtarchiv (1907-)", "SA", "StA", "Marchivum", "Stadtarchiv-Mannheim",
        "Mannheim City Archives", 'Archiv "Alt"', "Firma AG. Archiv", "Verein e.V.. Archiv", "St. Anna Archiv"])
    assert de_aliases(b) == ["Stadtarchiv Mannheim", "Archiv der Stadt Mannheim", "Stadtarchiv-Mannheim", "St. Anna Archiv"]


def _terms(gnd, **kw):
    t = {"gnd": set(gnd), "human": False, "glam": True, "lde": "", "lmul": "", "dde": "", "aliases": set()}
    t.update(kw)
    return t


def test_term_proposals():
    bodies = {b.gnd: b for b in [
        body("1", "Stadtarchiv (Mannheim)", broader=["Stadtarchiv"], variants=["Archiv der Stadt Mannheim"]),
        body("2", "Heimatmuseum (Augsburg)", places=(AUGSBURG,), broader=["Heimatmuseum"], types=["museum"],
             variants=["Augsburger Heimatmuseum"]),
        body("3", "Museum X"), body("4", "Museum Y"), body("5", "Bibliothek Z")]}
    terms = {
        "Q1": _terms({"1"}, lde="", lmul="", aliases={"archiv der stadt mannheim"}),  # Alias schon da
        "Q2": _terms({"2"}, lde="Heimatmuseum Augsburg", dde="Museum"),  # Beschreibung bleibt
        "Q3": _terms({"3"}, human=True),
        "Q4": _terms({"4"}, glam=False),
        "Q5": _terms({"5"}), "Q6": _terms({"5"}),  # GND an zwei Items
        "Q7": _terms({"99"}),  # GND nicht in der Tabelle
    }
    by = {p.qid: p for p in term_proposals(terms, bodies)}
    assert by["Q1"].label == "Stadtarchiv Mannheim" and by["Q1"].description == "Stadtarchiv in Mannheim"
    assert by["Q1"].aliases == []
    assert by["Q2"].label == "" and by["Q2"].description == "" and by["Q2"].aliases == ["Augsburger Heimatmuseum"]
    assert not by["Q3"].has_changes and "Mensch" in by["Q3"].note
    assert not by["Q4"].has_changes and "P31" in by["Q4"].note
    assert not by["Q5"].has_changes and "mehreren Items" in by["Q5"].note
    assert "Q7" not in by
    assert term_commands([by["Q1"]], "Lde") == [['Q1\tLde\t"Stadtarchiv Mannheim"']]
    assert term_commands([by["Q2"]], "Ade") == [['Q2\tAde\t"Augsburger Heimatmuseum"']]


def test_term_proposals_mul_label_and_unique_pair():
    bodies = {b.gnd: b for b in [body("1", "Stadtarchiv (Mannheim)", broader=["Stadtarchiv"]),
                                 body("2", "Stadtarchiv Mannheim", broader=["Stadtarchiv"])]}
    terms = {"Q1": _terms({"1"}, lmul="Stadtarchiv Mannheim"), "Q2": _terms({"2"}, lde="Stadtarchiv Mannheim")}
    by = {p.qid: p for p in term_proposals(terms, bodies)}
    assert by["Q1"].label == ""  # mul-Label zählt als vorhanden
    assert by["Q1"].description == by["Q2"].description == ""  # gleiches Paar → keine Beschreibung
    assert "nicht eindeutig" in by["Q1"].note


# --- Zeitraum ------------------------------------------------------------------------------
@pytest.mark.parametrize("values, year", [
    (["1606"], 1606), (["15.09.1883"], 1883), (["XX.05.1952"], 1952), (["XX.XX.1902"], 1902), (["529"], 529),
    (["19XX"], None), (["ca. 1906"], None), (["13. Jh."], None), (["55"], None), (["1619", "1962"], None), ([], None),
])
def test_exact_year(values, year):
    assert exact_year(values) == year


@pytest.mark.parametrize("est, term, comb, start, end, closed, raw", [
    (["1606"], ["1749"], [], 1606, 1749, True, "1606–1749"),  # Gottorfer Hofbibliothek
    (["1894"], [], [], 1894, None, False, "1894–"),
    ([], ["192X"], [], None, None, True, "–192X"),
    ([], [], ["ca. 1972-2002"], None, None, True, "ca. 1972-2002"),
    ([], [], ["ca. 1550-"], None, None, False, "ca. 1550-"),
    ([], [], ["ca. 1820 - vor 1897"], None, None, True, "ca. 1820 - vor 1897"),
    ([], [], ["ca. Wintersemester 1969/1970 bis Wintersemester 1974/1975"], None, None, True,
     "ca. Wintersemester 1969/1970 bis Wintersemester 1974/1975"),
    ([], [], ["1975"], None, None, False, "1975"),  # Beginn oder Ende? unscharf
])
def test_parse_period(est, term, comb, start, end, closed, raw):
    assert parse_period(est, term, comb) == {"start_year": start, "end_year": end, "period_raw": raw, "closed": closed}


def test_build_table_period_and_successor(tmp_path):
    subj = [{"@id": "https://d-nb.info/gnd/4006439-6", GNDO + "preferredNameForTheSubjectHeading": [{"@value": "Bibliothek"}]},
            {"@id": "https://d-nb.info/gnd/4040795-0", GNDO + "preferredNameForTheSubjectHeading": [{"@value": "Museum"}]},
            {"@id": "https://d-nb.info/gnd/4002859-8", GNDO + "preferredNameForTheSubjectHeading": [{"@value": "Archiv"}]}]
    lib = ["4006439-6"]
    pre = GNDO + "precedingCorporateBody"
    bodies = [
        _node("7659780-5", "Gottorfer Hofbibliothek", broader=lib,
              **{GNDO + "dateOfEstablishment": [{"@value": "1606"}], GNDO + "dateOfTermination": [{"@value": "1749"}]}),
        _node("100-1", "Stadtbücherei Alt", broader=lib, **{GNDO + "dateOfTermination": [{"@value": "1990"}]}),
        # Nachfolger ist kein GLAM-Satz, zählt trotzdem als Nachfolger
        _node("200-2", "Kulturverein Neu", **{pre: [{"@id": "https://d-nb.info/gnd/100-1"}],
                                             GNDO + "dateOfEstablishment": [{"@value": "1990"}]}),
        _node("300-3", "Stadtbibliothek Neu", broader=lib, **{pre: [{"@id": "https://d-nb.info/gnd/100-1"}],
                                                             GNDO + "dateOfEstablishment": [{"@value": "1990"}]}),
    ]
    corp = _dump(tmp_path / "authorities-gnd-koerperschaft_lds_20261013.jsonld.gz", bodies)
    _dump(tmp_path / "authorities-gnd-sachbegriff_lds_20261013.jsonld.gz", subj)
    _dump(tmp_path / "authorities-gnd-geografikum_lds_20261013.jsonld.gz", [])
    stats = build_table(corp, tmp_path / "t.sqlite")
    by = {b.gnd: b for b in load_table(tmp_path / "t.sqlite")}
    assert set(by) == {"7659780-5", "100-1", "300-3"}
    g = by["7659780-5"]
    assert (g.start_year, g.end_year, g.period_raw, g.historical, g.period_status) == (1606, 1749, "1606–1749", True, "historisch")
    assert by["100-1"].successors == ["200-2", "300-3"] and by["100-1"].period_status == "vorgaenger"
    assert by["300-3"].predecessors == ["100-1"] and by["300-3"].period_status == ""
    assert stats["abgeschlossen"] == 2 and stats["historisch"] == 1


def test_classify_historical_and_mnm():
    hist = body("7659780-5", "Gottorfer Hofbibliothek", types=["library"], closed=True, historical=True,
                start_year=1606, end_year=1749, period_raw="1606–1749", established="1606", terminated="1749")
    old = body("100-1", "Stadtarchiv (Mannheim)", closed=True, successors=["300-3"], period_raw="–1990")
    items = [item("Q1", "Gottorfer Hofbibliothek"), item("Q2", "Stadtarchiv")]
    locs = {"Q1": [("Q2119", "Mannheim")], "Q2": [("Q2119", "Mannheim")]}
    ms = run([hist, old], items, locs)
    assert [m.status for m in ms] == [HISTORISCH, VORGAENGER] and all(not m.qids for m in ms)
    assert mnm_rows(ms) == {}
    rows = mnm_rows(ms, include_historical=True)
    descs = {r["id"]: r["description"] for rs in rows.values() for r in rs}
    assert "1606–1749" in descs["7659780-5"] and "aufgelöst" in descs["7659780-5"]
    assert "früherer Name" in descs["100-1"]
    ms = run([hist, old], items, locs, include_historical=True)
    assert [m.status for m in ms] == [STARK, STARK]


def test_classify_redirected_body_never_candidate():
    b = body("10623-9", "Stadtarchiv (Mannheim)")
    reds = {"10623-9": rd.Redirect("10623-9", "999-9", rd.REDIRECTED)}
    (m,) = run([b], [item("Q5", "Stadtarchiv")], {"Q5": [("Q2119", "Mannheim")]}, redirects=reds)
    assert m.status == GND_UMGELENKT and not m.qids and "999-9" in m.note
    assert mnm_rows([m], include_historical=True) == {}
    reds = {"10623-9": rd.Redirect("10623-9", None, rd.DELETED_KIND)}
    (m,) = run([b], [], {}, redirects=reds)
    assert m.status == GND_GELOESCHT


@pytest.mark.parametrize("kw, p571, p576, expected", [
    (dict(closed=True, historical=True, start_year=1606, end_year=1749), set(), set(), "historisch_in_wikidata"),
    (dict(closed=True, historical=True, start_year=1606, end_year=1749), {1606}, {1749}, ""),
    (dict(closed=True, historical=True, start_year=1606, end_year=1749), {1606}, {1750}, "zeitraum_abweichend"),
    (dict(start_year=2001), {1779}, set(), "zeitraum_abweichend"),
    (dict(start_year=2001, predecessors=["1-1"]), {1779}, set(), ""),  # Umbenennungsjahr: kein Vergleich
    (dict(closed=True, successors=["2-2"], end_year=1990), set(), {1990}, "vorgaenger_gnd"),
    (dict(), set(), {1945}, "p576_nur_in_wikidata"),
])
def test_period_annotation(kw, p571, p576, expected):
    a = period_annotation(body("1", "X", **kw), p571, p576)
    assert a["anmerkung"] == expected
    assert set(a) == {"gnd_zeitraum", "gnd_historisch", "wd_p571", "wd_p576", "anmerkung"}


def test_qualifier_proposals():
    N, D = "http://wikiba.se/ontology#NormalRank", "http://wikiba.se/ontology#DeprecatedRank"
    def st(rank=N, s=False, e=False):
        return {"rank": rank, "has_p580": s, "has_p582": e}
    hist = body("7659780-5", "Gottorfer Hofbibliothek", closed=True, historical=True, start_year=1606, end_year=1749,
                period_raw="1606–1749")
    renamed = body("2", "Alte Namensform", closed=True, successors=["3"], start_year=1950, end_year=1990,
                   predecessors=["1"], period_raw="1950–1990")  # Ende der Namensform: gehört an den Identifier
    has_start = body("4", "Hat Start", start_year=1900, end_year=1950, closed=True, period_raw="1900–1950")
    reopened = body("5", "Schlossmuseum", closed=True, end_year=1944, period_raw="1924 / 1965–1944")
    vague = body("6", "Unscharf", period_raw="19XX–")
    two = body("7", "Zwei Aussagen", start_year=1900, period_raw="1900–")
    dep = body("8", "Nur deprecated", start_year=1900, period_raw="1900–")
    cand = body("9", "Kandidat", start_year=1900, period_raw="1900–")
    ms = [GndMatch(b, VORHANDEN, [f"Q{b.gnd[0]}"]) for b in (hist, renamed, has_start, reopened, vague, two, dep)]
    ms.append(GndMatch(cand, STARK, ["Q9"]))
    sts = {("Q7", "7659780-5"): [st()], ("Q2", "2"): [st()], ("Q4", "4"): [st(s=True)], ("Q5", "5"): [st()],
           ("Q6", "6"): [st()], ("Q7", "7"): [st(), st()], ("Q8", "8"): [st(D)], ("Q9", "9"): [st()]}
    props = qualifier_proposals(ms, sts, 2026)
    assert [(p.qid, p.gnd, p.start, p.end) for p in props] == [
        ("Q7", "7659780-5", 1606, 1749), ("Q2", "2", 1950, 1990), ("Q4", "4", None, 1950)]
    assert qualifier_commands(props) == [
        ['Q7\tP227\t"7659780-5"\tP580\t+1606-00-00T00:00:00Z/9\tP582\t+1749-00-00T00:00:00Z/9'],
        ['Q2\tP227\t"2"\tP580\t+1950-00-00T00:00:00Z/9\tP582\t+1990-00-00T00:00:00Z/9'],
        ['Q4\tP227\t"4"\tP582\t+1950-00-00T00:00:00Z/9']]
    assert qualifier_proposals(ms, {k: [st(s=True, e=True)] for k in sts}, 2026) == []  # alles schon da


def _gz(path, text):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_iter_redirects_and_resolve(tmp_path):
    G = "https://d-nb.info/gnd/"
    def red(o, n):
        return ('{"@id":"%s%s","http://www.w3.org/2002/07/owl#sameAs":[{"@id":"%s%s"}],'
                '"https://d-nb.info/standards/elementset/dnb#canonicalUri":[{"@type":"x","@value":"%s%s"}]}' % (G, o, G, n, G, n))
    deleted = '{"@id":"%s273-2","@type":["https://d-nb.info/standards/elementset/dnb#DeletedResource"]}' % G
    text = "[" + red("1-1", "2-2") + ",\n" + deleted + "," + red("2-2", "3-3") + ",\n" + red("4-4", "273-2") + \
        ",\n" + red("5-5", "6-6") + ",\n" + '{"@id":"kaputt", "x": [' + "\n" + red("7-7", "8-8") + "]"
    stats = Counter()
    reds = {r.old_id: r for r in rd.iter_redirects(_gz(tmp_path / "u_20261113.jsonld.gz", text), stats)}
    assert stats["umgelenkt"] == 5 and stats["geloescht"] == 1 and stats["defekt_objekt"] == 1
    assert reds["7-7"].new_id == "8-8"  # nach dem defekten Objekt geht es weiter
    assert reds["273-2"].kind == rd.DELETED_KIND and reds["273-2"].new_id is None
    assert rd.resolve(reds, "1-1") == ("3-3", rd.REDIRECTED)  # Kette 1 → 2 → 3
    assert rd.resolve(reds, "4-4") == (None, rd.DELETED_KIND)  # Kette endet in Löschung
    assert rd.resolve(reds, "9-9") == ("9-9", "")
    assert rd.file_date(tmp_path / "authorities-gnd_umlenk_loesch_20261113.jsonld.gz") == "2026-11-13"


def test_redirect_table_roundtrip(tmp_path):
    p = _gz(tmp_path / "authorities-gnd_umlenk_loesch_20261113.jsonld.gz",
            '[{"@id":"https://d-nb.info/gnd/1-1","http://www.w3.org/2002/07/owl#sameAs":[{"@id":"https://d-nb.info/gnd/2-2"}]}]')
    db = tmp_path / "t.sqlite"
    assert rd.ensure(p, db)["umgelenkt"] == 1
    assert rd.load(db) == {"1-1": rd.Redirect("1-1", "2-2", rd.REDIRECTED)}
    assert rd.ensure(p, db)["umgelenkt"] == 1  # unverändert: aus meta


def test_p227_redirect_findings():
    reds = {"1-1": rd.Redirect("1-1", "2-2", rd.REDIRECTED), "2-2": rd.Redirect("2-2", "3-3", rd.REDIRECTED),
            "5-5": rd.Redirect("5-5", "6-6", rd.REDIRECTED), "7-7": rd.Redirect("7-7", None, rd.DELETED_KIND),
            "8-8": rd.Redirect("8-8", "9-9", rd.REDIRECTED), "10-0": rd.Redirect("10-0", "11-1", rd.REDIRECTED)}
    N, D = "http://wikiba.se/ontology#NormalRank", "http://wikiba.se/ontology#DeprecatedRank"
    sts = [
        {"qid": "Q1", "value": "1-1", "rank": N, "refs": "0", "quals": "0"},  # Kette → Korrektur, Ziel ergänzen
        {"qid": "Q5", "value": "5-5", "rank": N, "refs": "1", "quals": "0"},  # Quelle → nur Bericht
        {"qid": "Q6", "value": "5-5", "rank": D, "refs": "0", "quals": "1"},  # schon deprecated → übergangen
        {"qid": "Q7", "value": "7-7", "rank": N, "refs": "0", "quals": "0"},  # gelöscht → Bericht
        {"qid": "Q8", "value": "8-8", "rank": N, "refs": "0", "quals": "0"},  # Ziel an anderem Item → Bericht
        {"qid": "Q10", "value": "10-0", "rank": N, "refs": "0", "quals": "0"},  # Ziel schon da → nur entfernen
        {"qid": "Q11", "value": "12-2", "rank": N, "refs": "0", "quals": "0"},  # nicht umgelenkt
    ]
    owners = {"3-3": [], "9-9": ["Q99"], "11-1": ["Q10"]}
    fs = {f.qid: f for f in p227_redirect_findings(sts, reds, owners)}
    assert set(fs) == {"Q1", "Q5", "Q7", "Q8", "Q10"}
    assert (fs["Q1"].status, fs["Q1"].new, fs["Q1"].action) == ("p227_umgelenkt", "3-3", "korrektur")
    assert fs["Q5"].action == "bericht" and "Quellen" in fs["Q5"].note
    assert fs["Q7"].status == "p227_geloescht" and fs["Q7"].action == "bericht"
    assert fs["Q8"].action == "bericht" and "Q99" in fs["Q8"].note
    assert fs["Q10"].action == "korrektur" and fs["Q10"].new_present
    cmds = redirect_commands(list(fs.values()), "2026-11-22")
    assert cmds == [['-Q1\tP227\t"1-1"', 'Q1\tP227\t"3-3"\tS248\tQ36578\tS813\t+2026-11-22T00:00:00Z/11'],
                    ['-Q10\tP227\t"10-0"']]


def test_write_gnd_report_period_columns(tmp_path):
    from glamsync.export import write_gnd_report

    hist = body("7659780-5", "Gottorfer Hofbibliothek", closed=True, historical=True, period_raw="1606–1749")
    new = body("2", "Neu", period_raw="2001–")
    ms = [GndMatch(hist, VORHANDEN, ["Q1"]), GndMatch(new, KEIN_TREFFER)]
    ann = {"7659780-5": period_annotation(hist, {1606}, set())}
    write_gnd_report(ms, {}, tmp_path / "r.csv", ann)
    rows = list(csv.DictReader((tmp_path / "r.csv").open(encoding="utf-8"), delimiter=";"))
    assert rows[0]["gnd_zeitraum"] == "1606–1749" and rows[0]["anmerkung"] == "historisch_in_wikidata"
    assert rows[0]["wd_p571"] == "1606" and rows[0]["gnd_historisch"] == "historisch"
    assert rows[1]["gnd_zeitraum"] == "2001–" and rows[1]["anmerkung"] == ""


def test_write_registry_report_unchanged(tmp_path):
    from types import SimpleNamespace

    from glamsync.export import write_registry_report

    rec = SimpleNamespace(value="DE-1", name="Archiv", city="Mannheim", websites=[], url="u")
    write_registry_report([SimpleNamespace(record=rec, status="kein_treffer", qids=[], score=None, note="")],
                          {}, tmp_path / "r.csv")
    assert (tmp_path / "r.csv").read_text(encoding="utf-8").splitlines()[0].startswith("status;wert;name")


def test_label_similarity_and_mark_sure():
    from glamsync.gnd import SICHER, STARK, label_similarity, mark_sure, p227_commands

    # Ortsname darf fehlen, Klammer-Ort zählt; ähnliche Nachbarorte reichen nicht
    assert label_similarity(body("1", "Volkskundemuseum Mannheim"), item("Q1", "Volkskundemuseum")) == 1.0
    assert label_similarity(body("1", "Heimatmuseum (Mannheim)"), item("Q1", "Heimatmuseum Mannheim")) == 1.0
    assert label_similarity(body("1", "Stadtarchiv Korneuburg"), item("Q1", "Stadtarchiv Klosterneuburg")) < 0.95

    loc = {"Q1": [("Q2119", "Mannheim")], "Q2": [("Q2119", "Mannheim")]}
    items = [item("Q1", "Stadtarchiv Mannheim"), item("Q2", "Kammerhof")]
    gp = make_pool(items, loc, aliases={"Q2": ["Museum im Kammerhof"]})
    ms = classify_gnd([body("1", "Stadtarchiv Mannheim"), body("2", "Museum im Kammerhof")], gp, {}, {})
    assert [m.status for m in ms] == [STARK, STARK]
    assert mark_sure(ms, gp, {}) == 1  # Treffer nur über Alias (Gebäude-Item) bleibt Handprüfung
    assert [m.status for m in ms] == [SICHER, STARK]
    assert p227_commands(ms, "2026-11-24") == [['Q1\tP227\t"1"\tS248\tQ36578\tS813\t+2026-11-24T00:00:00Z/11']]
