# glamsync – Abgleich von Kulturerbe-Einrichtungen zwischen Europeana, DDB und Wikidata

Repository: <https://github.com/Baum64/Wikidata-Europeana-link>

Das Tool ruft Organisationen (Museen, Bibliotheken, Archive) aus der **Europeana Entity API** und
der **API der Deutschen Digitalen Bibliothek (DDB)** ab und gleicht sie mit **Wikidata** ab.
Für fehlende oder fehlerhafte Identifikatoren erzeugt es **QuickStatements zur manuellen Prüfung**.
Das Tool schreibt **niemals selbst** in Wikidata.

| Property | Name | Format in Wikidata |
|---|---|---|
| P7704 | Europeana entity | `organisation/<n>` (britische Schreibweise), `agent/<n>`, `place/<n>`, `concept/<n>`. Neue Werte ohne `base` (seit 20.11.2026), vorhandene `…/base/<n>` bleiben gültig |
| P791 | ISIL | z. B. `DE-MUS-123456`, `DE-1` (Hauptschlüssel) |
| P227 | GND | z. B. `2022341-9` (Zweitschlüssel) |
| P214 | VIAF | nur übernommen, wenn die Quelle genau einen Wert hat |
| P213 | ISNI | 16 Zeichen **ohne Leerzeichen**, z. B. `0000000123531945` |
| P7917 | Museums in Austria Code | `M` + 5 Ziffern, z. B. `M10072` (nur Registerabgleich) |

> Status (15.11.2026): Erkundung, Abgleich und Export implementiert; Trockenlauf für Deutschland durchgeführt.

## Einrichtung

```bash
git clone https://github.com/Baum64/Wikidata-Europeana-link.git
cd Wikidata-Europeana-link
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt     # alternativ: pip install -e ".[dev]"
cp .env.example .env                # EUROPEANA_API_KEY eintragen
pytest                              # Tests für Normalisierung und Abgleich
```

Voraussetzung: Python ≥ 3.11.

| Variable (`.env`) | Zweck |
|---|---|
| `EUROPEANA_API_KEY` | Pflicht für die Europeana Entity API (`wskey`), erhältlich unter <https://pro.europeana.eu/page/get-api> |
| `DDB_API_KEY` | Für die hier genutzten DDB-Endpunkte **nicht nötig** |
| `GLAMSYNC_CONTACT` | Kontaktangabe im User-Agent, die die Wikimedia-User-Agent-Policy verlangt; Standard ist die Repository-URL |
| `GLAMSYNC_WIKIDATA_CACHE_HOURS` | Lebensdauer des Wikidata-Caches in Stunden (Standard 12) |
| `GND_DUMP_PATH` | lokaler GND-Dump Körperschaften (siehe „GND-Dump Körperschaften“) |
| `GND_REDIRECTS_PATH` | GND-Umlenkungs-/Löschdatei (leer: neueste `authorities-gnd_umlenk_loesch_*.jsonld.gz` neben dem Dump) |

API-Schlüssel werden nur aus Umgebungsvariablen bzw. `.env` gelesen, nie geloggt und nicht in den
Cache geschrieben. `.env` ist über `.gitignore` von Git ausgeschlossen.

Alle HTTP-Antworten werden in `cache/` zwischengespeichert (eine JSON-Datei pro Anfrage).
**Wikidata-Antworten und die Grundmengen unter `data/wikidata_*.json` verfallen nach 12 Stunden**
(`GLAMSYNC_WIKIDATA_CACHE_HOURS`), damit bereits eingespielte QuickStatements nicht erneut
vorgeschlagen werden. Europeana-, DDB- und Registerdaten bleiben unbegrenzt im Cache. Direkt nach
dem Einspielen von QuickStatements einen frischen Stand erzwingen:

```bash
rm -rf cache/query.wikidata.org data/wikidata_*
```

Alternativ `--no-cache` (lädt alles neu).

## Benutzung

```bash
# Trockenlauf: max. 25 Einrichtungen je Quelle, ohne Wikidata-Grundmenge
python -m glamsync compare --country Q183 --limit 25

# Vollständiger Lauf: lädt die Grundmenge je Typ; beim ersten Mal ~5.000 DDB-Einzelabrufe (ca. 20 min)
python -m glamsync compare --country Q183

# QuickStatements aus dem letzten Abgleich schreiben (alle oder gezielt)
python -m glamsync export-qs
python -m glamsync export-qs --property P7704 --action new

# Rohdaten einzeln abrufen und unter data/ ablegen
python -m glamsync fetch --source europeana --country Q183
```

Die Wikidata-Grundmenge umfasst **standardmäßig auch Unterklassen** (P31/P279*, z. B.
Kunstmuseum, Universitätsbibliothek); `--no-subclasses` beschränkt auf direkte P31.
Weitere Optionen: `--types museum,library,archive`, `--sources europeana,ddb`, `--no-cache`, `-v`.

### Ausgabe (`out/`)

| Datei | Inhalt |
|---|---|
| `summary_<datum>.md` | Zählungen: Grundmenge vorher → nachher-Potenzial, Zuordnung, Klassifikation, QS-Umfang |
| `report_<datum>.csv` | eine Zeile je Quell-Einrichtung (Klassifikation je Property, Trefferquelle) plus Zeilen `nur_wikidata` für reine Formatbefunde; Trennzeichen `;` |
| `kandidaten_namen_<datum>.csv` | nicht zugeordnete Einrichtungen mit ähnlich benannten Items, **nur zur Handprüfung** |
| `qs_<P>_{neu,korrektur,loeschen}_<datum>_partNN.txt` | max. 500 Zeilen je Datei; Korrekturpaare (Löschen + Neu) werden nicht getrennt; `loeschen` = ungültige Werte |
| `results_<datum>.json` | vollständige Ergebnisse, Grundlage für `export-qs` |

Beispiel für eine neue Aussage mit Quellenangabe:

```
Q170109|P7704|"organisation/4349"|S248|Q234110|S813|+2026-11-15T00:00:00Z/11
```

## Abgleichslogik

Zuordnung einer Einrichtung zu einem Wikidata-Item, in dieser Reihenfolge:

1. Europeana `sameAs` → Wikidata-QID (Weiterleitungen werden aufgelöst)
2. ISIL (P791), exakt, ohne Beachtung der Groß-/Kleinschreibung
3. GND (P227), exakt
4. Kein automatischer Namensabgleich, ähnliche Namen nur in `kandidaten_namen_<datum>.csv`

Klassifikation je Property:

| Status | Bedeutung | QuickStatement |
|---|---|---|
| `fehlt_in_wikidata` | Quelle hat einen Wert, das Item nicht, und der Wert hängt an keinem anderen Item | neu, mit S248 + S813 |
| `identisch` | stimmt überein | – |
| `formatfehler` | gleicher Wert in falscher Form (URL, Portal-Pfad, ISNI mit Leerzeichen …) | Löschen + Neu, **nur wenn** die alte Aussage keine Quellen/Qualifier hat |
| `konflikt` | Wikidata hat einen anderen Wert | – (nur Bericht) |
| `mehrdeutig` | Wert passt auf mehrere Items, Quelle hat mehrere Werte, oder Wert hängt schon an anderem Item | – (nur Bericht) |

Fundstellen (S248): **Europeana = Q234110**, **Deutsche Digitale Bibliothek = Q621630**
(per SPARQL geprüft). Werte mit dem Rang „deprecated“ werden beim Vergleich ignoriert.

Unabhängig von den Quellen prüft das Tool alle P7704-Werte außer `agent/…` sowie alle
Identifikatoren der beteiligten Items auf Formatfehler. Korrekturen bei
`organisation/…` werden nur vorgeschlagen, wenn es die ID in der Europeana-Organisationsliste
gibt; dann wird Europeana als Fundstelle gesetzt.

**Ungültige Werte** (nicht reparierbares Format, z. B. Objekt-IDs `item/…` in P7704, die zu P727
gehören, Werte ohne Präfix, `DE-MUS` ohne Nummer) werden **zur Löschung vorgeschlagen**
(`qs_<P>_loeschen_<datum>_partNN.txt`, Aktion `delete`), sofern die Aussage weder
Quellen noch Qualifier hat; sonst nur Bericht. Objekt-IDs ggf. vorher nach P727 übertragen.
Dubletten (ein P7704-Wert an mehreren Items) erscheinen nur im Bericht; für sie gibt es auch
keine Formatkorrektur.

## Register ohne Wikidata-Verknüpfung (ISIL-Verzeichnis, Museen in Österreich)

```bash
python -m glamsync registry --source isil --country Q183   # Deutsche ISIL-Agentur → P791 (DE-*)
python -m glamsync registry --source isil --country Q40    # … AT-* (109 Einträge)
python -m glamsync registry --source mure --country Q40    # Museen in Österreich → P7917
```

| Quelle | Schnittstelle | Inhalt | Fundstelle in QS |
|---|---|---|---|
| Deutsche ISIL-Agentur | `https://isil.staatsbibliothek-berlin.de/api/org.jsonld?q=isil=DE-*` (JSON, max. 100 je Seite, CC0) | 17.119 aktive `DE-`-ISIL: Name, Kurzformen, Ort, Website, Koordinaten | S248 = Q28657655 („ISIL Datei und Sigelverzeichnis“) |
| Museen in Österreich | `https://museen-in-oesterreich.at/wp-json/wp/v2/museum` (WordPress-API) | 813 Museen: M-Nummer, Name, Ort, Website, Koordinaten | S854 = URL des Registereintrags (es gibt kein Wikidata-Item für das Register) |

Beide Register enthalten **weder Wikidata-QID noch GND**. Werte, die schon in Wikidata stehen,
werden über den exakten Schlüssel erkannt. Für fehlende Werte gibt es nur Kandidaten:

| Status | Regel | Ausgabe |
|---|---|---|
| `vorhanden` | Wert steht an genau einem Item | – |
| `vorhanden_pruefen` | Wert vorhanden, Name passt kaum (Ähnlichkeit < 0,5) | Bericht |
| `kandidat_stark` | Item **ohne** diesen Wert mit **gleichem Namen UND gleicher Website** | `qs_<P>_kandidaten_<land>_<datum>_partNN.txt` – **nur nach Handprüfung einspielen** |
| `kandidat_schwach` | nur Name gleich, oder Website gleich + Name ähnlich, oder Name ähnlich + < 1 km | nur CSV |
| `konflikt` | starker Kandidat hat schon einen anderen Wert | Bericht |
| `mehrdeutig` | Wert an mehreren Items, oder ein Item ist starker Kandidat für mehrere Einträge | Bericht |

„Gleicher Name“ heißt: nach Normalisierung (Kleinschreibung, ß→ss, &→und, ohne Satzzeichen)
identisch, verglichen werden Name und Kurzformen des Registers mit Labels und Aliassen (de/en)
in Wikidata. Der Hinweis im Bericht zeigt, ob der Hauptname oder eine Kurzform/ein Alias
übereinstimmt. „Gleiche Website“ heißt: ohne Schema, `www.`, Index-Datei und Schluss-Slash
identisch; die Query bleibt (ohne Tracking-Parameter) erhalten.

Kandidaten-Pool sind die Wikidata-Items des Landes mit P31/P279* = Museum/Bibliothek/Archiv
(`--types`, `--no-subclasses`). Einrichtungen außerhalb davon landen als `kein_treffer`.

