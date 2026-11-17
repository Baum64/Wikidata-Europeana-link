# glamsync – Abgleich von Kulturerbe-Einrichtungen zwischen Europeana, DDB und Wikidata

Repository: <https://github.com/Baum64/Wikidata-Europeana-link>

Das Tool ruft Organisationen (Museen, Bibliotheken, Archive) aus der **Europeana Entity API** und
der **API der Deutschen Digitalen Bibliothek (DDB)** ab und gleicht sie mit **Wikidata** ab.
Für fehlende oder fehlerhafte Identifikatoren erzeugt es **QuickStatements zur manuellen Prüfung**.
Das Tool schreibt **niemals selbst** in Wikidata.

| Property | Name | Format in Wikidata |
|---|---|---|
| P7704 | Europeana entity | `organisation/<n>` (britische Schreibweise), `agent/base/<n>`, `place/base/<n>`, `concept/base/<n>` |
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
| `quickstatements_<P>_{neu,korrektur,loeschen}_<datum>_partNN.qs` | max. 50 Zeilen je Datei; Korrekturpaare (Löschen + Neu) werden nicht getrennt; `loeschen` = ungültige Werte |
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
(`quickstatements_<P>_loeschen_<datum>_partNN.qs`, Aktion `delete`), sofern die Aussage weder
Quellen noch Qualifier hat; sonst nur Bericht. Objekt-IDs ggf. vorher nach P727 übertragen.
Dubletten (ein P7704-Wert an mehreren Items) erscheinen nur im Bericht.

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
| `kandidat_stark` | Item **ohne** diesen Wert mit **gleichem Namen UND gleicher Website** | `quickstatements_<P>_kandidaten_<land>_<datum>_partNN.qs` – **nur nach Handprüfung einspielen** |
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
| Wert hat ungültiges Format | `quickstatements_<P>_loeschen_<land>_<datum>_partNN.qs` |
| Wert fehlt im Register | `quickstatements_<P>_loeschen_pruefen_<land>_<datum>_partNN.qs` – **vor dem Löschen prüfen**: kann eine aufgelöste Einrichtung (historisch korrekter Wert) oder ein Tippfehler sein |

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
- **Wertform:** `place/base/<n>`, so wie die bestehenden Orts-Werte in Wikidata und der
  Format-Constraint. `place/<n>` löst ebenfalls auf.
- **Europeana-Dubletten:** Viele Städte gibt es in Europeana zwei- oder dreimal mit
  identischen Daten. Weil P7704 einwertig ist, gilt:
  1. Führt Wikidata bereits einen der Orte, zählt dieser.
  2. Sonst wird der Ort mit eindeutig den meisten verknüpften Objekten
     (`isAggregatedBy.recordCount`) vorgeschlagen.
  3. Bei Gleichstand ist der Fall `mehrdeutig` und steht nur im Bericht.
- **Ausgabe:**
  - `out/staedte_<land|europa>_<datum>.csv` (Bericht mit Länderspalte) und
    `out/staedte_<land|europa>_<datum>.md` (Zusammenfassung mit Tabelle je Land und Dublettenliste);
  - `out/quickstatements_P7704_staedte_<land>_{neu,korrektur,loeschen}_<datum>_partNN.qs`,
    Fundstelle S248 Europeana (Q234110) + S813.

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

## Erkundung der Quellen

```bash
python -m explore.explore_ddb          # DDB: wo stehen ISIL und GND?
python -m explore.explore_europeana    # Europeana: Umfang, sameAs-Abdeckung, ISIL?
```

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
- Die amerikanische Schreibweise **`organization/<n>`** löst ebenfalls auf und gilt als
  korrekt: Dafür werden **keine** Korrektur-QS erzeugt (beim Vergleich zählen beide
  Schreibweisen als gleich).

## Projektstruktur

```
glamsync/
  config.py, http.py        # Konfiguration, HTTP-Client mit Cache, Drosselung und Retry
  sources/                  # wikidata.py, europeana.py, ddb.py, places.py (Europeana-Orte),
                            # registry.py (ISIL-Verzeichnis, Museen in Österreich)
  normalize.py              # ID-Formate
  match.py                  # Zuordnung und Klassifikation (ohne Netz, getestet)
  registry.py               # Registerabgleich über Name + Website (ohne Netz, getestet)
  pipeline.py               # Ablauf fetch / compare / export-qs
  export.py, __main__.py    # Ausgabe, CLI
explore/                    # Erkundungsskripte
tests/                      # pytest
openapi.yaml                # OpenAPI-Spezifikation der DDB-API
```

## Lizenz

Apache License 2.0, siehe [LICENSE](LICENSE).
