"""Websites (P856): Adressen, Linkprüfung (ohne Netz), Namensnachweis, Klassifikation, QuickStatements."""

from glamsync import websites as W
from glamsync.sources.linkcheck import Check, parse_page

D = "2026-11-28"


def ok(url, final=None, title="", lang="de", **kw):
    return Check(url=url, status=200, final=final or url, title=title, lang=lang, **kw)


def info(q="Q1", label="Heimatmuseum Weißenhorn", sources=(), places=("Weißenhorn",), **kw):
    return W.ItemInfo(q, label, names=[label], places=list(places), sources=list(sources), **kw)


def src(source="isil", url="http://www.heimatmuseum-weissenhorn.de", key="DE-MUS-1", ref="https://w3id.org/isil/DE-MUS-1"):
    return W.SourceUrl(source, key, url, ref, "")


def classify(i, checks, owners=None, shared=None, country="Q183"):
    return W.classify_item(i, {c.url: c for c in checks}, owners or {}, shared or {}, country)


# --- Adressen ------------------------------------------------------------------------------
def test_url_problem():
    assert W.url_problem("www.museum.de") == ""
    assert W.url_problem("https://www.müller-museum.de/") == ""
    assert W.url_problem("https://koblach.at/museum/ ") == ""  # Rand wird ignoriert
    assert "Leerzeichen" in W.url_problem("http://www.remagen.de/Museen/#Römisches Museum")
    assert "Umlaute" in W.url_problem("http://www.remagen.de/#RÃ¶misches")
    assert "Datei" in W.url_problem("http://www.mikwe.de/files/Rollup.pdf")
    assert "sozial" in W.url_problem("https://instagram.com/buecherei.stamsried")
    assert "sozial" in W.url_problem("https://de.wikipedia.org/wiki/Museum")
    assert W.url_problem("mailto:info@museum.de") == "keine Website"
    assert W.url_problem("ftp://museum.de").startswith("Schema")
    assert W.url_problem("http://localhost/") == "keine Domain"
    assert W.url_problem("") == "leer"


def test_registrable():
    assert W.registrable("museum.krumbach.de") == "krumbach.de"
    assert W.registrable("www.wienmuseum.at") == "wienmuseum.at"
    assert W.registrable("landesmuseum.ktn.gv.at") == "ktn.gv.at"


def test_proposed_value():
    # nur Domain: Startseite mit Schema/Host des Ziels, auch wenn auf /start oder /de/ umgeleitet wird
    assert W.proposed_value("http://www.kuenzelsau.de", "https://www.kuenzelsau.de/start") == "https://www.kuenzelsau.de/"
    assert W.proposed_value("www.rohnerhaus.at", "https://www.rohnerhaus.at/home") == "https://www.rohnerhaus.at/"
    # Unterseite: Zieladresse ohne Tracking, Session und Fragment
    assert W.proposed_value("http://x.de/a", "https://x.de/b/?utm_source=x&id=5#top") == "https://x.de/b/?id=5"
    assert W.proposed_value("http://x.de/a", "https://x.de/b;jsessionid=AB12?p=1") == "https://x.de/b?p=1"
    # Umzug von einer Domain: flaches Ziel → Startseite, tiefes Ziel → volle Adresse
    assert W.proposed_value("http://www.bergbaumuseum-oelsnitz.de/", "https://www.kohlewelt.de/de/") == "https://www.kohlewelt.de/"
    assert W.proposed_value("http://www.feuerwehrmuseum-marxen.de",
                            "https://www.kiekeberg-museum.de/de/aussenstellen/feuerwehrmuseum-in-marxen") == \
        "https://www.kiekeberg-museum.de/de/aussenstellen/feuerwehrmuseum-in-marxen"