**Bereinigung vorhandener Wikidata-Werte** (nur Aussagen ohne Quellen/Qualifier, sonst Bericht):

| Fall | QuickStatement |
|---|---|
| Wert hat ungültiges Format | `qs_<P>_loeschen_<land>_<datum>_partNN.txt` |
| Wert fehlt im Register | `qs_<P>_loeschen_pruefen_<land>_<datum>_partNN.txt` – **vor dem Löschen prüfen**: kann eine aufgelöste Einrichtung (historisch korrekter Wert) oder ein Tippfehler sein |

Ausgabe in `out/`: `register_<quelle>_<land>_<datum>.md` (Zusammenfassung), `….csv` (alle
Register-Einträge), `…_bereinigung.csv` (ungültige bzw. im Register fehlende Wikidata-Werte).

Ergebnis 16.11.2026 (Kandidaten-Pool mit Unterklassen):

| | ISIL DE (P791) | Museen in Österreich (P7917) |
|---|---|---|
| Register-Einträge | 17.119 | 813 |
| Kandidaten-Pool (Wikidata) | 17.135 | 1.777 |
| vorhanden / prüfen | 10.869 / 427 | 637 / 23 |
| starke Kandidaten (QS) | 13 | 9 |
| schwache Kandidaten (CSV) | 676 | 54 |
| konflikt / mehrdeutig | 24 / 88 | 1 / 4 |
| ungültige Werte → Löschvorschlag | 1 | 0 |
| nicht im Register → Löschvorschlag (prüfen) / nur Bericht | 9 / 1 | 11 / 84 |

## GND-Dump: Körperschaften ↔ Wikidata (P227)

```bash
python -m glamsync fetch --source gnd                          # Tabelle aus dem Dump erzeugen (~3 min, nur bei neuem Dump)
python -m glamsync registry --source gnd --country Q183 --limit 50   # Trockenlauf
python -m glamsync registry --source gnd --country Q183        # Deutschland (inkl. DDR und Deutsches Reich)
python -m glamsync registry --source gnd --country Q183 --propose-dates   # zusätzlich P580/P582 an P227
python -m glamsync registry --source gnd --country Q183 --include-historical  # auch aufgelöste/frühere Namen
python -m glamsync export-mnm --country Q183                   # Mix'n'match-Dateien (CSV) erneut schreiben
python -m glamsync export-mnm --country Q183 --include-historical
python -m glamsync labels --source gnd --country Q183          # deutsche Labels/Beschreibungen/Aliase (QS)
```

Voraussetzung sind die drei Dumps und `GND_DUMP_PATH` (siehe „GND-Dump Körperschaften“ unter
„Erkundung der Quellen“). `registry --source gnd` erzeugt die Tabelle bei Bedarf selbst.
Für die Prüfung auf Umlenkungen und Löschungen kommt die monatliche Datei dazu:

```bash
curl -o data/gnd/authorities-gnd_umlenk_loesch_20261113.jsonld.gz \
  https://data.dnb.de/opendata/authorities-gnd_umlenk_loesch_20261113.jsonld.gz   # 8,5 MB
```

`GND_REDIRECTS_PATH` zeigt darauf. Ohne Angabe nimmt das Tool die neueste solche Datei neben dem
Dump. Fehlt sie ganz, läuft der Abgleich ohne diese Prüfung, mit Warnung und Hinweis in der
Zusammenfassung.

**Ziel und Umfang.** Gesucht werden Museen, Bibliotheken und Archive aus der GND, deren GND in
Wikidata fehlt. Für P227 entstehen **keine QuickStatements**, denn die Erkundung hat gezeigt:

- Die Wikidata-Links der GND sind Rückverweise auf vorhandene P227-Aussagen. Von 2.506
  deutschen Einrichtungen mit Link fehlte P227 bei keinem einzigen Item, das nicht schon an
  einem anderen Item hing.
- Die GND enthält keine ISIL.

Ergebnis sind daher eine **Kandidatenliste nach Name und Ort** zur Handprüfung und
**Mix'n'match-Dateien**. Für Items, die die GND schon tragen, erzeugt `labels` außerdem deutsche
Labels, Beschreibungen und Aliase (siehe unten).

**Tabelle** (`data/gnd_einrichtungen.sqlite`, Tabelle `body`, eine Zeile je Einrichtung). Der
Dump wird streamend gelesen und auf zwei Bedingungen gefiltert:

- **Typfilter Körperschaft:** mindestens ein `@type` aus `CorporateBody`, `OrganOfCorporateBody`,
  `Company`, `MusicalCorporateBody`, `ReligiousCorporateBody`, `ReligiousAdministrativeUnit`,
  `ProjectOrProgram`, aber kein ausgeschlossener Typ (`FictiveCorporateBody`, Gebietskörperschaft,
  Kongress, Bauwerk, Person, Werk …);
- **Museum/Bibliothek/Archiv:** ein Oberbegriff, der in der Sachbegriff-Hierarchie unter
  Museum, Bibliothek oder Archiv liegt (streng, siehe Erkundung).

Gespeichert werden GND-ID, Name, Varianten und Kurzformen, Typ(en), Oberbegriffe, Land,
Orte (Name, QID, Koordinaten), Homepage, Wikidata/VIAF/ISNI aus `owl:sameAs`,
Gründungs- und Auflösungsdatum, übergeordnete Körperschaft (mit Namen) und umgelenkte alte GND-IDs.
Zum Zeitraum kommen hinzu:

| Spalte | Inhalt |
|---|---|
| `start_year`, `end_year` | genaues Jahr, nur bei genau einem Wert wie `1606`, `15.09.1883` oder `XX.05.1952`; sonst leer |
| `period_raw` | alle Rohwerte, z. B. `1606–1749`, `1619 / 1962–1810`, `ca. 1972-2002` (kombiniertes Feld) |
| `closed` | Ende vorhanden oder Bereich mit Ende im kombinierten Feld |
| `predecessors` | `gndo:precedingCorporateBody` |
| `successors` | Sätze (aller Körperschaften), die diesen als Vorgänger nennen |
| `historical` | abgeschlossen **und** ohne Nachfolger |

Daraus folgt der Zeitraum-Status: `historisch` (aufgelöst) oder `vorgaenger` (frühere Namensform
mit Nachfolgesatz). Ein fehlendes Ende gilt nicht als Beleg, dass die Einrichtung besteht. Bei
geänderten Spalten baut das Tool die Tabelle beim nächsten Lauf neu (`SCHEMA_VERSION`). In
derselben SQLite-Datei steht die Tabelle `redirect` (`old_id`, `new_id`, `kind` = `umgelenkt` |
`geloescht`).
Die DNB liefert Umlaute zerlegt (Unicode NFD, „a“ + Trema). `GndBody` bringt alle Texte in die
übliche Form NFC; das wirkt auch auf eine schon gebaute Tabelle und gespeicherte Ergebnisse.
Defekte Knoten und Sätze werden übersprungen, gezählt und geloggt. Sätze mit falscher Prüfziffer
werden ebenfalls gezählt.

**Land.** Grundlage ist `geographicAreaCode`. Laut GND-Vokabular bedeuten `XA-DE` Deutschland,
`XA-DDDE` „Deutschland (DDR)“ (22.479 Körperschaften) und `XA-DXDE` „Deutschland, Deutsches Reich“
(109.990, bis 1945). `--country Q183` umfasst alle drei, und jedes bekommt eigene
Mix'n'match-Dateien (`DE`, `DDR`, `DR`). Einrichtungen des Deutschen Reichs können heute
außerhalb Deutschlands liegen, z. B. in Königsberg. Bei mehreren Codes gilt die Reihenfolge
DE, DDR, DR. `Q40` umfasst `XA-AT*`, `Q39` umfasst `XA-CH*`. Der Wikidata-Pool für Q183 enthält
auch Items mit P17 = DDR (Q16957); Items mit P17 = Deutsches Reich sind nicht im Pool.

**Normalisierung** (`normalize.py`). GND-IDs werden normalisiert (`…/gnd/5036103-x` → `5036103-X`).
`gnd_checksum_ok` prüft die Prüfziffer nach Modulo 11 mit den Gewichten 2, 3, … von rechts:

- neue IDs: (11 − Summe mod 11) mod 11;
- alte IDs mit Bindestrich: Summe mod 11;
- in beiden Fällen steht 10 für X.

Das Verfahren ist an allen 2,1 Mio. IDs der drei Dumps belegt. Eine falsche Prüfziffer an einer
Wikidata-P227 erscheint nur im Bericht. `normalize_gnd` prüft weiterhin nur das Format, damit
andere Abläufe solche Werte nicht zur Löschung vorschlagen.

**Abgleich** (`glamsync/gnd.py`, ohne Netz, getestet):

| Status | Regel | Ausgabe |
|---|---|---|
| `vorhanden` | GND steht an genau einem Item (P227, ganz Wikidata) | – |
| `vorhanden_alt` | eine umgelenkte alte GND der Einrichtung steht an einem Item | Bericht |
| `kandidat_sicher` | aus stark/schwach: genau ein Item ohne P227, Ort gleich, ein GND-Name ist einem **Label** (nicht Alias) sehr ähnlich (kompakt ≥ 0,95, Ortsname darf fehlen); kein anderer sicherer Kandidat für dasselbe Item | `qs_P227_gnd_<land>_neu_<datum>_partNN.txt`, nicht in Mix'n'match |
| `kandidat_stark` | genau ein Pool-Item mit **gleichem Namen UND gleichem Ort**, ohne P227 | CSV, Handprüfung |
| `konflikt` | der starke Kandidat hat schon eine andere P227 (Vorgänger, Untereinheit, GND-Dublette?) | Bericht |
| `mehrdeutig` | GND an mehreren Items, mehrere starke Kandidaten, oder ein Item ist starker Kandidat für mehrere GND | Bericht |
| `kandidat_schwach` | Name gleich bei anderem/unbekanntem Ort; Name ähnlich (≥ 0,85) bei gleichem Ort; Website gleich bei ähnlichem Name (≥ 0,6) | CSV |
| `kein_treffer` | – | – |
| `historisch` | abgeschlossen, ohne Nachfolger: keine Kandidatensuche | Bericht |
| `vorgaenger` | abgeschlossen, mit Nachfolgesatz: keine Kandidatensuche | Bericht |
| `gnd_umgelenkt` / `gnd_geloescht` | die GND ist nach dem Dump-Stand umgelenkt bzw. gelöscht (Umlenkungsdatei) | Bericht |

`historisch` und `vorgaenger` greifen nur, wenn die GND nicht schon in Wikidata steht. Mit
`--include-historical` werden diese Einrichtungen normal gesucht und exportiert; die
Mix'n'match-Beschreibung nennt dann Zeitraum und „aufgelöst“ bzw. „früherer Name“. Umgelenkte und
gelöschte GND erscheinen nie als Kandidat und nie in Mix'n'match.

- **Gleicher Name:** wie beim Registerabgleich nach Normalisierung identisch. Verglichen
  werden Haupt-, Varianten- und Kurzform der GND, jeweils auch mit angehängtem Ort, mit Labels
  und Aliassen (de/en). Der Name ohne Orts-Klammer (`Stadtarchiv (Oberursel (Taunus))` →
  `Stadtarchiv`) zählt nur zusammen mit gleichem Ort.
