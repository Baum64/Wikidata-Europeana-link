"""Tests der Klassifikation und des QS-Exports anhand kleiner Fixtures (ohne Netz)."""

from glamsync.export import qs_commands, split_parts
from glamsync.match import (
    build_lookups,
    classify_property,
    decide_qid,
    p7704_duplicates,
    resolve_collisions,
    wikidata_format_findings,
)
from glamsync.models import (
    FEHLT,
    FORMATFEHLER,
    IDENTISCH,
    KEIN_TREFFER,
    KONFLIKT,
    MEHRDEUTIG,
    Finding,
    SourceRecord,
    Statement,
    WikidataItem,
)


def st(value, refs=False, quals=False, rank="normal"):
    return Statement(value, "Q1$x", rank, refs, quals)


def rec(**ids):
    qids = ids.pop("qids", [])
    return SourceRecord("europeana", "4373", "Test", "", qids=qids, ids=ids)


def item(**claims):
    return WikidataItem("Q1", claims={p: [st(v) if isinstance(v, str) else v for v in vs] for p, vs in claims.items()})


NO_LOOKUPS = build_lookups({})


# --- Zuordnung ------------------------------------------------------------------
def test_decide_prefers_sameas_and_notes_disagreement():
    lk = build_lookups({"P227": {"36441-1": ["Q2"]}})
    m = decide_qid(rec(qids=["Q1"], P227=["36441-1"]), lk, {})
    assert m.qid == "Q1" and m.via == "sameAs" and "GND zeigt auf Q2" in m.note


def test_decide_follows_redirect():
    assert decide_qid(rec(qids=["Q9"]), NO_LOOKUPS, {"Q9": "Q1"}).qid == "Q1"


def test_decide_isil_case_insensitive():
    lk = build_lookups({"P791": {"DE-KS18": ["Q5"]}})
    m = decide_qid(rec(P791=["DE-Ks18"]), lk, {})
    assert m.qid == "Q5" and m.via == "ISIL"


def test_decide_ambiguous_and_no_hit():
    lk = build_lookups({"P227": {"1-1": ["Q2", "Q3"]}})
    assert decide_qid(rec(P227=["1-1"]), lk, {}).status == MEHRDEUTIG
    assert decide_qid(rec(), NO_LOOKUPS, {}).status == KEIN_TREFFER


# --- Klassifikation -------------------------------------------------------------
def test_identisch():
    f = classify_property(rec(P7704=["organisation/4373"]), "Q1", item(P7704=["organisation/4373"]), "P7704", NO_LOOKUPS)
    assert f.status == IDENTISCH and f.action is None


def test_fehlt():
    f = classify_property(rec(P7704=["organisation/4373"]), "Q1", item(), "P7704", NO_LOOKUPS)
    assert f.status == FEHLT and f.action == "new"


def test_fehlt_but_value_used_elsewhere():
    lk = build_lookups({"P7704": {"organization/4373": ["Q2"]}})
    f = classify_property(rec(P7704=["organisation/4373"]), "Q1", item(), "P7704", lk)
    assert f.status == MEHRDEUTIG and "Q2" in f.note


def test_organization_spelling_is_fixed():
    # gleicher Wert, aber 'organization/' verletzt den Format-Constraint → Korrektur
    f = classify_property(rec(P7704=["organisation/4373"]), "Q1", item(P7704=["organization/4373"]), "P7704", NO_LOOKUPS)
    assert f.status == FORMATFEHLER and f.action == "fix" and f.old_value == "organization/4373"


def test_formatfehler_fix():
    f = classify_property(rec(P7704=["organisation/4373"]), "Q1", item(P7704=["http://data.europeana.eu/organisation/4373"]), "P7704", NO_LOOKUPS)
    assert f.status == FORMATFEHLER and f.action == "fix" and f.old_value == "http://data.europeana.eu/organisation/4373"


def test_formatfehler_with_refs_is_report_only():
    it = item(P7704=[st("http://data.europeana.eu/organisation/4373", refs=True)])
    f = classify_property(rec(P7704=["organisation/4373"]), "Q1", it, "P7704", NO_LOOKUPS)
    assert f.status == FORMATFEHLER and f.action is None and "manuell" in f.note


def test_konflikt():
    f = classify_property(rec(P227=["36441-1"]), "Q1", item(P227=["2022341-9"]), "P227", NO_LOOKUPS)
    assert f.status == KONFLIKT and f.action is None


def test_deprecated_is_ignored():
    it = item(P227=[st("2022341-9", rank="deprecated")])
    f = classify_property(rec(P227=["36441-1"]), "Q1", it, "P227", NO_LOOKUPS)
    assert f.status == FEHLT


def test_multiple_viaf_in_source_is_ambiguous():
    f = classify_property(rec(P214=["1", "2"]), "Q1", item(), "P214", NO_LOOKUPS)
    assert f.status == MEHRDEUTIG


