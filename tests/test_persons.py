"""Personenabgleich Europeana-Agents ↔ Wikidata, ohne Netz."""

from glamsync.models import FEHLT, IDENTISCH, KEIN_TREFFER, KONFLIKT, MEHRDEUTIG, Statement, WikidataItem
from glamsync.persons import (
    GND_PROBLEM,
    KEIN_MENSCH,
    agent_cleanup,
    classify_person,
    person_gnd,
)
from glamsync.sources.agents import _compact, to_record
from glamsync.sources.gnd import gnd_problem


def agent(n, gnd=(), qid="Q1"):
    return to_record({"id": str(n), "label": "Goethe", "qids": [qid], "gnd": list(gnd)})


def human(*gnd, p31=("Q5",)):
    claims = {"P227": [Statement(g, "Q1$x", "normal", False, False) for g in gnd]}
    return WikidataItem("Q1", labels={"de": "Goethe"}, p31=list(p31), claims=claims)


def test_compact_page():
    page = {"items": [{"id": "http://data.europeana.eu/agent/146741", "prefLabel": {"en": "Goethe"},
                       "sameAs": ["http://www.wikidata.org/entity/Q5879", "https://d-nb.info/gnd/118540238"]}],
            "next": "x", "partOf": {"total": 1}}
    assert _compact(page)["items"] == [{"id": "146741", "label": "Goethe", "qids": ["Q5879"], "gnd": ["118540238"]}]
    r = to_record(_compact(page)["items"][0])
    assert r.ids == {"P7704": ["agent/146741"], "P227": ["118540238"]}


def test_identisch():
    f, rec = classify_person("Q1", [agent(5)], {"agent/5"}, None, {}, {})
    assert f.status == IDENTISCH and rec.source_id == "5"


def test_konflikt_other_agent():
    f, rec = classify_person("Q1", [agent(5)], {"agent/9"}, None, {}, {})
    assert f.status == KONFLIKT and f.action is None and rec is None


def test_new_for_human():
    f, rec = classify_person("Q1", [agent(5, ["118540238"])], set(), human("118540238"), {}, {"118540238": {"Q1"}})
    assert f.status == FEHLT and f.action == "new" and f.source_value == "agent/5"


def test_missing_item_and_non_human():
    assert classify_person("Q1", [agent(5)], set(), None, {}, {})[0].status == KEIN_TREFFER
    assert classify_person("Q1", [agent(5)], set(), human(p31=["Q43229"]), {}, {})[0].status == KEIN_MENSCH


def test_agent_already_on_other_item():
    f, _ = classify_person("Q1", [agent(5)], set(), human(), {"agent/5": {"Q2"}}, {})
    assert f.status == MEHRDEUTIG and "Q2" in f.note


def test_gnd_points_to_other_item():
    f, _ = classify_person("Q1", [agent(5, ["118540238"])], set(), human(), {}, {"118540238": {"Q2"}})
    assert f.status == MEHRDEUTIG and "GND 118540238" in f.note


def test_gnd_differs_is_noted_but_new():
    f, _ = classify_person("Q1", [agent(5, ["118540238"])], set(), human("1234567890"), {}, {})
    assert f.action == "new" and "GND abweichend" in f.note


def test_duplicates():
    recs = [agent(5), agent(6)]
    assert classify_person("Q1", recs, set(), human(), {}, {}, {"5": 0, "6": 0})[0].status == MEHRDEUTIG
    f, rec = classify_person("Q1", recs, set(), human(), {}, {}, {"5": 1, "6": 40})
    assert f.action == "new" and rec.source_id == "6"
    assert classify_person("Q1", recs, {"agent/5"}, None, {}, {})[0].status == IDENTISCH