- **Gleicher Ort:** Es reicht eine der folgenden Bedingungen:
  1. Die Wikidata-QID des GND-Orts kommt beim Item vor, als P131 (eine oder zwei Stufen),
     P276, P276/P131 oder P159.
  2. Der Ortsname stimmt mit dem Label eines dieser Orte überein.
  3. Die Koordinaten liegen höchstens 10 km vom GND-Ort entfernt.
- **Mutter-Einrichtung:** Hat ein Item als P227 die GND der übergeordneten Körperschaft, ist
  es kein Kandidat. So wird etwa die „Staatsbibliothek zu Berlin. Ostasienabteilung“ nicht der
  Staatsbibliothek (Q170109) zugeordnet.
- **Pool:** Wikidata-Items des Landes mit P31/P279* = Museum, Bibliothek oder Archiv.
- **Formatbefunde:** P227-Werte im Pool mit falschem Format oder falscher Prüfziffer stehen
  nur im Bericht.

**Zeitraum bei vorhandenen GND-IDs.** Für jede Einrichtung mit Status `vorhanden` liest das Tool
P571 und P576 des Items live und füllt im Bericht die Spalten `gnd_zeitraum`, `gnd_historisch`,
`wd_p571`, `wd_p576` und `anmerkung`. Die Anmerkungen stehen nur im Bericht:

| Anmerkung | Regel |
|---|---|
| `historisch_in_wikidata` | GND: aufgelöst (ohne Nachfolger); Item ohne P576 |
| `zeitraum_abweichend` | genaues GND-Gründungsjahr ≠ P571 (nur bei Sätzen ohne Vorgänger) oder genaues Auflösungsjahr ≠ P576 (nur bei aufgelösten) |
| `vorgaenger_gnd` | Item trägt die GND einer früheren Namensform |
| `p576_nur_in_wikidata` | GND ohne Ende, Item mit P576 |

**`--propose-dates`** (Standard aus) schreibt den Zeitraum des GND-Satzes als **Qualifikator an die
P227-Aussage**: P580 (Startzeitpunkt) und P582 (Endzeitpunkt), jeweils mit Jahresgenauigkeit. Die
Angaben beschreiben, wann der GND-Satz bzw. die Namensform galt, und nicht Gründung oder Auflösung
der Einrichtung. Deshalb gibt es keine P571/P576 am Item. Ein Ende in der GND ist meist eine
Umbenennung und als Datum der Einrichtung oft falsch. Auch frühere Namensformen bekommen ihren
Zeitraum. Bedingungen:
- Status `vorhanden` (GND an genau einem Item) und genau eine nicht-deprecated P227-Aussage mit
  diesem Wert;
- das Jahr ist genau (kein „ca.“, kein Jahrhundert, kein `19XX`, kein kombiniertes Feld, nur ein Wert);
- keine Vorschläge bei mehreren Beginn- oder Endwerten. Das sind Wiedereröffnungen, z. B.
  Schlossmuseum Darmstadt `1924 / 1965–1944`;
- keine Jahre in der Zukunft, Beginn nicht nach Ende;
- **keine Dopplung:** Hat die Aussage schon einen P580 bzw. P582 (gleich welcher Wert, auch
  „unbekannter Wert“), wird dieser Qualifikator nicht ergänzt.

QuickStatements ergänzt bei gleichem P227-Wert die vorhandene Aussage und legt keine zweite an.
Eine eigene Fundstelle bekommt der Qualifikator nicht; er stammt aus dem Satz, auf den die
Aussage selbst verweist.

**Umgelenkte und gelöschte P227 in Wikidata.** Geprüft werden alle P227-Werte des Pools und alle
alten IDs, deren Endziel eine Einrichtung des Laufs ist. Ketten werden bis zum Endziel aufgelöst.
Bereits als `deprecated` markierte Aussagen bleiben unberührt.

| Befund | Ausgabe |
|---|---|
| `p227_umgelenkt`, alte Aussage ohne Quellen und Qualifikatoren, Ziel an keinem anderen Item | Korrektur-QuickStatements: alte Aussage entfernen, Ziel mit Quelle ergänzen (falls es fehlt) |
| `p227_umgelenkt`, mit Quellen/Qualifikatoren oder Ziel an anderem Item | nur Bericht (von Hand deprecaten) |
| `p227_geloescht` | nur Bericht |

Aufspaltungen (mehrere Ziele) werden nie automatisch bearbeitet; die Datei enthält derzeit keine.

**Mix'n'match** (`out/mnm_gnd_<typ>_<land>_<datum>.csv`, je Typ und Land). Aufgenommen werden
alle Einrichtungen, deren GND weder aktuell noch als alte ID in Wikidata steht, also auch
Kandidaten, Konflikte und Fälle ohne Treffer. Ohne `--include-historical` fehlen historische
Einrichtungen und frühere Namensformen. Die Datei hat eine Kopfzeile und folgende Spalten:

| Spalte | Inhalt |
|---|---|
| `id` | GND-ID |
| `name` | Hauptname aus der GND |
| `description` | Ort; Oberbegriff (z. B. Kunstmuseum); Zeitraum (`1907–1990`, `gegr. 1907`, `bis 1990`); `DDR` bzw. `Deutsches Reich`; `Teil von:` übergeordnete Körperschaft. Höchstens 255 Zeichen, **ohne ISIL** (nicht in der GND) |
| `url` | `https://d-nb.info/gnd/<id>` (so stimmt der Link auch ohne Verknüpfung des Katalogs mit P227) |
| `type` | Q33506 Museum, Q7075 Bibliothek oder Q166118 Archiv |

Das Format folgt der Mix'n'match-Testdatei: CSV mit Komma, UTF-8, genau eine Kopfzeile.
Felder mit Komma oder Anführungszeichen werden in `"…"` gesetzt. Einrichtungen mit mehreren Typen
stehen in jeder passenden Datei.

**Katalog anlegen** auf <https://mix-n-match.toolforge.org/#/import>:

1. Neuen Katalog angeben. Die Property **P227** eintragen, dann übernimmt die Seite die Angaben
   der Property. Dazu Name, Beschreibung, Sprache `de` und als Standardtyp den Wert aus der
   Spalte `type` (z. B. Q33506). Für einen gemeinsamen Katalog aus mehreren Typen Q43229
   (Organisation) als Standardtyp wählen; jede Zeile behält ihren eigenen `type`.
2. Als Quelle die CSV-Datei hochladen und **Komma** als Trennzeichen wählen.
3. Mit **„Test“** die ersten bis zu 1.000 Zeilen ohne Änderung prüfen. Erst danach
   „Looks good, queue import/update“ wählen.

Weitere optionale Spalten der Importseite: `autoq` (Kandidat als automatische Zuordnung),
`P17`/`P131` und `coord` werden bewusst nicht geschrieben. Die Kandidaten sollen zuerst von Hand
geprüft werden, und die Koordinaten der GND bezeichnen nur den Ortsmittelpunkt.

**Ausgabe:**

- `out/register_gnd_<land>_<datum>.md` – Zusammenfassung mit GND-Zahlen (Dump, Einrichtungen je
  Typ/Land und Anteil mit Wikidata-Link, Status, Mix'n'match-Dateien)
- `out/register_gnd_<land>_<datum>.csv` – Kandidatenliste/Bericht, eine Zeile je Einrichtung
- `out/register_gnd_<land>_<datum>_formatfehler.csv` – P227-Formatbefunde im Pool
- `out/register_gnd_<land>_<datum>_umlenkungen.csv` – P227 auf umgelenkte/gelöschte GND
- `out/qs_P227_gnd_<land>_korrektur_<datum>_partNN.txt` – Korrekturen (je 500 Zeilen)
- `out/qs_P227_gnd_<land>_zeitraum_<datum>_partNN.txt` – Qualifikatoren P580/P582, nur mit `--propose-dates`
- `out/mnm_gnd_<typ>_<land>_<datum>.csv` – Mix'n'match
- `data/gnd_results_<land>_<datum>.json` – Grundlage für `export-mnm`

**Ergebnis 21.11.2026 (Q183, Kandidaten-Pool 17.146 Items).** 10.257 Einrichtungen: 9.788 DE,
61 DDR, 487 Deutsches Reich. Manche haben mehrere Typen.

| Status | Anzahl |
|---|---:|
| vorhanden | 2.830 |
| vorhanden_alt | 2 |
| kandidat_stark | 1.075 (Ort über QID 882, über Entfernung 189, über Namen 4; 27 davon aufgelöst) |
| kandidat_schwach | 1.187 |
| konflikt | 395 (meist Vorgänger oder GND-Dubletten, z. B. UB Bonn bis 1993 ↔ ULB Bonn) |
| mehrdeutig | 139 |
| kein_treffer | 4.629 |

- Eine Zufallsstichprobe von 30 starken Kandidaten war vollständig plausibel. Die Liste ist
  trotzdem nur zur Handprüfung gedacht.
- Im Pool gibt es keine P227-Formatfehler.
- Mix'n'match: 7.466 Zeilen in 9 CSV-Dateien. Größte Dateien: Museum DE 2.996, Bibliothek DE 2.997,
  Archiv DE 957, Bibliothek DR 398.

**Ergebnis 22.11.2026 mit Zeitraum und Umlenkungen** (`--propose-dates`, Umlenkungsdatei vom
13.11.2026):

| Status | DE (Q183) | AT (Q40) | CH (Q39) |
|---|---:|---:|---:|
| vorhanden | 3.538 | 253 | 285 |
| vorhanden_alt | 2 | 0 | 0 |
| kandidat_stark | 638 | 168 | 169 |
| kandidat_schwach | 876 | 119 | 120 |
| konflikt | 179 | 26 | 17 |
| mehrdeutig | 56 | 15 | 11 |
| kein_treffer | 3.668 | 496 | 255 |
| historisch | 383 | 43 | 22 |
| vorgaenger | 915 | 99 | 84 |
| gnd_umgelenkt | 2 | 0 | 0 |
| **Anmerkungen** (Items mit P227) | | | |
| historisch_in_wikidata | 46 | 4 | 4 |
| zeitraum_abweichend | 133 | 5 | 18 |
| vorgaenger_gnd | 243 | 25 | 20 |
| p576_nur_in_wikidata | 28 | 1 | 6 |
| **Vorschläge** | | | |
| P571 | 426 | 27 | 37 |
| P576 | 23 | 2 | 3 |
| P227-Korrekturen | 0 | 0 | 0 |

- Seit dem 21.11. ist `vorhanden` gestiegen (2.830 → 3.538), vor allem durch die Zuordnungen in
  Mix'n'match. Starke Kandidaten sinken von 1.075 auf 638, weil historische Einrichtungen und
  frühere Namensformen nicht mehr gesucht werden.
- **P227 auf umgelenkte GND (DE):** 6 Aussagen. Drei haben eine Quelle, bei dreien steht das Ziel
  an einem anderen Item. Alle sechs stehen daher nur im Bericht.
- Mix'n'match ohne historische Einrichtungen: DE Museum 2.122, Bibliothek 2.188, Archiv 773;
  AT 482/184/163; CH 369/132/72.
- Die Stichprobe von P571 war plausibel. Bei P576 führten Wiedereröffnungen zu Fehlern, z. B.
  Schlossmuseum Darmstadt; die Regel schließt sie jetzt aus.

### Deutsche Labels, Beschreibungen und Aliase aus der GND

