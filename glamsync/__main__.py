"""Kommandozeile: python -m glamsync {fetch,compare,export-qs} …"""

from __future__ import annotations

import argparse
import logging
import sys

from . import pipeline
from .http import CachedClient
from .normalize import PROPERTIES


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="glamsync", description="Abgleich Europeana/DDB ↔ Wikidata (nur Vorschläge, schreibt nie in Wikidata)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-cache", action="store_true", help="HTTP-Cache ignorieren (Antworten werden neu geschrieben)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="Daten einer Quelle abrufen und unter data/ speichern")
    f.add_argument("--source", required=True, choices=["europeana", "ddb", "wikidata"])
    f.add_argument("--country", default="Q183")
    f.add_argument("--types", default="museum,library,archive")
    f.add_argument("--subclasses", action="store_true", help="Wikidata: P31/P279* statt nur direkter P31")

    c = sub.add_parser("compare", help="Abgleich durchführen, Bericht und Zusammenfassung schreiben")
    c.add_argument("--country", default="Q183")
    c.add_argument("--types", default="museum,library,archive")
    c.add_argument("--sources", default="europeana,ddb")
    c.add_argument("--limit", type=int, help="Trockenlauf: max. N Einrichtungen je Quelle")
    c.add_argument("--subclasses", action="store_true")

    e = sub.add_parser("export-qs", help="QuickStatements aus dem letzten Abgleich schreiben")
    e.add_argument("--property", default="all", choices=PROPERTIES + ["all"])
    e.add_argument("--action", default="all", choices=["new", "fix", "all"])
    e.add_argument("--date", help="Datum des Abgleichs (Standard: heute bzw. letzter)")

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
    elif args.cmd == "export-qs":
        for p in pipeline.export_qs(args.property, args.action, args.date):
            print(p)


if __name__ == "__main__":
    sys.exit(main())
