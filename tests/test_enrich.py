"""Anreicherung aus der GND (P214, P213, P571, P131, P31), ohne Netz."""

from glamsync.enrich import class_proposal, id_proposals, inception_proposal, place_proposal
from glamsync.sources.gnd import GndBody

HD = {"qid": "Q2966", "name": "Heidelberg"}


def ent(labels=None, **claims):
    return {"labels": labels or {"de": "X"},
            "claims": {p: [{"value": v, "rank": "normal", "refs": 0, "quals": 0, "id": "s"} if not isinstance(v, dict) else v
                           for v in vs] for p, vs in claims.items()}}


def body(**kw):
    return GndBody(gnd="1", name=kw.pop("name", "Museum X"), places=kw.pop("places", [HD]), **kw)


def test_ids_only_when_missing_and_free():
    b = body(viaf=["123"], isni=["0000000122905087"])
    ps = id_proposals("Q1", ent(), b, {"P214": {}, "P213": {"0000000122905087": ["Q9"]}})
    assert [(p.prop, p.action) for p in ps] == [("P214", "neu"), ("P213", "bericht")]
    assert id_proposals("Q1", ent(P214=["5"]), body(viaf=["123"]), {}) == []
    assert ps[0].commands("2026-11-24") == ['Q1\tP214\t"123"\tS248\tQ36578\tS813\t+2026-11-24T00:00:00Z/11']


def test_inception_exact_year_only():
    assert inception_proposal("Q1", ent(), body(start_year=1907)).value == "+1907-00-00T00:00:00Z/9"
    assert inception_proposal("Q1", ent(), body(established="19XX")) is None
    assert inception_proposal("Q1", ent(P571=["+1900"]), body(start_year=1907)) is None


def test_place_district_in_name():
    d = {"Q2966": {"Kirchheim", "Handschuhsheim"}}
    assert place_proposal("Q1", ent({"de": "Heimatmuseum Kirchheim"}), body(name="Heimatmuseum Kirchheim"), d).action == "bericht"
    assert place_proposal("Q1", ent({"de": "Kurpfälzisches Museum"}), body(), d).action == "neu"
    # Stadt und Ortsteil im Namen → Stadt passt
    assert place_proposal("Q1", ent({"de": "Heidelberg-Kirchheim Archiv"}), body(name="Archiv"), d).action == "neu"
    assert place_proposal("Q1", ent(P131=["Q5"]), body(), d) is None
    assert place_proposal("Q1", ent(), body(places=[HD, {"qid": "Q64", "name": "Berlin"}]), d) is None


def test_class_replace_generic():
    cm, cr = {"Heimatmuseum": "Q2516357"}, {"Q2516357": "Q33506"}
    p = class_proposal("Q1", ent(P31=["Q33506"]), body(broader=["Heimatmuseum"]), cm, cr)
    assert p.remove == "Q33506" and p.commands("2026-11-24")[0] == "-Q1\tP31\tQ33506"
    ref = {"value": "Q33506", "rank": "normal", "refs": 1, "quals": 0, "id": "s"}
    assert class_proposal("Q1", ent(P31=[ref]), body(broader=["Heimatmuseum"]), cm, cr).remove == ""
    assert class_proposal("Q1", ent(P31=["Q33506", "Q41176"]), body(broader=["Heimatmuseum"]), cm, cr) is None
    assert class_proposal("Q1", ent(P31=["Q7075"]), body(broader=["Heimatmuseum"]), cm, cr) is None


def test_place_name_with_qualifier():
    pol = {"qid": "Q1", "name": "Polling (Landkreis Weilheim-Schongau)"}
    d = {"Q1": {"Polling", "Etting"}}
    assert place_proposal("Q9", ent({"de": "Klosterbibliothek Polling"}), body(name="Klosterbibliothek Polling", places=[pol]), d).action == "neu"
    assert place_proposal("Q9", ent({"de": "Heimatstube Etting"}), body(name="Heimatstube Etting", places=[pol]), d).action == "bericht"