`labels --source gnd` liest live alle Items, deren P227 (nicht deprecated) auf eine Einrichtung
der Tabelle zeigt, mit ihrem deutschen und „mul“-Label, der deutschen Beschreibung und den
Aliassen (de/mul). Daraus entstehen QuickStatements, die nur **ergänzen**:

| Art | Bedingung | Wert |
|---|---|---|
| `Lde` | kein de- und kein mul-Label | GND-Name; eine Orts-Klammer wird angehängt: „Stadtarchiv (Kelsterbach)“ → „Stadtarchiv Kelsterbach“ |
| `Dde` | keine de-Beschreibung, GND mit Ort | „Oberbegriff in Ort“, z. B. „Heimatmuseum in Weißenhorn“, „wissenschaftliche Bibliothek in München“; sind alle GND-Sätze des Items aufgelöst: „ehemaliges …“/„ehemalige …“ |
| `Ade` | Name ist weder Label noch schon Alias | Hauptname und Varianten der GND in Label-Form |

- **Nur passende Items:** Die GND muss an genau einem Item stehen, und das Item muss in Wikidata
  ein Museum, eine Bibliothek oder ein Archiv sein (P31/P279*). Für Menschen (Q5), andere Items und
  GND an mehreren Items gibt es nur einen Hinweis im Bericht, weil die P227 dort zu prüfen ist.
- **Oberbegriff:** der erste GND-Oberbegriff, der eine Einrichtung benennt (…museum, …bibliothek,
  …archiv, …bücherei, …sammlung, …galerie, …thek), sonst Museum/Bibliothek/Archiv. Ein führendes
  Adjektiv wird kleingeschrieben.
- **Aliase ohne:** übrige Klammerformen (Zeiträume, Übersetzungen, GND-Zusätze),
  Hierarchieformen „Träger. Einrichtung“, erkennbar fremdsprachige Formen und Kürzel mit bis zu
  3 Zeichen (z. B. „ULB“, „MAK“; nicht eindeutig) sowie Einwortformen ohne Leerzeichen und
  Bindestrich (z. B. „Bistumsarchiv“, „HStAS“; Bindestrich-Namen wie „Städel-Museum“ bleiben).
- **Sprachprüfung der Aliase** (`glamsync/lang.py`, Hunspell): Ein Alias entfällt, wenn er kein
  rein deutsches Wort enthält, aber ein fremdsprachiges („German Historical Museum“, „Muzeum
  Narodowe“, „Galleria di Stato“), oder in nichtlateinischer Schrift steht. Wörter in beiden
  Sprachen („Museum“, „Berlin“), Eigennamen („Folkwang“, „Selma“) und Abkürzungen entscheiden nichts.
  Benötigt `hunspell` mit `de_DE` (und `en_US`/`en_GB`); weitere Sprachen über eine Wortliste und
  Buchstaben außerhalb des deutschen Alphabets.
- **Bereits eingespielte fremdsprachige Aliase entfernen:** `qs_Ade_gnd_Q183_loeschen_<datum>_partNN.txt`
  mit `-Q…<TAB>Ade<TAB>"Alias"` (QuickStatements entfernt genau diesen einen Alias über
  `wbsetaliases remove`); Prüfliste `aliase_fremdsprachig_gnd_Q183_<datum>.csv`.
- **Eindeutigkeit:** Wikidata verlangt Label + Beschreibung je Sprache eindeutig. Ergäbe sich
  dasselbe Paar für mehrere Items im Lauf, entfällt die Beschreibung. Kollisionen mit Items
  außerhalb des Laufs meldet erst QuickStatements.
- Für Werte gibt es keine Fundstellen. Werte mit Anführungszeichen werden ausgelassen.
- Direkt nach dem Einspielen erneut ausführen: `GLAMSYNC_WIKIDATA_CACHE_HOURS=0` setzen.

Ausgabe: `out/qs_{Lde,Dde,Ade}_gnd_<land>_neu_<datum>_partNN.txt` (je 500 Zeilen) und
`out/bezeichnungen_gnd_<land>_<datum>.csv` (Vorschläge und Hinweise je Item).

**Ergebnis 21.11.2026 (Q183):** 3.279 Items tragen eine GND aus der Tabelle. Vorschläge:
8 Labels, 253 Beschreibungen, 4.934 Aliase an 1.893 Items. 195 Hinweise: 178 × „P31 ist kein
Museum/Bibliothek/Archiv“, 17 × GND an mehreren Items.

## Städte: Europeana-Orte ↔ Wikidata (P7704)

Europeana führt auch Orte (`http://data.europeana.eu/place/<n>`), deren `sameAs` auf
Wikidata zeigt. Zunächst werden nur **Großstädte** abgeglichen, standardmäßig in ganz Europa:

```bash
python -m glamsync cities                               # alle europäischen Staaten (Standard)
python -m glamsync cities --country Q183                # nur Deutschland
python -m glamsync cities --country Q183,Q40,Q39 --min-population 50000
```

- **Länder (`europa`):** Alle souveränen Staaten (Q3624078) mit Kontinent Europa (P30 Q46),
  ohne aufgelöste Staaten, zusammen 49. Dazu kommen Niederlande (Q55) und Dänemark (Q35),
  weil deren Städte P17 = Landesteil tragen und nicht das Königreich. Transkontinentale
  Staaten (Russland, Türkei, Kasachstan, Georgien) gehen vollständig ein. Die Länder werden
  einzeln abgefragt; ein Land mit SPARQL-Fehler steht in der Zusammenfassung.
- **Grundmenge:** P31/P279* Großstadt (Q1549591) **oder** Stadt (Q515) mit P1082 ≥ 100.000,
  jeweils ohne aufgelöste Städte (P576, z. B. Altona). Die Klasse allein reicht nicht, Berlin
  hat z. B. kein P31 Großstadt.
- **Zuordnung** nur über sameAs: Jede Stadt wird einzeln über
  `GET /entity/resolve?uri=http://www.wikidata.org/entity/<QID>` aufgelöst. Es gibt keinen
  Namensabgleich.
- **Wertform:** `place/<n>` (seit 20.11.2026). Das ist die aktuelle Europeana-ID
  (`http://data.europeana.eu/place/<n>`, Webseite `…/collections/place/<n>`). `place/base/<n>`
  leitet Europeana nur noch per 301 um, und in die Webseiten-URL kopiert führt es zu einem 404.
  Vorhandene `place/base/<n>`-Werte in Wikidata gelten weiter als gültig (`identisch`, keine
  Korrektur). Der Format-Constraint von P7704 verlangt noch `/base/`, Diskussionsentwurf:
  [docs/P7704_place_ohne_base.md](docs/P7704_place_ohne_base.md). Schalter:
  `normalize.WITH_BASE` (gilt für alle Typen außer Organisationen, auch für Personen).
- **Europeana-Dubletten:** Viele Städte gibt es in Europeana zwei- oder dreimal mit
  identischen Daten. Weil P7704 einwertig ist, gilt:
  1. Führt Wikidata bereits einen der Orte, zählt dieser.
  2. Sonst wird der Ort mit eindeutig den meisten verknüpften Objekten
     (`isAggregatedBy.recordCount`) vorgeschlagen.
  3. Bei Gleichstand ist der Fall `mehrdeutig` und steht nur im Bericht.
- **Orte ohne Objekte:** Hat der vorgeschlagene Ort in Europeana 0 verknüpfte Objekte
  (`isAggregatedBy.recordCount`), bekommt er wie bei den Personen den Status
  `fehlt_ohne_objekte` und landet in einer eigenen QS-Datei (zunächst nicht einspielen).
- **Ausgabe:**
  - `out/staedte_<land|europa>_<datum>.csv` (Bericht mit Länderspalte) und
    `out/staedte_<land|europa>_<datum>.md` (Zusammenfassung mit Tabelle je Land und Dublettenliste);
  - `out/qs_P7704_staedte_<land>_{neu,korrektur,loeschen}_<datum>_partNN.txt`,
    Fundstelle S248 Europeana (Q234110) + S813;
  - `out/qs_P7704_staedte_<land>_ohne_objekte_<datum>_partNN.txt` – Orte ohne Objekte.

**Ergebnis 16.11.2026 (Europa, 51 Länder, 802 Städte):**

| Status | Anzahl |
|---|---:|
| fehlt_in_wikidata → QS (12 Dateien) | 554 |
| kein_treffer | 198 |
| mehrdeutig (Europeana-Dubletten) | 49 |
| identisch (Paris) | 1 |
| konflikt (Graz) | 1 |

- 38 Dubletten wurden nach Objektzahl aufgelöst, z. B. Dresden mit 7.572 zu 0 Objekten.
- `kein_treffer` betrifft fast nur die Türkei (122), Kasachstan (21), Georgien, Kosovo und
  Zypern. Europeana führt diese Orte gar nicht, auch Istanbul und Ankara nicht.
- **Graz (Q13298)** hat in Wikidata `place/base/80869`. Das ist der Europeana-Ort „Innere
  Stadt“ (sameAs Q250880). Graz selbst ist `place/base/80868`.
- München heißt in Europeana fälschlich „Monaco“; sameAs, GND und Koordinaten passen aber.

## Personen: Europeana-Agents ↔ Wikidata (P7704, P227)

```bash
python -m glamsync persons        # alle ~170.000 Europeana-Agents, nur P7704
python -m glamsync persons --gnd  # zusätzlich P227: Items ohne GND suchen, über lobid prüfen (langsam)
```

### Konzept

**Ziel.** Europeana führt rund 170.000 Agents (überwiegend Personen) unter
`http://data.europeana.eu/agent/<n>`. Für Personen in Wikidata werden zwei Werte ergänzt:

- **P7704** `agent/<n>` (aktuelle Europeana-Form, Webseite `…/collections/person/<n>`; vorhandene
  `agent/base/<n>` gelten weiter als gültig), die Verknüpfung zu Europeana;
- **P227 GND**, wenn sie in Wikidata fehlt und Europeana sie nennt.

**Datenlage (geprüft 18.11.2026).**

- Alle Agents der Stichprobe (700) haben im sameAs eine Wikidata-QID, gut die Hälfte (388)
  zusätzlich eine GND.
- Wikidata führt bereits 158.437 `agent/base/…`-Werte. Die Lücke ist also klein im
  Verhältnis, aber in absoluten Zahlen relevant.
- DDB-Personenseiten haben keine eigene Wikidata-Property, sie sind über die GND adressiert.
  Deshalb ist die DDB hier keine eigene Quelle.

**Schlüssel und Reihenfolge.**

1. **sameAs → QID.** Weiterleitungen werden aufgelöst, gelöschte Items werden zu `kein_treffer`.
2. **GND** dient als Gegenprobe: Zeigt die GND des Agents in Wikidata auf ein *anderes* Item,
   ist der Fall `mehrdeutig` und wird nur berichtet. Agents ganz ohne QID werden nur bei
   eindeutigem GND-Treffer (P227) zugeordnet.
3. Es gibt keinen Namensabgleich.

**Klassifikation P7704** (je Wikidata-Item, P7704 ist einwertig).

