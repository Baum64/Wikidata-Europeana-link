"""Städteabgleich Europeana-Orte ↔ Wikidata (P7704 place/base/<n>), ohne Netz."""

from glamsync.export import write_qs
from glamsync.match import build_lookups, classify_city
from glamsync.models import FEHLT, IDENTISCH, KEIN_TREFFER, KONFLIKT, MEHRDEUTIG, Statement, WikidataItem
from glamsync.sources.places import place_record, record_count

NO = build_lookups({})


def city(*p7704):
    return WikidataItem("Q2079", labels={"de": "Leipzig"},
                        claims={"P7704": [Statement(v, "Q2079$x", "normal", False, False) for v in p7704]})


def place(n, count=0):
    return {"id": f"http://data.europeana.eu/place/{n}", "prefLabel": {"de": "Leipzig"},
            "sameAs": ["http://www.wikidata.org/entity/Q2079"], "isAggregatedBy": {"recordCount": count}}


def test_place_record():
    r = place_record("1444", place(1444, 5))
    assert r.ids == {"P7704": ["place/base/1444"]} and r.qids == ["Q2079"] and r.label == "Leipzig"
    assert record_count(place(1, 5)) == 5 and record_count(None) == 0


def test_no_place():
    assert classify_city("Q2079", city(), [], NO).status == KEIN_TREFFER


def test_single_place_new():
    f = classify_city("Q2079", city(), [place_record("1444", place(1444))], NO)
    assert f.status == FEHLT and f.action == "new" and f.source_value == "place/base/1444"


def test_single_place_existing_and_conflict():
    r = [place_record("1444", place(1444))]
    assert classify_city("Q2079", city("place/base/1444"), r, NO).status == IDENTISCH
    assert classify_city("Q2079", city("place/base/9"), r, NO).status == KONFLIKT


def test_place_already_on_other_item():
    lk = build_lookups({"P7704": {"place/base/1444": ["Q99"]}})
    f = classify_city("Q2079", city(), [place_record("1444", place(1444))], lk)
    assert f.status == MEHRDEUTIG and f.action is None


def test_duplicates_tie_is_ambiguous():
    rs = [place_record("1442", place(1442)), place_record("1444", place(1444))]
    f = classify_city("Q2079", city(), rs, NO, {"1442": 0, "1444": 0})
    assert f.status == MEHRDEUTIG and f.action is None


def test_duplicates_pick_most_used():
    rs = [place_record("2312", place(2312)), place_record("2313", place(2313))]
    f = classify_city("Q2079", city(), rs, NO, {"2312": 0, "2313": 7572})
    assert f.action == "new" and f.source_value == "place/base/2313" and "7572 Objekte" in f.note


def test_duplicates_existing_value_wins():
    rs = [place_record("1442", place(1442)), place_record("1444", place(1444))]
    f = classify_city("Q2079", city("place/base/1442"), rs, NO, {"1444": 100})
    assert f.status == IDENTISCH


def test_city_qs_filename(tmp_path):
    f = classify_city("Q2079", city(), [place_record("1444", place(1444))], NO)
    [p] = write_qs([f], "P7704", "new", "2026-11-16", tmp_path, tag="staedte_Q183")
    assert p.name == "quickstatements_P7704_staedte_Q183_neu_2026-11-16_part01.qs"
    assert p.read_text() == 'Q2079|P7704|"place/base/1444"|S248|Q234110|S813|+2026-11-16T00:00:00Z/11\n'
