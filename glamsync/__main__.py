"""Kommandozeile: python -m glamsync {fetch,compare,export-qs,registry,cities,persons} …"""

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
    f.add_argument("--source", required=True, choices=["europeana", "ddb", "wikidata"])
    f.add_argument("--country", default="Q183")
    f.add_argument("--types", default="museum,library,archive")
    f.add_argument("--subclasses", action=argparse.BooleanOptionalAction, default=True, help=SUB_HELP)

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
    r.add_argument("--source", required=True, choices=["isil", "mure"],
                   help="isil = Deutsche ISIL-Agentur (P791), mure = Museen in Österreich (P7917)")
    r.add_argument("--country", default="Q183", help="Q183 (DE), Q40 (AT), Q39 (CH); mure nur Q40")
    r.add_argument("--types", help="Kandidaten-Pool (Standard: isil = museum,library,archive; mure = museum)")
    r.add_argument("--subclasses", action=argparse.BooleanOptionalAction, default=True, help=SUB_HELP)

    st = sub.add_parser("cities", help="Städte: Europeana-Orte ↔ Wikidata (P7704 place/…), über sameAs")
    st.add_argument("--country", default="europa",
                    help="europa (Standard: alle souveränen Staaten Europas), ein Land (Q183) oder Liste (Q183,Q40)")
    st.add_argument("--class", dest="city_class", default=pipeline.BIG_CITY,
                    help="Wikidata-Klasse der Städte (Standard: Q1549591 Großstadt)")
    st.add_argument("--min-population", type=int, default=100_000,
                    help="zusätzlich jede Stadt (Q515) ab dieser Einwohnerzahl (Standard: 100000)")
    st.add_argument("--subclasses", action=argparse.BooleanOptionalAction, default=True, help=SUB_HELP)

    pe = sub.add_parser("persons", help="Personen: Europeana-Agents ↔ Wikidata (P7704 agent/…, P227)")
    pe.add_argument("--no-gnd-check", action="store_true",
                    help="GND nicht über lobid prüfen (dann keine P227-Vorschläge)")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    client = CachedClient(use_cache=not args.no_cache)

    if args.cmd == "fetch":
        types = pipeline.parse_types(args.types)
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
        s = pipeline.persons_compare(client, check_gnd=not args.no_gnd_check)
        print(f"Fertig. Siehe out/personen_{s['date']}.md")
        for name in s["files"]:
            print("out/" + name)
    elif args.cmd == "export-qs":
        for p in pipeline.export_qs(args.property, args.action, args.date):
            print(p)


if __name__ == "__main__":
    sys.exit(main())