| Status | Bedeutung | Aktion |
|---|---|---|
| `identisch` | Wikidata führt einen der Agents dieses Items | – |
| `fehlt_in_wikidata` | Mensch (P31 Q5) ohne agent-Wert, Agent frei, GND widerspricht nicht | QS neu |
| `konflikt` | Wikidata führt einen anderen Agent | Bericht |
| `mehrdeutig` | Dublette ohne Favorit, Agent schon an anderem Item, GND zeigt auf anderes Item, sameAs mit mehreren QIDs | Bericht |
| `kein_mensch` | Item ist kein Mensch (Organisation, Gruppe, fiktive Figur …) | Bericht |
| `kein_treffer` | QID existiert nicht | Bericht |
| `fehlt_ohne_objekte` | wie `fehlt_in_wikidata`, aber der Agent hat in Europeana 0 Objekte (`isAggregatedBy.recordCount`) | eigene QS-Datei, zunächst nicht einspielen |
| `abgelehnt_in_wikidata` | derselbe Agent steht am Item schon mit Rang „veraltet“ (z. B. Vermischung, zurückgezogener Identifikator) | Bericht |

Europeana-Dubletten (mehrere Agents → dieselbe QID) werden wie bei den Städten nach den
meisten verknüpften Objekten (`isAggregatedBy.recordCount`) entschieden, bei Gleichstand
nur berichtet.

**P227 GND.** Ein Vorschlag entsteht nur, wenn alle Bedingungen erfüllt sind:

- das Item ist sicher zugeordnet (`identisch` oder `fehlt_in_wikidata`) und ein Mensch;
- in Wikidata fehlt P227;
- Europeana nennt genau eine GND, und diese hängt an keinem anderen Item;
- **lobid-gnd** bestätigt eine aktuelle Personen-GND (`DifferentiatedPerson`), die nicht
  umgelenkt wurde. Ist die GND nicht prüfbar, keine Person oder umgelenkt, erhält der Fall
  den Status `gnd_problem` und steht nur im Bericht.

**Bereinigung (Rückrichtung).** Wikidata-Werte `agent/<n>` bzw. `agent/base/<n>`, deren Nummer in der
Europeana-Liste fehlt, werden einzeln abgefragt:

- **404** → `loeschen_pruefen` (Agent existiert nicht mehr);
- **Weiterleitung auf eine andere Nummer** → Korrektur auf den neuen Wert (Europeana hat
  zusammengeführt).

In beiden Fällen gilt: nur ohne Quellen/Qualifier, sonst nur Bericht.

**Ausgabe.**

- `out/qs_P7704_personen_neu_<datum>_partNN.txt` – nur Agents mit mindestens einem Objekt in Europeana
- `out/qs_P7704_personen_ohne_objekte_<datum>_partNN.txt` – Agents ohne Objekte (`fehlt_ohne_objekte`);
  formal korrekt, die Europeana-Seite zeigt aber keine Treffer – **zunächst nicht einspielen**
- `out/qs_P227_personen_neu_<datum>_partNN.txt`
- `out/qs_P7704_personen_korrektur_<datum>_partNN.txt` und `…_personen_loeschen_pruefen_…`
- `out/personen_<datum>.csv` (nur Abweichungen) und `out/personen_<datum>.md` (Zusammenfassung)

Alle Vorschläge haben die Fundstelle S248 Europeana (Q234110) + S813.

**Aufwand.**

- Europeana: rund 1.700 Seiten à 100, **fest sortiert** (`sort=id asc`). Ohne Sortierung
  liefert das Deep Paging Dubletten und lässt Agents aus (im ersten Lauf nur 131.165
  verschiedene von 170.053). Gecacht wird nur die kompakte Form (id, Label, QIDs, GND).
- Wikidata: Der Bestand wird in Teilabfragen (Form mit/ohne `base`, je nach erster Ziffer der
  Nummer) geladen; Detaildaten werden nur für die Lücke (und mit `--gnd` für die
  P227-Kandidaten) abgerufen. Die P227-Suche ist seit 22.11.2026 standardmäßig aus: Sie ist
  teuer und hat zuletzt keinen Vorschlag ergeben.
- Wartezeit: Je Anfrage wird insgesamt höchstens 40 s auf einen Server gewartet
  (`http.MAX_WAIT`, gilt für 429/5xx/Netzwerkfehler und Retry-After). Danach wird die Anfrage
  übersprungen. Wo eine fehlende Antwort zu falschen Vorschlägen führen würde (Bestand aus
  Wikidata), bricht der Lauf stattdessen ab; bei lobid und je Land im Städteabgleich wird nur
  der eine Eintrag ausgelassen.
- lobid: nur für die GND, die als P227 vorgeschlagen würden.

### Ergebnis (Lauf 19.11.2026)

170.053 Europeana-Agents, 169.915 verschiedene Wikidata-Items, 158.387 vorhandene agent-Werte.

| Status P7704 | Anzahl |
|---|---:|
| identisch | 156.986 |
| fehlt_in_wikidata → QS `…_personen_neu_…` | 7.414 |
| fehlt_ohne_objekte → QS `…_personen_ohne_objekte_…` (zunächst nicht einspielen) | 4.653 |
| kein_mensch | 463 |
| konflikt | 366 |
| mehrdeutig | 105 (85 sameAs mit mehreren QIDs, 18 Agent schon an anderem Item, 2 Dubletten) |
| kein_treffer | 13 |

- **P227: keine Vorschläge.** Jedes Item, für das Europeana eine GND nennt, hat sie in
  Wikidata bereits eine P227 – Europeana hat seine GND-Verweise offenbar aus Wikidata übernommen.
- **Bereinigung:** 1.218 Wikidata-Werte zeigen auf Agents, die Europeana nicht mehr kennt
  (404). 1.215 davon haben Quellen/Qualifier (nur Bericht), 3 stehen in
  `…_personen_loeschen_pruefen_…`. Zusammenführungen (Weiterleitungen) gab es keine.
- **Link-Hinweis:** Der bevorzugte Formatter von P7704 (`https://data.europeana.eu/$1`) funktioniert.
  Die zusätzlichen Formatter `…/en/$1` und `…/fr/$1` liefern für jede ID 404 (Stand 19.11.2026).
- In 194 Vorschlägen hat das Wikidata-Item kein deutsches/englisches Label; die Zuordnung
  beruht wie alle anderen auf sameAs.

## Organisationen europaweit: Europeana ↔ Wikidata (P7704)

```bash
python -m glamsync organisations                  # Stufe 1 (sameAs) + Stufe 2 (Website/Kennung)
python -m glamsync organisations --no-candidates  # nur Stufe 1
```

`compare` arbeitet je Land (englisches Länderlabel, Grundmenge Museum/Bibliothek/Archiv, DDB).
Dabei fehlen Organisationen ohne Land (156), und „Netherlands“/„Kingdom of the Netherlands“ bzw.
„Czechia“/„Czech Republic“ sind getrennte Länder. `organisations` gleicht deshalb **alle**
Europeana-Organisationen ab, unabhängig vom Land und vom Typ, und nur mit Europeana als Quelle.

**Stufe 1 – sameAs → QID** (Weiterleitungen aufgelöst), Befund je Item (P7704 ist einwertig):

| Status | Regel | Ausgabe |
|---|---|---|
| `identisch` | Item führt einen der Werte (auch als `organization/…`) | – |
| `konflikt` | Item führt einen anderen P7704-Wert, auch `agent/…` | Bericht |
| `kein_treffer` | QID existiert nicht | Bericht |
| `kein_organisation` | P31 liegt nicht unter Organisation (Q43229), z. B. Palast, Kirchengebäude, Zeitschrift; oder das Item hat eine Einwohnerzahl (P1082), ist also das Gebiet (Gemeinde, Region) und nicht die Verwaltung | Bericht |
| `mehrdeutig` | Europeana-Dublette ohne Favorit (nach Objekten), Wert an anderem Item, mehrere QIDs | Bericht |
| `abgelehnt_in_wikidata` | derselbe Wert steht am Item mit Rang „veraltet“ | Bericht |
| `fehlt_in_wikidata` | Organisation, **ein Name gleich**, Objekte > 0 | `qs_P7704_organisationen_neu_…` |
| `fehlt_ohne_objekte` | ebenso, aber 0 Objekte in Europeana | `…_ohne_objekte_…` (zunächst nicht einspielen) |
| `name_pruefen` | sonst passend, aber **kein Name gleich** | `…_pruefen_…`, **auskommentiert** |

„Name gleich“ heißt: nach Normalisierung (Kleinschreibung, ohne diakritische Zeichen und
Satzzeichen, ß→ss, &→and) identisch. Verglichen werden alle Europeana-Namen (prefLabel aller
Sprachen, altLabel, Akronyme) mit allen Labels und Aliassen des Items in allen Sprachen. Namen
unter 4 Zeichen zählen nicht. Typische Fälle für `name_pruefen`: Europeana verknüpft eine
Untereinheit mit der Mutter („SKD. Gemäldegalerie Alte Meister“ → SKD), ein Museum mit seinem
Gebäude oder ganz falsch („American School of Classical Studies at Athens“ → Sardinien).

**Stufe 2 – Organisationen ohne sameAs** (und ohne Wert in Wikidata). Gesucht wird über:

- die **Website**: Europeana-`homepage` gegen P856, in allen Formen mit http/https, mit/ohne
  `www.` und Schluss-Slash; außerdem nur die Domain (schwächer, oft die Mutter-Organisation);
- die Kennungen **GND, VIAF, ISNI** aus dem sameAs.

Kandidaten sind nur Organisationen (wie in Stufe 1, also auch ohne Einwohnerzahl), die nicht
schon sameAs-Ziel einer anderen Europeana-Organisation sind.

| Status | Regel | Ausgabe |
|---|---|---|
| `kandidat_stark` | genau ein Item mit gleicher Website bzw. Kennung **und** gleichem Namen | `…_kandidaten_…` |
| `kandidat_schwach` | genau ein Item: Website/Kennung + ähnlicher Name (≥ 0,85) oder Domain + gleicher Name | `…_kandidaten_…`, **auskommentiert** |
| `konflikt` | Kandidat führt schon einen anderen P7704-Wert (Europeana-Dublette?) | Bericht |
| `mehrdeutig` | mehrere Kandidaten, oder ein Item ist Kandidat für mehrere Organisationen | Bericht |
| `identisch` | Wikidata führt den Wert, Europeana hat keinen sameAs (Rückmeldung an Europeana) | Bericht |

Kandidaten von Organisationen ohne Objekte stehen in `…_kandidaten_ohne_objekte_…`.

**Auskommentierte Zeilen.** In `…_pruefen_…` und für schwache Kandidaten steht je Vorschlag eine
Hinweiszeile und darunter der Befehl, beide mit `#`:

```
# organisation/5120 „Moldova Iași National Museum Complex - Art Museum“ ↔ Q18540862 „Muzeul de Artă Iași“ – kein Name gleich (Ähnlichkeit 0.39)
#Q18540862|P7704|"organisation/5120"|S248|Q234110|S813|+2026-11-27T00:00:00Z/11
```

Geprüfte Befehle entkommentieren, dann die übrigen `#`-Zeilen entfernen und erst danach
einspielen, z. B. `grep -v '^#' datei.txt > datei_geprueft.txt`. QuickStatements dokumentiert
`#`-Zeilen im V1-Format nicht als Kommentar.

**Formatprüfung.** Vorhandene Werte `organization/<n>` werden auf `organisation/<n>` korrigiert
(`…_korrektur_…`, Löschen + Neu, siehe „Befunde zu Wikidata“), sofern die Aussage weder Quellen
noch Qualifikatoren hat und die ID in der Europeana-Liste steht. Ungültige Werte ohne Quellen und
Qualifikatoren kommen nach `…_loeschen_…`. Dubletten (ein Wert an mehreren Items) erscheinen nur im
Bericht, auch ihre Korrektur (z. B. `organization/4648` an zwei Items „Bernhard-Heiliger-Stiftung“).

