import pytest

from glamsync.normalize import (
    check,
    isni_display,
    normalize_gnd,
    normalize_isil,
    normalize_isni,
    normalize_p7704,
    normalize_viaf,
    p7704_problem,
    qid_from_uri,
)


# --- QID --------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://www.wikidata.org/entity/Q27302", "Q27302"),
        ("https://www.wikidata.org/entity/Q27302", "Q27302"),
        ("https://www.wikidata.org/wiki/Q27302", "Q27302"),
        ("http://wikidata.org/entity/Q1/", "Q1"),
        ("q42", "Q42"),
        ("http://www.wikidata.org/entity/P31", None),
        ("http://example.org/Q1", None),
        ("", None),
        (None, None),
    ],
)
def test_qid_from_uri(raw, expected):
    assert qid_from_uri(raw) == expected


# --- ISNI -------------------------------------------------------------------
# Wikidata-Format für P213 ist OHNE Leerzeichen (Constraint 0{7}\d{8}[\dX]).
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://isni.org/isni/0000000123531945", "0000000123531945"),
        ("https://isni.org/isni/000000012097381X", "000000012097381X"),
        ("http://isni.org/isni/000000012097381x", "000000012097381X"),
        ("0000000122905087", "0000000122905087"),
        ("0000 0001 2290 5087", "0000000122905087"),
        ("0000-0001-2290-5087", "0000000122905087"),
        ("0000000122905088", None),  # falsche Prüfziffer
        ("00000001229050", None),  # zu kurz
        (None, None),
    ],
)
def test_normalize_isni(raw, expected):
    assert normalize_isni(raw) == expected


def test_isni_display():
    assert isni_display("0000000123531945") == "0000 0001 2353 1945"


def test_isni_check_flags_spaces():
    c = check("P213", "0000 0001 2290 5087")
    assert not c.format_ok and c.normalized == "0000000122905087"
    assert check("P213", "0000000122905087").format_ok


# --- P7704 ------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("organisation/1234", "organisation/1234"),
        ("organization/1234", "organisation/1234"),
        ("http://data.europeana.eu/organization/4373", "organisation/4373"),
        ("https://data.europeana.eu/organisation/4373", "organisation/4373"),
        ("https://api.europeana.eu/entity/organization/4373.json", "organisation/4373"),
        ("agent/base/146741", "agent/base/146741"),
        ("http://data.europeana.eu/agent/base/146741", "agent/base/146741"),
        ("http://data.europeana.eu/agent/146741", "agent/base/146741"),
        ("place/base/216254", "place/base/216254"),
        ("concept/base/48", "concept/base/48"),
        ("https://www.europeana.eu/en/collections/organisation/5053-saints-archangels-church", "organisation/5053"),
        ("https://www.europeana.eu/en/collections/organisation/251-x?page=1&reusability=open", "organisation/251"),
        ("collections/person/73049", "agent/base/73049"),
        ("https://www.europeana.eu/de/collections/topic/48-photography", "concept/base/48"),
        ("https://www.europeana.eu/de/item/15508/DG1934_497", None),
        ("person/marco-di-meco", None),
        ("item/2048128/618580", None),
        ("de/item/2048128/618580", None),
        ("90402/SK_A_3262", None),
        ("1234", None),
        ("", None),
    ],
)
def test_normalize_p7704(raw, expected):
    assert normalize_p7704(raw) == expected


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ("organisation/1234", None),
        ("organization/1234", "amerikanische"),
        ("http://data.europeana.eu/organization/1", "URL"),
        ("item/2048128/618580", "P727"),
        ("90402/SK_A_3262", "P727"),
        ("1234", "ohne Präfix"),
        ("organisation/1482250000004502115", "veraltet"),
    ],
)
def test_p7704_problem(raw, fragment):
    problem = p7704_problem(raw)
    if fragment is None:
        assert problem is None
    else:
        assert fragment in problem


def test_p7704_check():
    c = check("P7704", "organization/4648")
    assert c.normalized == "organisation/4648" and not c.format_ok
    assert check("P7704", "organisation/4648").format_ok


# --- ISIL -------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DE-MUS-123456", "DE-MUS-123456"),
        ("de-mus-123456", "DE-MUS-123456"),
        ("de-1", "DE-1"),
        ("DE-Ks18", "DE-Ks18"),  # gemischte Schreibung bleibt erhalten
        ("http://ld.zdb-services.de/resource/organisations/DE-MUS-924218", "DE-MUS-924218"),
        ("https://sigel.staatsbibliothek-berlin.de/resource/organisations/DE-Lnn1", "DE-Lnn1"),
        (" DE-2294 ", "DE-2294"),
        ("Q12345", None),
        ("DE-", None),
        (None, None),
    ],
)
def test_normalize_isil(raw, expected):
    assert normalize_isil(raw) == expected


def test_isil_check_lowercase_prefix_is_format_error():
    c = check("P791", "de-1")
    assert not c.format_ok and c.normalized == "DE-1"


# --- GND / VIAF -------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://d-nb.info/gnd/2022341-9", "2022341-9"),
        ("https://d-nb.info/gnd/1125722673", "1125722673"),
        ("http://d-nb.info/gnd/5036103-x", "5036103-X"),
        ("36441-1", "36441-1"),
        ("http://d-nb.info/gnd/4034013-2/about", "4034013-2"),
        ("abc", None),
    ],
)
def test_normalize_gnd(raw, expected):
    assert normalize_gnd(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://viaf.org/viaf/148328332", "148328332"),
        ("https://viaf.org/viaf/148328332/", "148328332"),
        ("0123", None),
    ],
)
def test_normalize_viaf(raw, expected):
    assert normalize_viaf(raw) == expected
