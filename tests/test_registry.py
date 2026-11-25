"""Tests für den Registerabgleich (Name + Website) ohne Netz."""

import pytest

from glamsync.export import write_registry_qs
from glamsync.models import WikidataItem
from glamsync.registry import (
    KEIN_TREFFER,
    KONFLIKT,
    MEHRDEUTIG,
    SCHWACH,
    STARK,
    VORHANDEN,
    VORHANDEN_PRUEFEN,
    Extras,
    Pool,
    classify_registry,
    norm_name,
    norm_url,
    wikidata_not_in_registry,
)
from glamsync.sources.registry import RegistryRecord, _isil_record


@pytest.mark.parametrize("raw,expected", [
    ("Weltmuseum Wien", "weltmuseum wien"),
    ("Kunst- & Gewerbemuseum", "kunst und gewerbemuseum"),
    ("Straßenbahnmuseum", "strassenbahnmuseum"),
    ("  MAK – Museum  ", "mak museum"),
])
def test_norm_name(raw, expected):
    assert norm_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("https://www.weltmuseumwien.at/", "weltmuseumwien.at"),
    ("www.weltmuseumwien.at", "weltmuseumwien.at"),
    ("http://WWW.Example.org:80/museum/index.html#top", "example.org/museum"),
    ("https://example.org/zusatzseite.aspx?id=5&utm_source=x", "example.org/zusatzseite.aspx?id=5"),
    ("https://example.org/zusatzseite.aspx?b=2&a=1", "example.org/zusatzseite.aspx?a=1&b=2"),
    ("https://example.org/de/", "example.org/de"),
    ("", ""),
])
def test_norm_url(raw, expected):
    assert norm_url(raw) == expected


def rec(value, name, web=None, lat=None, lon=None):
    return RegistryRecord("mure", "P7917", value, name, websites=[web] if web else [], lat=lat, lon=lon,
                          url=f"https://museen-in-oesterreich.at/museum/{value.lower()}/")


def pool(*specs):
    items, extras = {}, {}
    for qid, label, web, coord in specs:
        items[qid] = WikidataItem(qid, labels={"de": label})
        extras[qid] = Extras(websites=[web] if web else [], coords=[coord] if coord else [])
    return Pool(items, extras)


P = pool(
    ("Q1", "Weltmuseum Wien", "https://www.weltmuseumwien.at/", (48.2055, 16.3647)),
    ("Q2", "Heimatmuseum Musterdorf", "https://musterdorf.at/museum", (47.0, 15.0)),
    ("Q3", "Stadtmuseum Beispielstadt", None, (47.5, 14.5)),
)


def test_strong_candidate_needs_name_and_website():
    m = classify_registry([rec("M10072", "Weltmuseum Wien", "www.weltmuseumwien.at")], P, {}, {})[0]
    assert m.status == STARK and m.qids == ["Q1"]


def test_name_only_is_weak():
    m = classify_registry([rec("M1", "Stadtmuseum Beispielstadt", "www.anders.at")], P, {}, {})[0]
    assert m.status == SCHWACH and m.qids == ["Q3"]


def test_website_and_similar_name_is_weak():
    m = classify_registry([rec("M2", "Heimatmuseum Muster-Dorf e.V.", "musterdorf.at/museum/")], P, {}, {})[0]
    assert m.status == SCHWACH and "Q2" in m.qids


def test_near_and_similar_name_is_weak():
    m = classify_registry([rec("M3", "Weltmuseum Wien (Neue Burg)", None, 48.2056, 16.3648)], P, {}, {})[0]
    assert m.status == SCHWACH and m.qids[0] == "Q1"


def test_no_hit():
    assert classify_registry([rec("M4", "Völlig anderes Museum")], P, {}, {})[0].status == KEIN_TREFFER


def test_existing_value():
    idx = {"M10072": {"Q1"}}
    assert classify_registry([rec("M10072", "Weltmuseum Wien")], P, idx, {})[0].status == VORHANDEN
    assert classify_registry([rec("M10072", "Ganz anderer Name XYZ")], P, idx, {})[0].status == VORHANDEN_PRUEFEN


def test_existing_value_on_two_items():
    m = classify_registry([rec("M10072", "Weltmuseum Wien")], P, {"M10072": {"Q1", "Q9"}}, {})[0]
    assert m.status == MEHRDEUTIG


def test_strong_but_item_has_other_value():
    m = classify_registry([rec("M10072", "Weltmuseum Wien", "weltmuseumwien.at")], P, {}, {"Q1": ["M99999"]})[0]
    assert m.status == KONFLIKT


def test_one_item_strong_for_two_records():
    rs = [rec("M1", "Weltmuseum Wien", "weltmuseumwien.at"), rec("M2", "Weltmuseum Wien", "weltmuseumwien.at")]
    assert {m.status for m in classify_registry(rs, P, {}, {})} == {MEHRDEUTIG}


def test_isil_case_insensitive_existing():
    r = RegistryRecord("isil", "P791", "DE-Ks18", "Bibliothek X")
    m = classify_registry([r], P, {"DE-KS18": {"Q5"}}, {})[0]
    assert m.status == VORHANDEN