**Klassenprüfung.** Ob eine Klasse unter Q43229 liegt, wird mit `P279*` in kleinen Paketen
abgefragt, mit `hint:gearing "forward"` (rund 0,2 s je Klasse). Das Ergebnis speichert das Tool dauerhaft in
`data/klassen_organisation.json`. Zum Erneuern die Datei löschen.

**Drosselung.** Der Lauf stellt einige hundert SPARQL-Abfragen. Verlangt der Query-Service eine
längere Pause (HTTP 429 mit Retry-After), wartet `organisations` sie ab (höchstens 300 s,
zwei Wiederholungen). Eine fehlende Antwort könnte sonst einen Kandidaten fälschlich eindeutig
machen. Die anderen Kommandos brechen weiterhin nach 40 s ab.

**Ausgabe:** `out/organisationen_<datum>.csv` (eine Zeile je Befund, Spalte `datei` nennt die
QS-Datei, `(#)` = auskommentiert), `out/organisationen_<datum>.md` (Zusammenfassung mit
Tabelle nach Land), `out/qs_P7704_organisationen_{neu,ohne_objekte,pruefen,kandidaten,kandidaten_ohne_objekte,korrektur,loeschen}_<datum>_partNN.txt`.
Fundstelle aller Vorschläge: S248 Europeana (Q234110) + S813.

**Ergebnis 27.11.2026** (Europeana-Stand 21.11.2026: 5.587 Organisationen, 1.042 mit sameAs;
296 `organisation/…`-Werte in Wikidata):

