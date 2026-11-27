"""Organisationsabgleich Europeana ↔ Wikidata (europaweit), ohne Netz."""

from glamsync.models import FEHLT, IDENTISCH, KEIN_TREFFER, KONFLIKT, MEHRDEUTIG, Statement, WikidataItem
from glamsync.orgs import (
    KANDIDAT_SCHWACH,
    KANDIDAT_STARK,
    KEIN_ORGANISATION,
    NAME_PRUEFEN,
    Org,
    classify_candidate,
    classify_org,
    multi_qid,
    name_check,
    norm_name,
    org_from_raw,
    qs_groups,
    website_variants,
)
from glamsync.persons import ABGELEHNT, OHNE_OBJEKTE

D = "2026-11-27"
REF = f"|S248|Q234110|S813|+{D}T00:00:00Z/11"


def org(num="5", names=("Muzeul de Artă Iași",), count=10, qids=("Q1",), homepage=None, **ids):
    return Org(num, names[0], list(names), [homepage] if homepage else [], "Romania", count, list(qids),
               {p: list(ids.get(p, [])) for p in ("P227", "P214", "P213")})


def item(q="Q1", p7704=(), p31=("Q33506",)):
    sts = [Statement(v, f"{q}$x", rank, False, False) for v, rank in
           ((v, "normal") if isinstance(v, str) else v for v in p7704)]
    return WikidataItem(q, labels={"en": "x"}, p31=list(p31), claims={"P7704": sts} if sts else {})


def test_org_from_raw():
    raw = {"id": "http://data.europeana.eu/organization/4373", "prefLabel": {"en": "National Library of France",
           "fr": "Bibliothèque nationale de France"}, "acronym": {"fr": ["BnF"]}, "homepage": "http://www.bnf.fr",
           "country": {"prefLabel": {"en": "France"}}, "isAggregatedBy": {"recordCount": 30},
           "sameAs": ["http://www.wikidata.org/entity/Q193563", "http://d-nb.info/gnd/5156217-0",
                      "http://viaf.org/viaf/137156173", "http://isni.org/isni/0000000123531945",
                      "http://data.europeana.eu/organization/1482250000002112001"]}
    o = org_from_raw(raw)
    assert o.value == "organisation/4373" and o.qids == ["Q193563"] and o.record_count == 30
    assert "BnF" in o.names and "Bibliothèque nationale de France" in o.names
    assert o.ids == {"P227": ["5156217-0"], "P214": ["137156173"], "P213": ["0000000123531945"]}
    assert o.homepages == ["http://www.bnf.fr"] and o.country == "France"


def test_norm_name_and_check():
    assert norm_name("Muzeul de Artă „Iași“") == "muzeul de arta iasi"
    assert name_check(org(), ["Muzeul de Arta Iasi"]) == (True, 1.0)
    same, ratio = name_check(org(names=("Moldova National Museum Complex - Art Museum",)), ["Muzeul de Artă Iași"])
    assert not same and ratio < 0.85
    assert name_check(org(names=("BnF", "x")), ["BNF"])[0] is False  # unter 4 Zeichen zählt nicht


def test_website_variants():
    full, dom = website_variants("https://www.filmschool.lodz.pl/en/")
    assert "http://filmschool.lodz.pl/en" in full and "https://www.filmschool.lodz.pl/en/" in full
    assert "https://filmschool.lodz.pl/" in dom and not full & dom
    full, dom = website_variants("bufvc.ac.uk")
    assert "http://bufvc.ac.uk/" in full and dom == set()
    assert website_variants("not a url") == (set(), set())


# --- Stufe 1 ---------------------------------------------------------------------
def test_identisch_also_with_old_spelling():
    f, o = classify_org("Q1", [org()], item(p7704=["organization/5"]), [], True, {})
    assert f.status == IDENTISCH and o.num == "5"


def test_konflikt_other_value_including_agent():
    assert classify_org("Q1", [org()], item(p7704=["organisation/9"]), [], True, {})[0].status == KONFLIKT
    f, _ = classify_org("Q1", [org()], item(p7704=["agent/123"]), [], True, {})
    assert f.status == KONFLIKT and "agent/123" in f.note


def test_kein_treffer_and_kein_organisation():
    assert classify_org("Q1", [org()], None, [], True, {})[0].status == KEIN_TREFFER
    f, _ = classify_org("Q1", [org()], item(p31=["Q16560"]), ["Muzeul de Artă Iași"], False, {})
    assert f.status == KEIN_ORGANISATION and f.action is None


def test_fehlt_with_and_without_objects():
    f, _ = classify_org("Q1", [org()], item(), ["Muzeul de Arta Iasi"], True, {})
    assert f.status == FEHLT and f.action == "new" and f.source_value == "organisation/5"
    f, _ = classify_org("Q1", [org(count=0)], item(), ["Muzeul de Arta Iasi"], True, {})
    assert f.status == OHNE_OBJEKTE and f.action == "new"


def test_name_pruefen():
    f, _ = classify_org("Q1", [org(names=("American School of Classical Studies at Athens",))], item(),
                        ["Autonomous Region of Sardinia"], True, {})
    assert f.status == NAME_PRUEFEN and f.action == "new" and "Ähnlichkeit" in f.note