def test_url_state():
    assert W.url_state(ok("http://x.de"))[0] == W.OK
    assert W.url_state(Check("http://x.de", status=404, final="http://x.de/"))[0] == W.TOT
    assert W.url_state(Check("http://x.de", error="dns")) == (W.TOT, "Domain unbekannt")
    assert W.url_state(Check("http://x.de", error="timeout"))[0] == W.UNKLAR
    assert W.url_state(Check("http://x.de", status=403, final="http://x.de/"))[0] == W.UNKLAR
    assert W.url_state(ok("http://x.de", parked=True))[0] == W.TOT
    assert W.url_state(ok("http://x.de", soft404=True))[0] == W.TOT
    assert W.url_state(ok("http://x.de", challenge=True))[0] == W.UNKLAR
    assert W.url_state(ok("http://www.x.de", "https://x.de/"))[0] == W.OK
    assert W.url_state(ok("http://musa.at", "https://www.wienmuseum.at/"))[0] == W.EXTERN


# --- Seiteninhalt ---------------------------------------------------------------------------
def test_parse_page():
    p = parse_page('<html lang="de-DE"><head><title>  Heimatmuseum &amp; Archiv\n Weißenhorn </title></head>')
    assert p["title"] == "Heimatmuseum & Archiv Weißenhorn" and p["lang"] == "de-DE"
    assert not p["parked"] and not p["soft404"] and not p["refresh"]
    assert parse_page("<title>x.de</title><p>Diese Domain kaufen? Domain kaufen.</p>")["parked"]
    assert parse_page('<div class="fallback-term-link">')["parked"]
    assert parse_page("<title>Host Europe GmbH – www.heinrich-boell.de</title>")["parked"]
    assert parse_page("<title>Seite nicht gefunden</title>")["soft404"]
    assert parse_page("<title>404 Not Found</title>")["soft404"]
    assert parse_page("<title>Just a moment...</title>")["challenge"]
    r = parse_page('<meta http-equiv="refresh" content="0; URL=https://www.lvr.de/industriemuseum/">')
    assert r["refresh"] == "https://www.lvr.de/industriemuseum/"
    assert parse_page('<meta http-equiv="refresh" content="300">')["refresh"] == ""  # nur Neuladen


# --- Namensnachweis -------------------------------------------------------------------------
def test_evidence_title_and_address():
    names = ["Passauer Glasmuseum"]
    assert W.evidence(names, ["Passau"], ok("http://www.glasmuseum.de", title="Startseite"), "http://www.glasmuseum.de/")[0] == "adresse"
    assert W.evidence(["Biodiversitätsmuseum Göttingen"], [], ok("x", title="Biodiversity Museum Goettingen"),
                      "https://www.biodivmuseum.de/")[0] == "titel"
    assert W.evidence(["Museum Stangenberg Merck"], [], ok("x", title="DAS MUSEUM"), "https://www.museum-jugenheim.info/")[0] == ""


def test_evidence_place_only_on_municipal_homepage():
    names, places = ["Stadtarchiv Weinsberg"], ["Weinsberg"]
    ev, why = W.evidence(names, places, ok("x", title="Stadt Weinsberg"), "https://www.weinsberg.de/")
    assert ev == "" and "Gemeindeseite" in why
    # Unterseite der Gemeinde oder Domain mit Einrichtungswort: Ortsname reicht
    assert W.evidence(names, places, ok("x", title="Stadtarchiv"), "https://www.weinsberg.de/kultur/stadtarchiv/")[0]
    assert W.evidence(["Heimatmuseum Weißenhorn"], ["Weißenhorn"], ok("x", title="Willkommen"),
                      "http://www.heimatmuseum-weissenhorn.de/")[0] == "adresse"


# --- Klassifikation -------------------------------------------------------------------------
def test_neu_with_language_and_reference():
    s = src()
    p = classify(info(sources=[s]), [ok(s.url, "https://www.heimatmuseum-weissenhorn.de/", title="Heimatmuseum Weißenhorn")])
    assert p.status == W.NEU and p.value == "https://www.heimatmuseum-weissenhorn.de/" and p.lang == "de"
    assert p.evidence == "titel"
    assert W.commands(p, D) == [
        f'Q1\tP856\t"https://www.heimatmuseum-weissenhorn.de/"\tP407\tQ188\tS248\tQ28657655\tS813\t+{D}T00:00:00Z/11']


def test_language_only_for_country_languages():
    s = src()
    c = ok(s.url, title="Heimatmuseum Weißenhorn", lang="en-US")
    assert classify(info(sources=[s]), [c]).lang == ""
    assert classify(info(sources=[s]), [ok(s.url, title="Musée Weißenhorn", lang="fr")], country="Q39").lang == "fr"