def test_legacy_p7704():
    r = rec(P7704=["organisation/4373"])
    r.legacy_p7704 = ["organisation/1482250000004502115"]
    f = classify_property(r, "Q1", item(P7704=["organisation/1482250000004502115"]), "P7704", NO_LOOKUPS)
    assert f.status == FORMATFEHLER and f.action is None


def test_collisions():
    a = Finding("P7704", FEHLT, "Q1", "europeana", "1", "organisation/1", action="new")
    b = Finding("P7704", FEHLT, "Q1", "europeana", "2", "organisation/2", action="new")
    c = Finding("P227", FEHLT, "Q1", "ddb", "x", "1-1", action="new")
    d = Finding("P227", FEHLT, "Q1", "europeana", "y", "1-1", action="new")  # gleicher Wert: ok
    resolve_collisions([a, b, c, d])
    assert a.status == b.status == MEHRDEUTIG and a.action is None
    assert c.action == d.action == "new"


# --- Wikidata-Formatprüfung -----------------------------------------------------
def test_wikidata_format_findings():
    items = {"Q1": item(P7704=["http://data.europeana.eu/organisation/4373"], P213=["0000 0001 2290 5087"])}
    fs = {f.prop: f for f in wikidata_format_findings(items, {"4373"}, set())}
    assert fs["P7704"].action == "fix" and fs["P7704"].source == "europeana"
    assert fs["P213"].action == "fix" and fs["P213"].source_value == "0000000122905087"


def test_wikidata_format_unknown_org_id_not_fixed():
    fs = wikidata_format_findings({"Q1": item(P7704=["http://data.europeana.eu/organisation/99999"])}, {"4373"}, set())
    assert fs[0].action is None


def test_wikidata_organization_spelling_fix():
    fs = wikidata_format_findings({"Q1": item(P7704=["organization/4373"])}, {"4373"}, set())
    assert len(fs) == 1 and fs[0].action == "fix" and fs[0].source_value == "organisation/4373"
    assert fs[0].source == "europeana" and "amerikanische" in fs[0].note


def test_wikidata_invalid_p7704_delete():
    fs = wikidata_format_findings({"Q1": item(P7704=["item/2048128/618580"])}, set(), set())
    assert fs[0].action == "delete" and "P727" in fs[0].note


def test_wikidata_invalid_with_refs_report_only():
    fs = wikidata_format_findings({"Q1": item(P791=[st("DE-MUS", refs=True)])}, set(), set())
    assert fs[0].action is None and "manuell" in fs[0].note


def test_qs_delete():
    f = Finding("P791", FORMATFEHLER, "Q1", "wikidata", "", None, ["DE-MUS"], old_value="DE-MUS", action="delete")
    assert qs_commands([f, f], "P791", "delete", "2026-11-16") == [['-Q1|P791|"DE-MUS"']]


def test_p7704_duplicates():
    scan = [{"qid": "Q18286386", "value": "organization/4648", "rank": "NormalRank"},
            {"qid": "Q76632421", "value": "organization/4648", "rank": "NormalRank"}]
    assert {f.qid for f in p7704_duplicates(scan)} == {"Q18286386", "Q76632421"}


# --- QS-Export ------------------------------------------------------------------
def test_qs_new_and_fix():
    new = Finding("P7704", FEHLT, "Q193563", "europeana", "1234", "organisation/1234", action="new")
    fix = Finding("P7704", FORMATFEHLER, "Q2", "europeana", "1", "organisation/1", ["organization/1"],
                  old_value="organization/1", action="fix")
    assert qs_commands([new, new], "P7704", "new", "2026-11-15") == [
        ['Q193563|P7704|"organisation/1234"|S248|Q234110|S813|+2026-11-15T00:00:00Z/11']]
    assert qs_commands([fix], "P7704", "fix", "2026-11-15") == [[
        '-Q2|P7704|"organization/1"',
        'Q2|P7704|"organisation/1"|S248|Q234110|S813|+2026-11-15T00:00:00Z/11']]


def test_split_parts_keeps_pairs_together():
    parts = split_parts([["a", "b"]] * 30, size=50)
    assert [len(p) for p in parts] == [50, 10]


def test_split_parts_default_500():
    assert [len(p) for p in split_parts([["a"]] * 1203)] == [500, 500, 203]


def test_duplicate_values_are_not_fixed():
    from glamsync.match import hold_duplicate_fixes

    items = {"Q1": item(P7704=["organization/4648"]), "Q2": item(P7704=["organization/4648"])}
    fs = wikidata_format_findings(items, {"4648"}, set())
    scan = [{"qid": q, "value": "organization/4648", "rank": "ontology#NormalRank"} for q in items]
    fs += p7704_duplicates(scan)
    assert hold_duplicate_fixes(fs) == 2
    assert all(f.action is None for f in fs) and "Dublette" in fs[0].note
