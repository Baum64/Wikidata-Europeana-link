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

API-Schlüssel werden nur aus Umgebungsvariablen bzw. `.env` gelesen, nie geloggt und nicht in den
Cache geschrieben. `.env` ist über `.gitignore` von Git ausgeschlossen.

Alle HTTP-Antworten werden in `cache/` zwischengespeichert (eine JSON-Datei pro Anfrage). Für
frische Daten den Ordner `cache/` löschen oder `--no-cache` verwenden.

## Benutzung

```bash
# Trockenlauf: max. 25 Einrichtungen je Quelle, ohne Wikidata-Grundmenge
python -m glamsync compare --country Q183 --limit 25

# Vollständiger Lauf: lädt die Grundmenge je Typ; beim ersten Mal ~5.000 DDB-Einzelabrufe (ca. 20 min)
python -m glamsync compare --country Q183 --types museum,library,archive

# QuickStatements aus dem letzten Abgleich schreiben (alle oder gezielt)
python -m glamsync export-qs
python -m glamsync export-qs --property P7704 --action new

# Rohdaten einzeln abrufen und unter data/ ablegen
python -m glamsync fetch --source europeana --country Q183
```

Weitere Optionen: `--subclasses` (Wikidata P31/P279* statt nur direkter P31),
`--sources europeana,ddb`, `--no-cache`, `-v`.

### Ausgabe (`out/`)

| Datei | Inhalt |
|---|---|
| `summary_<datum>.md` | Zählungen: Grundmenge vorher → nachher-Potenzial, Zuordnung, Klassifikation, QS-Umfang |
| `report_<datum>.csv` | eine Zeile je Quell-Einrichtung (Klassifikation je Property, Trefferquelle) plus Zeilen `nur_wikidata` für reine Formatbefunde; Trennzeichen `;` |
| `kandidaten_namen_<datum>.csv` | nicht zugeordnete Einrichtungen mit ähnlich benannten Items, **nur zur Handprüfung** |
| `quickstatements_<P>_{neu,korrektur}_<datum>_partNN.qs` | max. 50 Zeilen je Datei; Korrekturpaare (Löschen + Neu) werden nicht getrennt |
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
| `formatfehler` | gleicher Wert in falscher Form (`organization/…`, URL, ISNI mit Leerzeichen …) | Löschen + Neu, **nur wenn** die alte Aussage keine Quellen/Qualifier hat |
| `konflikt` | Wikidata hat einen anderen Wert | – (nur Bericht) |
| `mehrdeutig` | Wert passt auf mehrere Items, Quelle hat mehrere Werte, oder Wert hängt schon an anderem Item | – (nur Bericht) |

Fundstellen (S248): **Europeana = Q234110**, **Deutsche Digitale Bibliothek = Q621630**
(per SPARQL geprüft). Werte mit dem Rang „deprecated“ werden beim Vergleich ignoriert.

Unabhängig von den Quellen prüft das Tool alle P7704-Werte außer `agent/…` sowie alle
Identifikatoren der beteiligten Items auf Formatfehler. Korrekturen bei
`organisation/…` werden nur vorgeschlagen, wenn es die ID in der Europeana-Organisationsliste
gibt; dann wird Europeana als Fundstelle gesetzt. Objekt-IDs (`item/…`, `90402/…`, gehören zu
P727), Werte ohne Präfix und Dubletten erscheinen nur im Bericht.

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
- Neben 104 Werten `organization/…` gibt es rund 50 Portal-URLs
  (`https://www.europeana.eu/en/collections/organisation/5053-…`); beide werden zu
  `organisation/<n>` korrigiert.

## Projektstruktur

```
glamsync/
  config.py, http.py        # Konfiguration, HTTP-Client mit Cache, Drosselung und Retry
  sources/                  # wikidata.py, europeana.py, ddb.py
  normalize.py              # ID-Formate
  match.py                  # Zuordnung und Klassifikation (ohne Netz, getestet)
  pipeline.py               # Ablauf fetch / compare / export-qs
  export.py, __main__.py    # Ausgabe, CLI
explore/                    # Erkundungsskripte
tests/                      # pytest
openapi.yaml                # OpenAPI-Spezifikation der DDB-API
```

## Lizenz

Apache License 2.0, siehe [LICENSE](LICENSE).
