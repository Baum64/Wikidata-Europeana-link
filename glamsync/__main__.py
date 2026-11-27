"""Kommandozeile: python -m glamsync {fetch,compare,export-qs,registry,export-mnm,cities,persons,organisations} …"""

from __future__ import annotations

import argparse
import logging
import sys

from . import pipeline
from .http import CachedClient
from .normalize import PROPERTIES


SUB_HELP = "Wikidata: P31/P279* einschließlich Unterklassen (Standard); --no-subclasses = nur direkte P31"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="glamsync", description="Abgleich Europeana/DDB ↔ Wikidata (nur Vorschläge, schreibt nie in Wikidata)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-cache", action="store_true", help="HTTP-Cache ignorieren (Antworten werden neu geschrieben)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="Daten einer Quelle abrufen und unter data/ speichern")
    f.add_argument("--source", required=True, choices=["europeana", "ddb", "wikidata", "gnd"],
                   help="gnd = lokale Tabelle aus dem GND-Dump erzeugen (GND_DUMP_PATH)")
    f.add_argument("--country", default="Q183")
    f.add_argument("--types", default="museum,library,archive")
    f.add_argument("--subclasses", action=argparse.BooleanOptionalAction, default=True, help=SUB_HELP)
    f.add_argument("--rebuild", action="store_true", help="gnd: Tabelle auch dann neu erzeugen, wenn sie aktuell ist")

    c = sub.add_parser("compare", help="Abgleich durchführen, Bericht und Zusammenfassung schreiben")
    c.add_argument("--country", default="Q183")
    c.add_argument("--types", default="museum,library,archive")
    c.add_argument("--sources", default="europeana,ddb")
    c.add_argument("--limit", type=int, help="Trockenlauf: max. N Einrichtungen je Quelle")
    c.add_argument("--subclasses", action=argparse.BooleanOptionalAction, default=True, help=SUB_HELP)

    e = sub.add_parser("export-qs", help="QuickStatements aus dem letzten Abgleich schreiben")
    e.add_argument("--property", default="all", choices=PROPERTIES + ["all"])
    e.add_argument("--action", default="all", choices=["new", "fix", "delete", "all"],
                   help="new = ergänzen, fix = Formatkorrektur, delete = ungültige Werte löschen")
    e.add_argument("--date", help="Datum des Abgleichs (Standard: heute bzw. letzter)")

    r = sub.add_parser("registry", help="Abgleich mit Registern ohne Wikidata-Link (Name + Website)")
    r.add_argument("--source", required=True, choices=["isil", "mure", "gnd"],
                   help="isil = Deutsche ISIL-Agentur (P791), mure = Museen in Österreich (P7917), "
                        "gnd = GND-Dump Körperschaften (P227: Kandidaten nach Name + Ort, Mix'n'match)")
    r.add_argument("--country", default="Q183", help="Q183 (DE), Q40 (AT), Q39 (CH); mure nur Q40")
    r.add_argument("--types", help="Kandidaten-Pool (Standard: isil = museum,library,archive; mure = museum)")
    r.add_argument("--subclasses", action=argparse.BooleanOptionalAction, default=True, help=SUB_HELP)
    r.add_argument("--limit", type=int, help="gnd: Trockenlauf mit max. N Einrichtungen")
    r.add_argument("--rebuild", action="store_true", help="gnd: Tabelle aus dem Dump neu erzeugen")
    r.add_argument("--include-historical", action="store_true",
                   help="gnd: auch aufgelöste Einrichtungen und frühere Namensformen suchen und exportieren")
    r.add_argument("--propose-dates", action="store_true",
                   help="gnd: GND-Zeitraum als Qualifikator P580/P582 an P227 (nur genaues Jahr, nur wenn er fehlt)")

    mn = sub.add_parser("export-mnm", help="Mix'n'match-CSV aus dem letzten GND-Abgleich (je Typ und Land)")
    mn.add_argument("--country", default="Q183", help="Q183 (DE inkl. DDR), Q40 (AT), Q39 (CH)")
    mn.add_argument("--date", help="Datum des Abgleichs (Standard: letzter)")
    mn.add_argument("--include-historical", action="store_true",
                    help="auch aufgelöste Einrichtungen und frühere Namensformen (mit Zeitraum in der Beschreibung)")

    lb = sub.add_parser("labels", help="Deutsche Labels, Beschreibungen und Aliase aus der GND (QuickStatements)")
    lb.add_argument("--source", required=True, choices=["gnd"])
    lb.add_argument("--country", default="Q183", help="Q183 (DE inkl. DDR), Q40 (AT), Q39 (CH)")
    lb.add_argument("--rebuild", action="store_true", help="GND-Tabelle aus dem Dump neu erzeugen")

    en = sub.add_parser("enrich", help="Ergänzungen aus der GND für Items mit P227: P214, P213, P571, P131, P31")
    en.add_argument("--source", required=True, choices=["gnd"])
    en.add_argument("--country", default="Q183", help="Q183 (DE inkl. DDR), Q40 (AT), Q39 (CH)")

    st = sub.add_parser("cities", help="Städte: Europeana-Orte ↔ Wikidata (P7704 place/…), über sameAs")
    st.add_argument("--country", default="europa",
                    help="europa (Standard: alle souveränen Staaten Europas), ein Land (Q183) oder Liste (Q183,Q40)")
    st.add_argument("--class", dest="city_class", default=pipeline.BIG_CITY,
                    help="Wikidata-Klasse der Städte (Standard: Q1549591 Großstadt)")
    st.add_argument("--min-population", type=int, default=100_000,
                    help="zusätzlich jede Stadt (Q515) ab dieser Einwohnerzahl (Standard: 100000)")
    st.add_argument("--subclasses", action=argparse.BooleanOptionalAction, default=True, help=SUB_HELP)

    pe = sub.add_parser("persons", help="Personen: Europeana-Agents ↔ Wikidata (P7704 agent/…, P227)")
    pe.add_argument("--gnd", action="store_true",
                    help="auch P227 vorschlagen: Items ohne GND suchen und über lobid prüfen (langsam, selten ergiebig)")

    og = sub.add_parser("organisations", help="Organisationen europaweit: Europeana ↔ Wikidata (P7704 organisation/…)")
    og.add_argument("--no-candidates", action="store_true",
                    help="nur Stufe 1 (sameAs); keine Kandidatensuche über Website/GND/VIAF/ISNI")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    client = CachedClient(use_cache=not args.no_cache)

    if args.cmd == "fetch":
        types = pipeline.parse_types(args.types)
        if args.source == "gnd":
            st = pipeline.gnd_table(args.rebuild)
            print(f"GND-Tabelle: {st.get('einrichtungen', 0)} Museen/Bibliotheken/Archive "
                  f"aus {st.get('datensaetze', 0)} Datensätzen, defekt: {st.get('defekt_knoten', 0)}")
            return
        if args.source == "europeana":
            n = len(pipeline.fetch_europeana(client))
        elif args.source == "ddb":
            n = len(pipeline.fetch_ddb(client, types, None))
        else:
            n = sum(len(v) for v in pipeline.fetch_wikidata(client, args.country, types, args.subclasses).values())
        print(f"{n} Datensätze gespeichert.")
    elif args.cmd == "compare":
        s = pipeline.compare(client, args.country, pipeline.parse_types(args.types), args.limit,
                             args.subclasses, tuple(x.strip() for x in args.sources.split(",")))
        print(f"Fertig. Siehe out/summary_{s['date']}.md und out/report_{s['date']}.csv")
    elif args.cmd == "registry" and args.source == "gnd":
        types = pipeline.parse_types(args.types) if args.types else None
        s = pipeline.gnd_compare(client, args.country, types, args.subclasses, args.limit, args.rebuild,
                                 args.include_historical, args.propose_dates)
        print(f"Fertig. Siehe out/register_gnd_{s['country']}_{s['date']}.md")
        for name in [*s["mnm"], *s["qs"]]:
            print("out/" + name)
    elif args.cmd == "export-mnm":
        for p, n in pipeline.export_mnm(args.country, args.date, args.include_historical):
            print(f"{p} ({n} Einträge)")
    elif args.cmd == "enrich":
        s = pipeline.gnd_enrich(client, args.country)
        print(f"{s['items']} Items mit GND; Bericht out/{s['report']}")
        for (prop, action), n in sorted(s["counts"].items()):
            print(f"  {prop} {action}: {n}")
        print(f"P31-Zuordnung: {s['map']} ({s['map_open']} Oberbegriffe noch nicht freigegeben)")
        for name in s["files"]:
            print("out/" + name)
    elif args.cmd == "labels":
        s = pipeline.gnd_labels(client, args.country, args.rebuild)
        print(f"{s['items']} Items mit GND aus der Tabelle; Hinweise: {s['notes']} (out/{s['report']})")
        for kind, c in s["counts"].items():
            print(f"  {kind}: {c['items']} Items, {c['lines']} Befehle")
        for name in s["files"]:
            print("out/" + name)
    elif args.cmd == "registry":
        types = pipeline.parse_types(args.types) if args.types else None
        s = pipeline.registry_compare(client, args.source, args.country, types, args.subclasses)
        print(f"Fertig. Siehe out/register_{s['source']}_{s['country']}_{s['date']}.md")
    elif args.cmd == "cities":
        s = pipeline.cities_compare(client, args.country, args.city_class, args.subclasses,
                                    args.min_population)
        print(f"Fertig. Siehe out/staedte_{s['country']}_{s['date']}.md")
        for name in s["files"]:
            print("out/" + name)
    elif args.cmd == "persons":
        s = pipeline.persons_compare(client, gnd_search=args.gnd)
        print(f"Fertig. Siehe out/personen_{s['date']}.md")
        for name in s["files"]:
            print("out/" + name)
    elif args.cmd == "organisations":
        s = pipeline.organisations_compare(client, candidates=not args.no_candidates)
        print(f"Fertig. Siehe out/organisationen_{s['date']}.md")
        for name in s["files"]:
            print("out/" + name)
    elif args.cmd == "export-qs":
        for p in pipeline.export_qs(args.property, args.action, args.date):
            print(p)


if __name__ == "__main__":
    sys.exit(main())
