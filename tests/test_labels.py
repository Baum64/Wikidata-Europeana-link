"""mul-/en-Labels für Museen, Bibliotheken und Archive ohne mul- und en-Label, ohne Netz."""

from glamsync.labels import country_langs, label_commands, label_proposals
from glamsync.orgs import Org

OFFICIAL = {"Q183": {"de"}, "Q31": {"nl", "fr", "de"}, "Q41": {"el"}, "Q403": {"sr"}, "Q20": {"no", "nb", "nn"},
            "Q36": {"pl"}, "Q38": {"it"}}


def one(labels, country="Q183", aliases=None, official_en=(), orgs=()):
    items = {"Q1": {"labels": labels, "aliases": aliases or {}, "countries": [country], "official_en": list(official_en)}}
    ps = label_proposals(items, OFFICIAL, {"Q1": list(orgs)} if orgs else None)
    return ps[0] if ps else None


def org(count=5, **pref):
    o = Org("7", next(iter(pref.values())), list(pref.values()), [], "Germany", count, ["Q1"])
    o.pref = dict(pref)
    return o


def test_country_langs():
    assert {"nb", "nn", "se"} <= country_langs(OFFICIAL, ["Q20"])
    assert {"it", "de", "fur"} <= country_langs(OFFICIAL, ["Q38"])  # Südtirol, Friaul
    assert country_langs(OFFICIAL, ["Q55"]) >= {"nl", "fy"}  # Niederlande ohne P37-Eintrag


def test_mul_from_native_label():
    p = one({"de": "Stormarnsches Dorfmuseum"})
    assert (p.mul, p.en, p.note) == ("Stormarnsches Dorfmuseum", "", "")
    assert label_commands([p], "Lmul") == [['Q1\tLmul\t"Stormarnsches Dorfmuseum"']]


def test_skips_items_with_en_or_mul():
    assert one({"de": "Stadtarchiv Worms", "en": "Worms City Archive"}) is None
    assert one({"mul": "Stadtarchiv Worms"}) is None


def test_language_variants_and_identical_labels():
    assert one({"de": "Kunsthaus Zürich", "de-ch": "Kunsthaus Zürich"}).mul == "Kunsthaus Zürich"
    assert one({"nb": "Nasjonalmuseet"}, "Q20").mul == "Nasjonalmuseet"
    assert one({"de": "Museum Ladin", "it": "Museum Ladin"}, "Q38").mul == "Museum Ladin"


def test_multilingual_country():
    p = one({"nl": "Koninklijke Bibliotheek van België", "fr": "Bibliothèque royale de Belgique"}, "Q31")
    assert p.mul == "" and "mehrere Namen" in p.note


def test_label_only_in_foreign_language():
    p = one({"fr": "Musée national de Varsovie"}, "Q36")
    assert p.mul == "" and "kein Label in einer Landessprache (fr)" in p.note


def test_non_latin():
    p = one({"el": "Μουσείο Μπενάκη"}, "Q41", official_en=["Benaki Museum"])
    assert (p.mul, p.en, p.en_source) == ("", "Benaki Museum", "P1448")
    assert "nichtlateinische Schrift" in p.note
    # Serbisch: lateinische Variante vorhanden → mul
    p = one({"sr": "Народни музеј", "sr-el": "Narodni muzej"}, "Q403")
    assert p.mul == "Narodni muzej"


def test_en_from_official_name():
    p = one({"de": "Ruhr-Universität Bochum"}, official_en=["Ruhr University Bochum"])
    assert (p.mul, p.en, p.en_source) == ("Ruhr-Universität Bochum", "Ruhr University Bochum", "P1448")
    assert label_commands([p], "Len") == [['Q1\tLen\t"Ruhr University Bochum"']]


def test_en_from_europeana():
    o = org(de="Sportmuseum Berlin", en="Berlin Sports Museum")
    p = one({"de": "Sportmuseum Berlin"}, orgs=[o])
    assert (p.en, p.en_source) == ("Berlin Sports Museum", "europeana")
    # P1448 geht vor
    p = one({"de": "Sportmuseum Berlin"}, official_en=["Sports Museum Berlin"], orgs=[o])
    assert (p.en, p.en_source) == ("Sports Museum Berlin", "P1448")


def test_europeana_only_english():
    p = one({"de": "Stadtarchiv Worms"}, orgs=[org(en="Stadtarchiv Worms")])
    assert (p.mul, p.en, p.note) == ("Stadtarchiv Worms", "", "")
    p = one({"de": "Heimatmuseum A"}, aliases={"de": ["Local Museum A"]}, orgs=[org(en="Local Museum A")])
    assert p.en == "" and "unklar ob Übersetzung" in p.note
    p = one({"de": "Heimatmuseum A"}, aliases={"en": ["Local Museum A"]}, orgs=[org(en="Local Museum A")])
    assert p.en == "Local Museum A"


def test_europeana_link_not_confirmed():
    p = one({"de": "Kunsthalle B"}, orgs=[org(de="Stadtmuseum A", en="City Museum A")])
    assert (p.mul, p.en) == ("Kunsthalle B", "") and "Verknüpfung" in p.note


def test_en_variant_or_partial_translation():
    p = one({"de": "Museum Charlottenburg-Wilmersdorf"},
            orgs=[org(de="Museum Charlottenburg-Wilmersdorf", en="Museum Charlottenburg-Wilmerdorf")])
    assert p.en == "" and "Variante" in p.note
    p = one({"de": "Rheinisches Schützenmuseum Neuss"}, official_en=["Rheinisches Schützenmuseum Neuss with Archive"])
    assert p.en == "" and "Variante" in p.note


def test_quotes_not_allowed():
    p = one({"pl": 'Biblioteka Cyfrowa "Pomerania"'}, "Q36")
    assert p.mul == "" and "ungeeignet" in p.note


def test_item_without_label():
    assert one({}).note == "Item ohne Label"


def test_defective_labels_held_back():
    for v, why in (("Poppelsdorfer Heimatmuseum\u200e", "unsichtbar"), ("Riedmuseum Ottersdorf&#8232;", "HTML"),
                   ("Bibliografen  Bagsværd", "Leerzeichen"), ("Riksarkivet (Sverige) [no]", "Klammern"),
                   ("Nationalregistret (Riksarkivet", "Klammern"), ("Sammlung -Humboldt-Universität-", "Satzzeichen")):
        p = one({"de": v})
        assert p.mul == "" and why in p.note, v
    assert one({"pl": "Muzeum Kultury Ludowej w\u00a0Węgorzewie"}, "Q36").mul == "Muzeum Kultury Ludowej w Węgorzewie"
    assert one({"de": "Danielsberg Medi@thek"}).mul == "Danielsberg Medi@thek"