def test_several_sources_one_reference_each():
    a = src("gnd", "https://heimatmuseum-weissenhorn.de", "123-4", "https://d-nb.info/gnd/123-4")
    b = src("isil", "http://www.heimatmuseum-weissenhorn.de/")
    m = src("mure", "heimatmuseum-weissenhorn.de", "M10001", "https://museen-in-oesterreich.at/m/1")
    checks = [ok(x.url, "https://www.heimatmuseum-weissenhorn.de/", title="Heimatmuseum") for x in (a, b, m)]
    p = classify(info(sources=[a, b, m]), checks)
    assert p.status == W.NEU and [s.source for s in p.sources] == ["isil", "mure", "gnd"]
    lines = W.commands(p, D)
    assert len(lines) == 3 and "\tP407\tQ188\tS248\tQ28657655" in lines[0]
    assert lines[1].endswith(f'\tS854\t"https://museen-in-oesterreich.at/m/1"\tS813\t+{D}T00:00:00Z/11')
    assert "P407" not in lines[1] and "\tS248\tQ36578" in lines[2]


def test_pruefen_without_evidence_is_commented():
    s = src(url="http://www.dmg-web.de/?page=1")
    p = classify(info(label="Orientalische Münzsammlung", places=["Halle"], sources=[s]),
                 [ok(s.url, title="DMG Homepage")])
    assert p.status == W.PRUEFEN
    lines = W.commands(p, D)
    assert lines[0].startswith("# Q1 „Orientalische Münzsammlung“ ← ISIL-Register DE-MUS-1 – Titel „DMG Homepage“")
    assert all(x.startswith("#") for x in lines) and lines[1].startswith('#Q1\tP856\t"http://www.dmg-web.de/?page=1"')


def test_moved_with_title_goes_to_pruefen_with_new_address():
    s = src(url="https://www.fischereimuseum-hohnstorf.de/")
    p = classify(info(label="Fischereimuseum Hohnstorf", places=["Hohnstorf (Elbe)"], sources=[s]),
                 [ok(s.url, "https://www.fischereimuseum-hohnstorf-elbe.de/",
                     title="Willkommen im Fischereimuseum Hohnstorf/Elbe e.V.")])
    assert p.status == W.PRUEFEN and p.value == "https://www.fischereimuseum-hohnstorf-elbe.de/"
    assert p.note.startswith("umgezogen") and W.commands(p, D)[1].startswith("#Q1\tP856\t")
    # neue Domain, Name nicht im Titel → nur Bericht
    s = src(url="http://www.bergbaumuseum-oelsnitz.de/")
    p = classify(info(label="Bergbaumuseum Oelsnitz", places=["Oelsnitz"], sources=[s]),
                 [ok(s.url, "https://www.kohlewelt.de/de/", title="KohleWelt - Museum Steinkohlenbergbau Sachsen")])
    assert p.status == W.UMGELEITET


def test_moved_without_title_is_report_only():
    s = src(url="http://www.landesmuseum.at/")
    p = classify(info(label="Landesgalerie Linz", places=["Linz"], sources=[s]),
                 [ok(s.url, "https://www.ooekultur.at/", title="OÖ Landes-Kultur GmbH")])
    assert p.status == W.UMGELEITET


def test_dead_unclear_invalid():
    s = src()
    assert classify(info(sources=[s]), [Check(s.url, status=404, final=s.url)]).status == W.TOT
    assert classify(info(sources=[s]), [Check(s.url, error="timeout")]).status == W.UNKLAR
    bad = src(url="http://x.de/#Römisches Museum")
    assert classify(info(sources=[bad]), []).status == W.UNGUELTIG
    # eine tote und eine erreichbare Adresse: Vorschlag, die tote steht im Hinweis
    g = src("gnd", "http://www.heimatmuseum-weissenhorn.de/alt.html", "1-2", "https://d-nb.info/gnd/1-2")
    p = classify(info(sources=[s, g]), [ok(s.url, title="Heimatmuseum"), Check(g.url, status=404, final=g.url)])
    assert p.status == W.NEU and "gnd:" in p.note and "HTTP 404" in p.note