def test_value_elsewhere_and_deprecated():
    f, _ = classify_org("Q1", [org()], item(), ["Muzeul de Arta Iasi"], True, {"organisation/5": {"Q2"}})
    assert f.status == MEHRDEUTIG and "Q2" in f.note
    f, _ = classify_org("Q1", [org()], item(p7704=[("organisation/5", "deprecated")]), ["Muzeul de Arta Iasi"], True, {})
    assert f.status == ABGELEHNT and f.action is None


def test_duplicate_chosen_by_objects():
    a, b = org("1", count=7740), org("2", count=32)
    f, o = classify_org("Q1", [b, a], item(), ["Muzeul de Arta Iasi"], True, {})
    assert f.status == FEHLT and o.num == "1" and "Dublette" in f.note
    f, _ = classify_org("Q1", [org("1", count=3), org("2", count=3)], item(), [], True, {})
    assert f.status == MEHRDEUTIG


def test_multi_qid():
    o = org(qids=("Q1", "Q2"))
    assert multi_qid(o, {"Q2": item("Q2", ["organisation/5"])}).status == IDENTISCH
    assert multi_qid(o, {}).status == MEHRDEUTIG


# --- Stufe 2 ---------------------------------------------------------------------
NAMES = {"Q7": ["Hälsinglands Museum"], "Q8": ["Hälsinglands museum (Hudiksvall)"], "Q9": ["Kommune"]}


def cand(o, cands, items=None, is_org=None, claimed=None, owner=None):
    items = items or {q: item(q) for q in cands}
    return classify_candidate(o, cands, items, NAMES, is_org or {q: True for q in cands}, claimed or {}, owner or {})


def test_candidate_value_already_in_wikidata():
    f = cand(org(qids=()), {}, owner={"organisation/5": {"Q3"}})
    assert f.status == IDENTISCH and f.qid == "Q3" and "ohne sameAs" in f.note


def test_candidate_strong_and_weak():
    o = org(names=("Hälsinglands Museum",), qids=())
    f = cand(o, {"Q7": {"website"}, "Q9": {"website"}})
    assert f.status == KANDIDAT_STARK and f.qid == "Q7" and f.action == "new"
    assert cand(o, {"Q7": {"domain"}}).status == KANDIDAT_SCHWACH          # Domain + gleicher Name
    f = cand(o, {"Q8": {"website"}})                                         # Website + ähnlicher Name
    assert f.status == KANDIDAT_SCHWACH and "ähnlich" in f.note
    assert cand(o, {"Q9": {"website"}}).status == KEIN_TREFFER               # Name passt nicht
    assert cand(o, {"Q7": {"P227"}}).status == KANDIDAT_STARK                # GND + gleicher Name


def test_candidate_excluded_and_conflicts():
    o = org(names=("Hälsinglands Museum",), qids=())
    assert cand(o, {"Q7": {"website"}}, is_org={"Q7": False}).status == KEIN_TREFFER
    f = cand(o, {"Q7": {"website"}}, claimed={"Q7": ["99"]})
    assert f.status == KEIN_TREFFER and "organisation/99" in f.note
    f = cand(o, {"Q7": {"website"}}, items={"Q7": item("Q7", ["organisation/12"])})
    assert f.status == KONFLIKT and f.action is None
    names2 = dict(NAMES, Q6=["Hälsinglands Museum"])
    f = classify_candidate(o, {"Q6": {"website"}, "Q7": {"website"}}, {"Q6": item("Q6"), "Q7": item("Q7")},
                           names2, {"Q6": True, "Q7": True}, {}, {})
    assert f.status == MEHRDEUTIG


# --- QuickStatements --------------------------------------------------------------
def test_qs_groups_commented():
    o = org()
    sure, _ = classify_org("Q1", [o], item(), ["Muzeul de Arta Iasi"], True, {})
    check, _ = classify_org("Q2", [org("6", names=("Brukenthal - History Museum",))], item("Q2"), ["Altes Rathaus"], True, {})
    groups = qs_groups([sure, check], D, {"5": o, "6": org("6", names=("Brukenthal - History Museum",))},
                       {"Q2": "Altes Rathaus"}, {NAME_PRUEFEN})
    assert groups[0] == [f'Q1|P7704|"organisation/5"{REF}']
    assert groups[1][0].startswith("# organisation/6 „Brukenthal - History Museum“ ↔ Q2 „Altes Rathaus“ – ")
    assert groups[1][1] == f'#Q2|P7704|"organisation/6"{REF}'


# --- Geduldiger Modus bei Drosselung ------------------------------------------------
def test_patient_query_retries_after_throttling(monkeypatch):
    import pytest

    from glamsync.http import SkippedRequest
    from glamsync.sources import wikidata as W

    class Client:
        calls = 0

        def post_json(self, *a, **k):
            Client.calls += 1
            if Client.calls == 1:
                exc = SkippedRequest("HTTP 429")
                exc.delay = 120
                raise exc
            return {"results": {"bindings": [{"x": {"value": "1"}}]}}

    slept = []
    monkeypatch.setattr(W.time, "sleep", slept.append)
    assert W.Wikidata(Client(), patient=True).query("q") == [{"x": "1"}] and slept == [120]
    Client.calls = 0
    with pytest.raises(SkippedRequest):
        W.Wikidata(Client()).query("q")  # ohne patient: wie bisher abbrechen