def test_person_gnd():
    ok = {"118540238": None}
    f = person_gnd("Q1", agent(5, ["118540238"]), human(), {}, ok)
    assert f.status == FEHLT and f.action == "new" and f.source_value == "118540238"
    assert person_gnd("Q1", agent(5, ["118540238"]), human("118540238"), {}, ok) is None  # hat schon P227
    assert person_gnd("Q1", agent(5, ["118540238"]), human(), {"118540238": {"Q2"}}, ok).status == MEHRDEUTIG
    bad = person_gnd("Q1", agent(5, ["4005728-8"]), human(), {}, {"4005728-8": "keine Person in der GND"})
    assert bad.status == GND_PROBLEM and bad.action is None
    assert person_gnd("Q1", agent(5, ["1", "2"]), human(), {}, {}).status == MEHRDEUTIG


def test_gnd_problem():
    assert gnd_problem("118540238", {"id": "118540238", "types": ["DifferentiatedPerson"]}) is None
    assert "umgelenkt" in gnd_problem("118540238", {"id": "118540239", "types": ["DifferentiatedPerson"]})
    assert "keine Person" in gnd_problem("4005728-8", {"id": "4005728-8", "types": ["PlaceOrGeographicName"]})
    assert "existiert nicht" in gnd_problem("1", None)
    assert "nicht prüfbar" in gnd_problem("1", {"error": 500})


def test_agent_cleanup():
    c = agent_cleanup("Q1", "agent/5", None, False, False, {})
    assert c.kind == "nicht_in_europeana" and c.action == "delete"
    assert agent_cleanup("Q1", "agent/5", None, True, False, {}).action is None
    c = agent_cleanup("Q1", "agent/5", {"id": "http://data.europeana.eu/agent/7"}, False, False, {})
    assert c.kind == "zusammengefuehrt" and c.action == "fix" and c.new_value == "agent/7"
    assert agent_cleanup("Q1", "agent/5", {"id": "http://data.europeana.eu/agent/7"}, False, False,
                         {"agent/7": {"Q2"}}).action is None
    assert agent_cleanup("Q1", "agent/5", {"id": "http://data.europeana.eu/agent/5"}, False, False, {}) is None


def test_fetch_all_dedupes_and_sorts(monkeypatch):
    from glamsync.sources import agents

    monkeypatch.setattr(agents, "_require_key", lambda: "x")
    pages = {1: [{"id": "1", "label": "", "qids": [], "gnd": []}, {"id": "2", "label": "", "qids": [], "gnd": []}],
             2: [{"id": "2", "label": "", "qids": [], "gnd": []}, {"id": "3", "label": "", "qids": [], "gnd": []}]}
    seen = []

    class Fake:
        def get_json(self, url, params, **kw):
            seen.append(params.get("sort"))
            return {"items": pages[params["page"]], "next": params["page"] < 2, "total": 3}

    assert [a["id"] for a in agents.fetch_all(Fake())] == ["1", "2", "3"]
    assert seen == ["id asc", "id asc"]


def test_mark_without_objects():
    from glamsync.models import FEHLT, IDENTISCH, Finding
    from glamsync.persons import OHNE_OBJEKTE, mark_without_objects

    a = Finding("P7704", FEHLT, "Q1", "europeana", "1", "agent/1", [], action="new")
    b = Finding("P7704", FEHLT, "Q2", "europeana", "2", "agent/2", [], action="new")
    c = Finding("P7704", IDENTISCH, "Q3", "europeana", "3", "agent/3", ["agent/3"])
    assert mark_without_objects([a, b, c], {"1": 5, "2": 0, "3": 0}) == 1
    assert (a.status, b.status, c.status) == (FEHLT, OHNE_OBJEKTE, IDENTISCH)
    assert b.action == "new" and "0 verknüpfte Objekte" in b.note


def test_mark_deprecated():
    from glamsync.models import FEHLT, Finding
    from glamsync.persons import ABGELEHNT, mark_deprecated

    a = Finding("P7704", FEHLT, "Q1", "europeana", "1", "agent/1", [], action="new")
    b = Finding("P7704", FEHLT, "Q2", "europeana", "2", "agent/2", [], action="new")
    # Q1 führt agent/base/1 als veraltet (normalisiert agent/1) → kein Vorschlag
    assert mark_deprecated([a, b], {"Q1": {"agent/1"}, "Q3": {"agent/2"}}) == 1
    assert (a.status, a.action, b.status, b.action) == (ABGELEHNT, None, FEHLT, "new")