| Stufe 1 (sameAs) | Anzahl | | Stufe 2 (ohne sameAs, 4.425 durchsucht) | Anzahl |
|---|---:|---|---|---:|
| fehlt_in_wikidata → `neu` | 504 | | kandidat_stark → `kandidaten` | 718 |
| fehlt_ohne_objekte → `ohne_objekte` | 178 | | kandidat_stark → `kandidaten_ohne_objekte` | 186 |
| name_pruefen → `pruefen` (#) | 138 | | kandidat_schwach (#), mit / ohne Objekte | 279 / 79 |
| identisch | 165 | | identisch (Wert in Wikidata, Europeana ohne sameAs) | 119 |
| kein_organisation | 43 | | mehrdeutig | 36 |
| mehrdeutig / kein_treffer / konflikt | 8 / 4 / 1 | | kein_treffer | 3.128 |

- **Formatprüfung:** 101 Korrekturen `organization/` → `organisation/` (`korrektur`), 10 Formatfehler
  nur im Bericht, 1 Dublette (`organisation/4648`, 2 Items).
- Deutschland und die Schweiz haben in Stufe 1 keine Lücke mehr (früherer `compare`-Lauf). In
  Stufe 2 kommen für DE aber 81 starke Kandidaten hinzu. Die meisten Stufe-1-Vorschläge betreffen
  Rumänien (59 + 56 zu prüfen), die USA, Schweden und die Niederlande.
- Die übrigen Normdaten im Europeana-sameAs (LoC, BnF, IdRef, ULAN, BIBSYS, GeoNames, Freebase)
  stehen bei allen zugeordneten Items schon in Wikidata; Europeana hat sie offenbar von dort
  übernommen. VIAF und ISNI fehlen nur vereinzelt (14 bzw. 3) und werden nicht vorgeschlagen.
- Stichproben: Die Stufe-1-Vorschläge und die starken Kandidaten waren plausibel. Unter den
  schwachen Kandidaten sind viele Untereinheiten, die auf die Mutter zeigen (z. B. „Biological
  Museum, Lund University“ → Universität Lund). Sie sind deshalb auskommentiert.
- Gebiete statt Verwaltungen (Venetien, Nordholland, Gemeinde Ommen …) fielen erst über die
  Einwohnerzahl auf, denn Gemeinden liegen in Wikidata oft auch unter Organisation.

## Erkundung der Quellen

```bash
python -m explore.explore_ddb          # DDB: wo stehen ISIL und GND?
python -m explore.explore_europeana    # Europeana: Umfang, sameAs-Abdeckung, ISIL?
python -m explore.explore_gnd          # GND-Dump Körperschaften: Felder, Typen, Wikidata-Lage
```

### GND-Dump Körperschaften (Stand 21.11.2026)

**Bezug.** Die DNB stellt Gesamtabzüge unter <https://data.dnb.de/opendata/> bereit (CC0). Benötigt
werden drei Dateien, die nicht ins Repository gehören (`data/` ist über `.gitignore` ausgeschlossen):

```bash
mkdir -p data/gnd && cd data/gnd
D=https://data.dnb.de/opendata
curl -O $D/authorities-gnd-koerperschaft_lds_20261013.jsonld.gz   # 211 MB, 1.583.228 Datensätze
curl -O $D/authorities-gnd-sachbegriff_lds_20261013.jsonld.gz     #  25 MB, für den Körperschaftstyp
curl -O $D/authorities-gnd-geografikum_lds_20261013.jsonld.gz     #  44 MB, für Ortsnamen
```

`GND_DUMP_PATH` (in `.env`) zeigt auf den Körperschafts-Dump. Die beiden anderen Dumps sucht das
Tool im selben Ordner. Die Dateien ohne Datum (`…_lds.jsonld.gz`) sind stabile Links auf den
jeweils aktuellen Stand. Der Abzug erscheint etwa zweimonatlich, der hier verwendete hat den
Stand 13.10.2026.

**Format.** Expandiertes JSON-LD als **ein einziges, eingerücktes JSON-Array** (`[ [ {…}, {…} ] ]`),
nicht zeilenweise. Es besteht aus flachen Knoten, die nach `@id` sortiert sind:

| Knoten | Anzahl | Inhalt |
|---|---:|---|
| `https://d-nb.info/gnd/<id>` | 1.583.228 | der Datensatz |
| `https://d-nb.info/gnd/<id>/about` | 1.583.228 | Verwaltungsdaten (Erfasser als ZDB-ISIL-URI, Datum, Lizenz) |
| `_:node…` (leere Knoten) | 26.996 | Beziehungen (Gründer, beteiligte Person …), stehen gesammelt am Dateianfang |

Das Skript liest die Knoten einzeln mit `json.JSONDecoder.raw_decode` aus einem Puffer, nur mit der
Standardbibliothek. Ein Durchlauf dauert rund 2,5 Minuten. Der Dump enthält keine defekten Knoten,
der Parser würde sie aber überspringen und zählen.

**Felder** (Prädikate im Namensraum `gndo:` = `https://d-nb.info/standards/elementset/gnd#`):

| Feld | Quelle im Dump | alle 1.583.228 | DE Museum (4.560) | DE Bibliothek (3.356) | DE Archiv (1.882) |
|---|---|---:|---:|---:|---:|
| GND-ID | `gndo:gndIdentifier` (= Ende von `@id`, immer gültig) | 100 % | 100 % | 100 % | 100 % |
| Name | `gndo:preferredNameForTheCorporateBody` | 100 % | 100 % | 100 % | 100 % |
| Ort | `gndo:placeOfBusiness`: **nur GND-URI eines Geografikums**, Name aus dem Geografikum-Dump | 61,7 % | 99,7 % | 98,4 % | 97,9 % |
| Land | `gndo:geographicAreaCode` (`XA-DE`, `XA-DE-BY`, `XA-AT`, `XA-CH`, `XA-DDDE` = DDR, `XA-DXDE` = Deutsches Reich …) | 93,3 % | 100 % | 100 % | 100 % |
| Körperschaftstyp | `gndo:broaderTermInstantial` → Sachbegriff (siehe unten) | 20,0 % | 100 %¹ | 100 %¹ | 100 %¹ |
| Sachgruppe | `gndo:gndSubjectCategory` (z. B. `6.7`, `6.8`) | 30,3 % | 90,5 % | 66,4 % | 88,8 % |
| **ISIL** | **kommt nicht vor** | 0 | 0 | 0 | 0 |
| Wikidata | `owl:sameAs` `http://www.wikidata.org/entity/Q…` (nie mehr als eine) | 4,5 % | 29,8 % | 9,4 % | 45,4 % |
| VIAF | `owl:sameAs` `http://viaf.org/viaf/…` | 96,1 % | 96,6 % | 95,7 % | 96,0 % |
| ISNI | `owl:sameAs` `https://isni.org/isni/…` (ohne Leerzeichen) | 16,0 % | 24,0 % | 10,3 % | 21,7 % |
| aufgelöst | `gndo:dateOfTermination` | 7,8 % | 12,2 % | 22,0 % | 8,6 % |
| Homepage | `gndo:homepage` | 16,5 % | 38,9 % | 16,8 % | 27,6 % |
| übergeordnete Körperschaft | `gndo:hierarchicalSuperiorOfTheCorporateBody` | 27,4 % | 22,0 % | 39,7 % | 28,7 % |
| alte GND-URIs | `dnb:deprecatedUri` (zusammengeführte Sätze) | 10,5 % | 44,2 % | 24,7 % | 35,0 % |

¹ Die Teilmengen sind über diesen Oberbegriff definiert.

Fehlende bzw. nicht direkt vorhandene Felder:

- **ISIL:** Weder ein Prädikat noch ein Wert. Die ZDB-Organisations-URIs (`…/organisations/DE-1`)
  stehen nur im `/about`-Knoten und bezeichnen die **erfassende** Einrichtung. ISIL-förmige
  Literale (1.150) sind Namensteile wie `DG-Bank`. Auch lobid-gnd hat kein ISIL-Feld. Ob der
  MARC-Datensatz eine ISIL enthält, ließ sich nicht prüfen: Die DNB bietet keine MARC-Dumps mehr
  an, und die SRU-Schnittstelle antwortet mit HTTP 403.
- **Ortsname:** Der Dump enthält nur die GND-URI des Ortes. Alle Orte der Teilmengen lassen sich
  über den Geografikum-Dump auflösen (100 %).
- **Körperschaftstyp:** `@type` unterscheidet nur grob (`CorporateBody` 81 %, `OrganOfCorporateBody`,
  `Company`, `MusicalCorporateBody` …), Museum, Bibliothek und Archiv kommen darin nicht vor.

**Körperschaftstyp Museum/Bibliothek/Archiv.** Er ergibt sich über den Oberbegriff
(`broaderTermInstantial`), der auf einen Sachbegriff zeigt. Dessen Hierarchie
(`broaderTermGeneral`/`broaderTermGeneric`) führt zu einer der drei Wurzeln, die per exaktem Namen
im Sachbegriff-Dump gefunden werden: **Museum 4040795-0** (83 Sachbegriffe mit Unterbegriffen),
**Bibliothek 4006439-6** (84) und **Archiv 4002859-8** (50). Kein Sachbegriff liegt unter
mehreren Wurzeln. 180 Körperschaften haben Oberbegriffe aus mehreren Wurzeln, z. B.
„Stadtbibliothek Bielefeld“ (Bibliothek + Archiv).

| | DE | AT | CH | alle Länder |
|---|---:|---:|---:|---:|
| Museum | 4.560 | 738 | 603 | 13.156 |
| Bibliothek | 3.356 | 272 | 220 | 7.758 |
| Archiv | 1.882 | 223 | 145 | 3.926 |
| davon mit Wikidata-Link (AT/CH) | | 218 | 228 | |

Häufigste Oberbegriffe in DE: Museum (Museum 924, Heimatmuseum 857, Kunstmuseum 787, Technisches
Museum 291), Bibliothek (Leihbücherei 780, Öffentliche Bibliothek 633, Spezialbibliothek 241,
Klosterbibliothek 214), Archiv (Stadtarchiv 817, Archiv 256, Staatsarchiv 158, Kreisarchiv 104).

Gegenprobe DE: Die Sachgruppen 6.7 „Bibliothek. Information und Dokumentation“ und 6.8
„Archiv. Museum“ umfassen 9.758 Körperschaften, davon 7.515 auch über die Hierarchie. Der Rest
sind überwiegend Fördervereine, Verbände, Gedenkstätten und „Sammlung“ (146), außerdem 1.165 ohne
Oberbegriff. Über den Namen (museum|bibliothek|bücherei|archiv) kommen 17.468 Treffer zusammen,
davon nur 7.406 auch über die Hierarchie. Die übrigen sind vor allem Untereinheiten und Vereine,
etwa „Kreisverein für das Museum in Rendsburg“. Es gibt aber auch echte Einrichtungen ohne
Oberbegriff, z. B. „Universitätsarchiv (Tübingen)“. Der Typfilter über die Hierarchie ist daher
streng, verliert aber einen Teil der Einrichtungen.

**Wikidata-Lage (SPARQL, 21.11.2026).**

- **Fundstelle für P227:** `wd:P227 wdt:P9073` (anwendbarer „angegeben in“-Wert) =
  **Q36578 „Gemeinsame Normdatei“**. P1629 verweist dagegen auf Q54506313 „GND-Kennung“, also nicht
  auf die Datenbank. An P227 von Museen/Bibliotheken/Archiven in DE ist Q36578 die
  zweithäufigste S248-Angabe (82). Am häufigsten ist VIAF (336).
- **Alle 2.506 DE-Einrichtungen mit Wikidata-Link** (2.471 verschiedene QIDs, d. h. 35 QIDs
  tragen mehrere GND-Sätze) wurden geprüft:

  | Lage in Wikidata | Anzahl |
  |---|---:|
  | P227 identisch | 2.468 |
  | P227 fehlt | 2 (beide GND hängen schon an einem anderen Item → `mehrdeutig`) |
  | P227 anderer Wert | 1 (Q19963734 → `konflikt`) |

  **Die Wikidata-Links der GND sind Rückverweise auf vorhandene P227-Aussagen.** Fall 1 („GND
  nennt QID, P227 fehlt“) liefert deshalb für Deutschland keinen einzigen sicheren Vorschlag.
  Bei den Europeana-Personen war das ebenso.

**Mix'n'match-Import** (<https://meta.wikimedia.org/wiki/Mix%27n%27match/Import>, Stand 17.03.2026):
CSV oder TSV mit **genau einer Kopfzeile**. Die Spaltennamen werden ohne Beachtung der
Groß-/Kleinschreibung erkannt, eine Reihenfolge ist nicht vorgeschrieben.

| Spalte | Bedeutung |
|---|---|
| `id` | Pflicht: ID im Katalog (hier GND-ID) |
| `name` | Pflicht |
| `desc` / `description` | höchstens 255 Zeichen |
| `url` | optional, sonst aus dem Formatter (P1630) der Property + `id` |
| `type` | `Q…`, überschreibt den Standardtyp des Katalogs je Zeile (z. B. Q33506 Museum) |
| `born`, `died` | `YYYY`, `YYYY-MM` oder `YYYY-MM-DD` |
| `autoq` / `q` | `Q…` als automatische bzw. manuelle Zuordnung |
| `P…` | Property-Wert (Text oder Item; `P625` als `lat/lon`) |

Andere Spalten werden ignoriert. Aliasse sind nicht vorgesehen. „Test“ verarbeitet bis zu
1.000 Zeilen, ohne etwas zu ändern.

#### Zeitraum, Umlenkungen und Löschungen (erkundet 22.11.2026)

**Stand der Dateien.** Körperschafts-Dump 13.10.2026 (Dateiname; die jüngsten
`dcterms:modified` im Dump stammen aus Oktober 2026). Umlenkungs-/Löschdatei
`authorities-gnd_umlenk_loesch_20261113.jsonld.gz` (Stand 13.11.2026, 8,5 MB), also einen Monat
jünger. Auf <https://data.dnb.de/opendata/> gibt es dazu den stabilen Link
`authorities-gnd_umlenk_loesch.jsonld.gz`.

**Zeitangaben im Dump.** Es gibt vier Felder, alle als einfache Literale ohne Datentyp:

| Feld (`gndo:`) | Sätze (alle Körperschaften) | Formate |
|---|---:|---|
| `dateOfEstablishment` | 341.387 | `1606` (289.144), `15.09.1883` (42.318), `XX.05.1952`/`XX.XX.1902` (13.251), `192X`/`19XX` (1.098), drei- und weniger stellige Jahre (`529`), vereinzelt `13. Jh.` |
| `dateOfTermination` | 123.737 | wie oben; vereinzelt `1984,20.Nov.`, `1944-45` |
| `dateOfEstablishmentAndTermination` | 9.958 | Freitext: `ca. 1972-2002`, `ca. 1550-`, `ca. 1820 - vor 1897`, `16XX`, `19. Jh.`, `2. H. 19. Jh.`, `1939/40`, oft nur ein Jahr (`1975`), bei dem offen bleibt, ob Beginn oder Ende |
| `associatedDate` | 78 | Freitext, selten |

- Einige Sätze haben mehrere Werte, bei den 24.642 Einrichtungen der Tabelle 123 Sätze (z. B.
  Beginn `1619` und `1962` bei Neugründung).
- Ein Aktiv-Kennzeichen gibt es nicht. Ein fehlendes Ende beweist nicht, dass die Einrichtung
  besteht.
- Beispiel Gottorfer Hofbibliothek (`7659780-5`): `dateOfEstablishment` `1606`,
  `dateOfTermination` `1749`. Den Freitext dazu enthält `biographicalOrHistoricalInformation`.
- Die Tabelle speicherte bisher nur den ersten Wert von Beginn und Ende als Text; das kombinierte
  Feld fehlte.

**Einrichtungen der Tabelle (Museen, Bibliotheken, Archive):**

| Land | Einrichtungen | mit Beginn | abgeschlossen | Ende als genaues Jahr | nur kombiniertes Feld |
|---|---:|---:|---:|---:|---:|
| DE | 9.710 | 3.852 | 1.449 | 1.397 | 69 |
| DDR | 61 | 13 | 12 | 12 | 0 |
| DR | 486 | 237 | 151 | 142 | 17 |
| AT | 1.219 | 431 | 171 | 162 | 2 |
| CH | 963 | 433 | 130 | 128 | 4 |

„Abgeschlossen“ heißt: `dateOfTermination` vorhanden oder ein Bereich mit Ende im kombinierten
Feld. Die Zahlen für DE weichen leicht von früheren ab (9.788), weil Einrichtungen mit mehreren
Ländercodes hier nur einmal zählen.

**Ende heißt oft Umbenennung, nicht Auflösung.** Die GND legt bei Umbenennung oder Umbildung
einen **neuen Satz** an und beendet den alten. Der neue Satz verweist mit
`gndo:precedingCorporateBody` auf den alten; ein Feld für den Nachfolger gibt es im Dump nicht.
Von 1.913 abgeschlossenen Einrichtungen (DE, DDR, DR, AT, CH) haben **1.083 einen Nachfolger in
der Tabelle**. Echte Auflösungen wie die Gottorfer Hofbibliothek (kein Nachfolger) sind die
Minderheit. Entsprechend beginnt ein Nachfolgesatz oft erst mit dem neuen Namen. Das GND-Gründungsjahr
weicht deshalb bei 872 der Items mit P227 von P571 ab, z. B. Q449280 (P571 1779, GND 2001).

**Abgleich mit Wikidata (live, Items mit P227 aus der Tabelle, 6.719 Items):**

| Befund | Items |
|---|---:|
| GND abgeschlossen (DE, DDR, DR, AT, CH) | 328 |
| davon P576 (Auflösungsdatum) vorhanden | 65 |
| davon P576 fehlt | 280 (Nachfolger in der GND: 197; ohne Nachfolger: 83) |
| GND offen, aber P576 in Wikidata | 64 |
| Ende als genaues Jahr weicht von P576 ab | 22 |
| Beginn als genaues Jahr weicht von P571 ab | 872 (meist Umbenennung, siehe oben) |
| Beginn als genaues Jahr, P571 fehlt | 1.028 |

**Umlenkungs-/Löschdatei.** Es handelt sich um JSON-LD als Array, meist ein Objekt je Zeile.
Gelöschte Sätze stehen aber teils mit dem nächsten Objekt in derselben Zeile; die Datei muss daher
wie der Dump mit `raw_decode` gelesen werden.

| Art | Anzahl | Aufbau |
|---|---:|---|
| Umlenkung | 611.719 | `@id` alte GND, `owl:sameAs` und `dnb:canonicalUri` (gleicher Wert) = neue GND |
| Löschung | 32.287 | `@id` alte GND, `@type` `dnb:DeletedResource`, kein Ziel |

- Die Datei enthält keinen Entitätstyp, kein Datum und keine Art der Umlenkung.
- Jede Umlenkung hat **genau ein Ziel**. Aufspaltungen kommen nicht vor.
- 6.021 Umlenkungen zeigen auf ein Ziel, das selbst umgelenkt ist. Es gibt also Ketten, und das
  Endziel muss aufgelöst werden. 172 Ziele sind gelöscht.
- Gegen den Dump: Die alten IDs aus `dnb:deprecatedUri` (15.751 bei der Tabelle) stehen fast alle
  (15.748) auch in der Datei. **6 GND der Tabelle** sind nach dem Dump-Stand umgelenkt worden,
  keine gelöscht.

**P227 in Wikidata auf alte IDs** (live, alte IDs mit Endziel in der Tabelle, 15.764 Werte):

- 54 Aussagen an 53 Items.
- 50 davon sind schon `deprecated`, mit Qualifikator, und die neue GND steht am selben Item.
- **4 Aussagen mit normalem Rang:** Das ist der eigentliche Korrekturbestand.
- Auf gelöschte IDs zeigt keine P227 mit bestem Rang.

### DDB (Stand 15.11.2026)

**Authentifizierung:** `GET /institutions`, `/items/…` und `/search/index/…` funktionieren
**ohne Schlüssel**. Laut `openapi.yaml` brauchen nur Favoriten, gespeicherte Suchen und die
User-API eine Authentifizierung (Bearer/JWT); `/version/test-auth` liefert ohne Schlüssel 401.

| Endpunkt | Inhalt | ISIL | GND |
|---|---|---|---|
| `GET /institutions` | Baum (3.855 Wurzeln, 5.023 Knoten, Tiefe ≤ 2); `id`, `name`, `sector`, Koordinaten, `numberOfItems` | – | – |
| `GET /search/index/organization/select` (Solr) | 151.004 Dokumente: **5.008 `ddb-institution`**, 15 `ddb-aggregator`, 145.981 `gnd-organization` (GND-Normdaten, keine DDB-Teilnehmer) | – | **`variant_id`** = `http://d-nb.info/gnd/<GND>` (1.652 der 5.008 Institutionen) |
| `GET /items/{institutions-id}` | `provider-info`, `edm.RDF.Organization`, eingebettete View | **`provider-info.provider-isil`** bzw. `edm.RDF.Organization.sameAs` als `http://ld.zdb-services.de/resource/organisations/<ISIL>` | in `view…fields` (`variant_id`) |
| `GET /items/{id}/view` | `cortex-institution.isil` und `europeana-name` | in der Praxis immer `null` | über `fields` |
| `GET /items/{objekt-id}` | `provider-info.provider-isil`, `provider-ddb-id`; `aggregator-info` | ISIL des **datengebenden** Teils (kann eine Untereinrichtung sein) | – |

- Die GND kommt für alle Institutionen in einem Massenabruf aus Solr. Die ISIL gibt es nur
  über Einzelabrufe `/items/{id}` (rund 5.000 Anfragen, danach im Cache).
- Stichprobe von 200 Institutionen: 59 mit ISIL (30 %), 73 mit GND (37 %), 37 mit beidem, 105 ohne beides.
- `sameAs` von DDB-Institutionen enthält nur ISIL-Links (ZDB), **keine Wikidata-QID**.
- `AggregatorInfo` beschreibt den **Aggregator** eines Objekts (z. B. digiCULT), nicht die
  Institution; `aggregator-isil` und `aggregator-europeana-id` waren in allen Stichproben leer.
  Als Brücke DDB ↔ Europeana taugt das Feld nicht.
- Objekt-ISILs gehören zur `provider-ddb-id`, die eine Untereinrichtung sein kann
  (Beispiel: Objekte der SKD tragen `DE-MUS-845516`).

### Europeana (Stand 15.11.2026)

- **5.579 Organisationen** über `GET /entity/search?query=*&type=organization`
  (Paginierung mit `page`/`pageSize` ≤ 100). IDs haben die Form
  `http://data.europeana.eu/organization/<n>`, für P7704 ergibt das `organisation/<n>`.
- Länder: Norwegen 536, **Deutschland 487**, Spanien 436 …; bei 147 fehlt das Land.
- **Wikidata-`sameAs`: 1.040 (18,6 %)**, für Deutschland 147 von 487 (30,2 %);
  10 Organisationen haben mehr als eine QID.
- Weitere `sameAs`-Ziele (alle / DE): VIAF 807 / 129, GND 468 / 127, ISNI 268 / 30.
- **ISIL kommt in Europeana nicht vor.**
- `sameAs` enthält auch **alte, lange Europeana-IDs** (`organization/1482250000…`); solche
  P7704-Werte werden als veraltet gemeldet.

### Denkmalatlas Niedersachsen (Stand 25.11.2026, nicht weiterverfolgt)

Geprüft wurde, ob sich für Denkmäler P7900, P625, P131, P6375 und P31 aus dem
[Denkmalatlas](https://denkmalatlas.niedersachsen.de/viewer/) des NLD ergänzen lassen. Das
Erkundungsskript wurde danach wieder entfernt, weil der Nutzen zu klein war.

- **Schnittstellen** (Goobi viewer, ohne Schlüssel):
  - OAI-PMH `…/viewer/oai`: nur `oai_dc` mit eigenen Feldern, 122.896 Datensätze, 100 je Seite.
    Abfrage nur ohne `set`, denn die Sets sind leer.
  - Vollständiger Datensatz als DenkXweb-XML über `…/viewer/metsresolver?id=<uuid>`.
  - IIIF-Manifest unter `…/viewer/rest/iiif/manifests/<uuid>/manifest/`.
  - Koordinaten in EPSG:25832.
- **Lizenz:** Laut Nutzungsbedingungen stehen die Metadaten unter CC0 1.0. Bilder und Texte
  (Beschreibung, Denkmalbegründung) stehen unter CC BY-SA 4.0. Das Feld `<licence>` im
  DenkXweb-Datensatz nennt trotzdem CC BY-SA 4.0; vor einem Import wäre das beim NLD zu klären.
- **P7900:** Die Property enthält die ADABweb-ID (Format `[0-9]+`). Rund 26 % der Datensätze haben
  keine ADABweb-ID. 9.768 Items haben P7900, das sind nur rund 4 % der Datensätze mit
  ADABweb-ID. In Wikidata stehen 78 Werte an mehreren Items, 27 Werte haben eine falsche Form.
- **Stichprobe von 100 Items mit P7900:**
  - P625 fehlt bei 4 %. Vorhandene Koordinaten weichen im Median 4 m ab.
  - P131 und P31 sind fast immer vorhanden.
  - Nur die Adresse (P6375) fehlt häufiger: bei 43 %.
- **Grund für den Abbruch:** Bei den vorhandenen Items gibt es kaum etwas zu ergänzen. Neue Items
  für über 100.000 Denkmäler wären ein eigenes Bot-Vorhaben. Bei Bodendenkmälern wären exakte
  Koordinaten zudem heikel.

### Folgerungen

1. **Europeana → Wikidata:** über `sameAs`-QID, sonst GND.
2. **DDB → Wikidata:** über ISIL und GND; die DDB enthält keine QIDs.
3. **Europeana ↔ DDB:** nur indirekt über gemeinsame GND oder die Wikidata-QID.

## Befunde zu Wikidata (geprüft 15.11.2026)

- **Q6955 ist „19. Jahrhundert“, nicht Europeana.** Verwendet wird Q234110, so auch in 228
  bestehenden P7704-Referenzen.
- **ISNI (P213) steht in Wikidata ohne Leerzeichen.** Der Format-Constraint ist
  `0{7}\d{8}[\dX]`; von 2.583.174 Werten haben nur 138 Leerzeichen. Werte mit Leerzeichen
  gelten daher als Formatfehler.
- **P7704 `organisation/<n>`** löst über den Formatter `https://data.europeana.eu/$1` auf,
  `organisation/base/<n>` dagegen nicht (404). Der Format-Constraint von P7704 lautete
  `(place|agent|concept|item|organisation)/base/[1-9][0-9]*` und wurde inzwischen angepasst.
- Rund 50 Werte sind Portal-URLs
  (`https://www.europeana.eu/en/collections/organisation/5053-…`); sie werden zu
  `organisation/<n>` korrigiert.
- Die amerikanische Schreibweise **`organization/<n>`** löst ebenfalls auf, verletzt aber den
  Format-Constraint von P7704 (`(place|agent|concept|item|organisation)(/base)?/[1-9][0-9]*`,
  geprüft 27.11.2026). Seit 27.11.2026 gilt sie deshalb als Formatfehler: Ohne Quellen und
  Qualifikatoren wird sie auf `organisation/<n>` korrigiert (Löschen + Neu, Fundstelle Europeana),
  sonst nur berichtet. Beim Vergleich zählen beide Schreibweisen weiterhin als gleich.

## Projektstruktur

```
glamsync/
  config.py, http.py        # Konfiguration, HTTP-Client mit Cache, Drosselung und Retry
  sources/                  # wikidata.py, europeana.py, ddb.py, places.py (Europeana-Orte),
                            # agents.py (Europeana-Personen), gnd.py (lobid-Prüfung, GND-Dump),
                            # gnd_redirects.py (GND-Umlenkungen/-Löschungen),
                            # registry.py (ISIL-Verzeichnis, Museen in Österreich)
  normalize.py              # ID-Formate
  match.py                  # Zuordnung und Klassifikation (ohne Netz, getestet)
  registry.py               # Registerabgleich über Name + Website (ohne Netz, getestet)
  persons.py                # Personenabgleich Europeana-Agents (ohne Netz, getestet)
  orgs.py                   # Organisationen europaweit: sameAs, Website-/Kennungs-Kandidaten (ohne Netz, getestet)
  gnd.py                    # GND-Körperschaften: Name + Ort, Zeitraum, Umlenkungen, Mix'n'match,
                            # Bezeichnungen (ohne Netz, getestet)
  pipeline.py               # Ablauf fetch / compare / export-qs
  export.py, __main__.py    # Ausgabe, CLI
explore/                    # Erkundungsskripte
tests/                      # pytest
openapi.yaml                # OpenAPI-Spezifikation der DDB-API
```

## Lizenz

Apache License 2.0, siehe [LICENSE](LICENSE).

## Anreicherung aus der GND (P214, P213, P571, P131, P31)

```bash
python -m glamsync enrich --source gnd --country Q183   # bzw. Q40, Q39
```

Für Pool-Items (Museum/Bibliothek/Archiv des Landes) mit genau einer GND aus der Tabelle; die
Aussagen werden live über die Wikidata-API gelesen. Ergänzt wird nur, was fehlt; Quelle S248 GND
(Q36578) + S813.

| Property | Regel | Ausgabe |
|---|---|---|
| P214 VIAF / P213 ISNI | Wert aus dem GND-Satz; steht er schon an einem anderen Item → nur Bericht | `qs_P214_gnd_<land>_neu_…`, `qs_P213_…` |
| P571 Gründung | nur genaues Jahr (Genauigkeit Jahr) | `qs_P571_…` |
| P131 Ort | genau ein GND-Ort mit QID; nennt der Name einen Ortsteil dieses Ortes, aber nicht den Ort selbst → nur Bericht | `qs_P131_…` |
| P31 Art | erster GND-Oberbegriff mit freigegebener Klasse ersetzt Museum/Bibliothek/Archiv; die allgemeine Aussage wird nur ohne Fundstellen/Qualifikatoren entfernt; gemischte P31 (z. B. Gebäude + Museum) bleiben | `qs_P31_gnd_<land>_ersetzen_…` |

Zuordnung Oberbegriff → Klasse: `data/gnd_oberbegriff_p31.csv` (Spalte `freigegeben` = `ja`).
Neue Oberbegriffe werden bei jedem Lauf ergänzt (Suche über das deutsche Label, auch klein
geschrieben; nur Unterklassen von Museum/Bibliothek/Archiv). Bericht aller Vorschläge:
`out/anreicherung_gnd_<land>_<datum>.csv`.

### ISIL Schweiz und Österreich

- **Schweiz (`registry --source isil --country Q39`):** Quelle ist das vollständige Schweizer
  ISIL-Verzeichnis der Nationalbibliothek (opendata.swiss, CSV, ~1.900 aktive von ~2.500 Einträgen;
  Namen in allen Landessprachen, Ort, Website). Fundstelle S854 auf die Datensatz-Seite. Inaktive
  ISIL gelten bei der Bereinigung als gültig (eine ISIL bleibt gültig, auch wenn die Einrichtung
  geschlossen ist) – Löschvorschläge nur für Werte, die das Verzeichnis gar nicht kennt.
- **Österreich (`--country Q40`):** Die Berliner ISIL-Datenbank führt nur ~110 AT-ISIL; ein
  offenes Gesamtverzeichnis gibt es nicht. Deshalb keine Löschvorschläge (nur Bericht).
- **`kandidat_sicher`** (zusätzlich zu `kandidat_stark`): genau ein Item am Registerort (P131/P276/P159,
  Ortsnamen de/fr/it/rm/en) ohne ISIL, dessen Label einem Registernamen sehr ähnlich ist (≥ 0,95,
  kompakt, Ortsname darf fehlen). Bei Berliner Datensätzen zählt nur der volle Name, nicht die
  Kurzform (die lässt Zusätze wie „…, Bibliothek“ weg). → `qs_P791_isil_<land>_neu_…`