def test_shared_with_other_item_or_in_run():
    s = src(url="http://www.weinsberg.de/")
    owners = {"weinsberg.de": {"Q99"}}
    p = classify(info(sources=[s]), [ok(s.url, "https://www.weinsberg.de/")], owners=owners)
    assert p.status == W.GETEILT and "Q99" in p.note
    w = src(url="https://www.wienmuseum.at/")
    p = classify(info(sources=[w]), [ok(w.url, title="Wien Museum")], shared={"wienmuseum.at": {"Q1", "Q2"}})
    assert p.status == W.GETEILT and "Q2" in p.note


def test_contradiction_dissolved_deprecated():
    a, b = src(url="http://www.museum-a.de"), src("gnd", "http://www.museum-b.de", "1-2", "")
    p = classify(info(sources=[a, b]), [ok(a.url, title="Heimatmuseum"), ok(b.url, title="Heimatmuseum")])
    assert p.status == W.WIDERSPRUCH
    s = src()
    assert classify(info(sources=[s], dissolved="P576"), [ok(s.url, title="Heimatmuseum")]).status == W.AUFGELOEST
    d = info(sources=[s], deprecated=["https://heimatmuseum-weissenhorn.de/"])
    assert classify(d, [ok(s.url, title="Heimatmuseum")]).status == W.VERALTET


def test_resolve_collisions():
    a = W.Proposal("Q1", "a", W.NEU, "https://x.de/")
    b = W.Proposal("Q2", "b", W.PRUEFEN, "http://www.x.de")
    c = W.Proposal("Q3", "c", W.NEU, "https://y.de/")
    assert W.resolve_collisions([a, b, c]) == 2
    assert a.status == b.status == W.GETEILT and c.status == W.NEU and "Q2" in a.note


def test_evidence_whole_name_and_municipal_domain():
    # „Schloss Hof“: kein Wort ≥ 4 Zeichen außer dem allgemeinen „Schloss“ → ganzer Name zählt
    assert W.evidence(["Schloss Hof"], [], ok("x", title="Die faszinierende Welt des Barock | Schloss Hof"),
                      "https://www.schlosshof.at/")[0] == "titel"
    assert W.evidence(["Schloss Hof"], [], ok("x", title="Barock"), "https://www.schlosshof.at/")[0] == "adresse"
    # Domain ist mehr als der Ortsname → keine Gemeindeseite
    assert W.evidence(["Schloss Eckartsau"], ["Eckartsau"], ok("x", title="Schloss Eckartsau"),
                      "https://www.schlosseckartsau.at/")[0] == "titel"
    assert W.municipal_homepage("https://www.stadt-spremberg.de/", ["Spremberg"])
    assert W.municipal_homepage("https://www.bad-muenstereifel.de/", ["Bad Münstereifel"])
    assert not W.municipal_homepage("http://www.museum.krumbach.de/", ["Krumbach (Schwaben)"])
    assert not W.municipal_homepage("https://www.weinsberg.de/stadtarchiv/", ["Weinsberg"])


def test_start_page_with_language_suffix():
    assert W.proposed_value("www.evangelischeskulturzentrum.at", "http://www.evangforumfresach.at/home_de/") == \
        "http://www.evangforumfresach.at/"


def test_output_names_date_first(tmp_path):
    from glamsync import export

    assert export.qs_stem(D, "P856", "Q183", "neu") == f"{D}_P856_Q183_neu"
    assert export.qs_stem(D, "P7704", "", "neu") == f"{D}_P7704_neu"
    assert export.out_path(D, "webseiten_Q183", ".csv", "P856", tmp_path).name == f"{D}_P856_webseiten_Q183.csv"
    assert export.out_path(D, "report", ".csv", out_dir=tmp_path).name == f"{D}_report.csv"
    [p] = export.write_qs_groups([["Q1\tP856\t\"https://x.de/\""]], "P856", "Q183", "neu", D, tmp_path)
    assert p.name == f"{D}_P856_Q183_neu_part01.txt"