def test_registry_cleanup():
    from glamsync.registry import registry_cleanup
    sts = [
        {"qid": "Q1", "value": "DE-1"},                                   # im Register
        {"qid": "Q2", "value": "DE-999"},                                 # fehlt im Register
        {"qid": "Q3", "value": "DE-MUS"},                                 # ungültig
        {"qid": "Q4", "value": "DE-MUS", "has_refs": True},               # ungültig, mit Quelle
        {"qid": "Q5", "value": "AT-X"},                                   # anderes Präfix
    ]
    cs = {(c.qid, c.kind, c.action) for c in registry_cleanup(sts, {"DE-1"}, "P791", "DE-")}
    assert cs == {("Q2", "nicht_im_register", "delete"), ("Q3", "ungueltig", "delete"), ("Q4", "ungueltig", None)}


def test_registry_delete_qs(tmp_path):
    from glamsync.export import write_registry_delete_qs
    from glamsync.registry import Cleanup
    cs = [Cleanup("Q3", "DE-MUS", "ungueltig", "delete"), Cleanup("Q2", "DE-999", "nicht_im_register", "delete"),
          Cleanup("Q4", "DE-MUS", "ungueltig", None)]
    paths = write_registry_delete_qs(cs, "P791", "Q183", "2026-11-16", tmp_path)
    assert {p.name: p.read_text() for p in paths} == {
        "qs_P791_loeschen_Q183_2026-11-16_part01.txt": '-Q3|P791|"DE-MUS"\n',
        "qs_P791_loeschen_pruefen_Q183_2026-11-16_part01.txt": '-Q2|P791|"DE-999"\n',
    }


def test_not_in_registry():
    rows = [("Q1", "DE-1"), ("Q2", "DE-999"), ("Q3", "AT-X")]
    assert wikidata_not_in_registry(rows, {"DE-1"}, "P791", "DE-") == [("Q2", "DE-999")]


def test_isil_record_parsing():
    m = {"identifier": "DE-Mb107", "name": "Hessisches Institut für Landesgeschichte", "location": "Marburg/Lahn",
         "data": {"009Q": [[{"u": "https://www.lagis-hessen.de"}, {"z": "A"}], [{"u": "https://opac"}, {"z": "B"}]],
                  "029@": [[{"a": "HLGL / LAGIS"}, {"4": "a"}]],
                  "032P": [[{"a": "Str. 6"}, {"d": "DE"}, {"k": "8.77969"}, {"l": "50.81008"}]]}}
    r = _isil_record(m)
    assert r.value == "DE-Mb107" and r.websites == ["https://www.lagis-hessen.de"]
    assert r.lat == 50.81008 and r.lon == 8.77969 and r.alt_names == ["HLGL / LAGIS"]


def test_registry_qs(tmp_path):
    m = classify_registry([rec("M10072", "Weltmuseum Wien", "weltmuseumwien.at")], P, {}, {})
    [path] = write_registry_qs(m, "P7917", "mure", "Q40", "2026-11-16", tmp_path)
    assert path.name == "qs_P7917_kandidaten_Q40_2026-11-16_part01.txt"
    assert path.read_text().strip() == (
        'Q1|P7917|"M10072"|S854|"https://museen-in-oesterreich.at/museum/m10072/"|S813|+2026-11-16T00:00:00Z/11')


def test_http_wait_budget(monkeypatch):
    import pytest
    from glamsync import http
    slept = []
    monkeypatch.setattr(http.time, "sleep", slept.append)
    assert http._wait(0, 30, "x") == 30 and slept == [30]
    with pytest.raises(http.SkippedRequest):
        http._wait(30, 16, "x")  # 46 s > MAX_WAIT (40)
    with pytest.raises(http.SkippedRequest):
        http._wait(0, 120, "x")  # Retry-After 120 s wird gar nicht erst abgewartet
    assert slept == [30]


def test_mark_sure_by_place_and_label():
    from glamsync.registry import KEIN_TREFFER, SICHER, Extras, Pool, RegistryMatch, mark_sure
    from glamsync.models import WikidataItem
    from glamsync.sources.registry import RegistryRecord

    items = {"Q1": WikidataItem("Q1", {"de": "Stadtbibliothek Biel", "fr": "Bibliothèque de la Ville de Bienne"}),
             "Q2": WikidataItem("Q2", {"de": "Stadtarchiv Biel"})}
    pool = Pool(items, {q: Extras() for q in items})
    locs = {"Q1": [("Q5", "Biel/Bienne"), ("Q5", "Bienne")], "Q2": [("Q5", "Bienne")]}
    rec = lambda v, name, alt=(): RegistryRecord("isil_ch", "P791", v, name, list(alt), city="Bienne")  # noqa: E731
    ms = [RegistryMatch(rec("CH-1", "Bibliothèque de la Ville de Bienne", ["Stadtbibliothek Biel"]), KEIN_TREFFER),
          RegistryMatch(rec("CH-2", "Musikschule Biel"), KEIN_TREFFER)]
    assert mark_sure(ms, pool, locs, {}, "P791") == 1
    assert (ms[0].status, ms[0].qids, ms[1].status) == (SICHER, ["Q1"], KEIN_TREFFER)
