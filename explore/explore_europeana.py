"""Erkundung der Europeana Entity API für Organisationen.

Aufruf:  python -m explore.explore_europeana [--page-size 100]

Ermittelt: Gesamtzahl Organisationen, Feldverteilung, Anteil mit Wikidata-sameAs,
Art der sameAs-Ziele (VIAF, ISNI, GND, ISIL …), Länderverteilung, ob ISIL vorkommt.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter

from glamsync import config
from glamsync.http import CachedClient

E = config.EUROPEANA_ENTITY
ISIL_RE = re.compile(r"\b(DE|AT|CH|[A-Z]{2})-(MUS-)?[A-Za-z0-9:\-/]{1,12}\b")


def classify(uri: str) -> str:
    """Ordnet einen sameAs-URI einer Quelle zu."""
    u = uri.lower()
    for key, pat in [
        ("wikidata", "wikidata.org/entity/"),
        ("viaf", "viaf.org/viaf/"),
        ("isni", "isni.org/isni/"),
        ("gnd", "d-nb.info/gnd/"),
        ("isil", "isil"),
        ("isil", "zdb-services.de/resource/organisations/"),
        ("zoho_crm", "crm.zoho.eu"),
        ("europeana", "data.europeana.eu"),
        ("bnf", "data.bnf.fr"),
        ("loc", "id.loc.gov"),
    ]:
        if pat in u:
            return key
    return "sonstige:" + re.sub(r"^https?://([^/]+).*", r"\1", u)


def fetch_all(client: CachedClient, page_size: int) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        d = client.get_json(
            f"{E}/search",
            params={"query": "*", "type": "organization", "page": page,
                    "pageSize": page_size, "wskey": config.EUROPEANA_API_KEY},
        )
        batch = d.get("items") or []
        items.extend(batch)
        total = d.get("partOf", {}).get("total")
        print(f"\rSeite {page}: {len(items)}/{total}", end="", file=sys.stderr)
        if not batch or not d.get("next"):
            break
        page += 1
    print(file=sys.stderr)
    return items


def label(o: dict) -> str:
    pl = o.get("prefLabel") or {}
    return pl.get("de") or pl.get("en") or next(iter(pl.values()), "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--page-size", type=int, default=100)
    args = ap.parse_args()
    if not config.EUROPEANA_API_KEY:
        sys.exit("EUROPEANA_API_KEY ist nicht gesetzt (siehe .env.example).")
    client = CachedClient()

    orgs = fetch_all(client, args.page_size)
    ids = [o["id"] for o in orgs]
    print(f"Organisationen gesamt: {len(orgs)} (eindeutige IDs: {len(set(ids))})")
    print("ID-Formen:", Counter(re.sub(r"\d+$", "<n>", i) for i in ids).most_common())
    print("type:", Counter(o.get("type") for o in orgs).most_common())
    print("Felder:", sorted(Counter(k for o in orgs for k in o).items(), key=lambda x: -x[1]))

    countries = Counter(((o.get("country") or {}).get("prefLabel") or {}).get("en") for o in orgs)
    print("Länder (Top 15):", countries.most_common(15))
    de = [o for o in orgs if ((o.get("country") or {}).get("prefLabel") or {}).get("en") == "Germany"]

    for name, subset in [("alle", orgs), ("Deutschland", de)]:
        src = Counter()
        n_wd = 0
        multi_wd = 0
        for o in subset:
            kinds = [classify(s) for s in o.get("sameAs") or []]
            src.update(set(kinds))
            n_wd += "wikidata" in kinds
            multi_wd += kinds.count("wikidata") > 1
        print(f"\n[{name}] n={len(subset)}, mit Wikidata-sameAs: {n_wd} "
              f"({100 * n_wd / max(len(subset), 1):.1f} %), davon >1 QID: {multi_wd}")
        print(f"[{name}] sameAs-Quellen (Organisationen je Quelle):", src.most_common(20))

    # ISIL: in sameAs, identifier oder irgendeinem Textfeld?
    isil_hits = Counter()
    examples = []
    for o in orgs:
        for field in ("identifier", "sameAs", "acronym", "hasAddress"):
            text = repr(o.get(field, ""))
            if field == "sameAs":
                text = repr([s for s in o.get("sameAs") or [] if classify(s) == "isil"])
            m = ISIL_RE.search(text) if field != "acronym" else None
            if m and field != "hasAddress":
                isil_hits[field] += 1
                if len(examples) < 5:
                    examples.append((o["id"], field, m.group(0)))
    print("\nISIL-Treffer nach Feld:", dict(isil_hits), "Beispiele:", examples)
    print("identifier-Beispiele:", [o.get("identifier") for o in orgs if o.get("identifier")][:5])

    # Einzelabruf testen (Format der Detailseite)
    sample = de[0]["id"].rsplit("/", 1)[-1] if de else "4373"
    one = client.get_json(f"{E}/organization/{sample}.json", params={"wskey": config.EUROPEANA_API_KEY})
    print(f"\nEinzelabruf organization/{sample}.json → Felder: {sorted(one)}")
    print("Beispiele DE mit sameAs:")
    for o in de[:8]:
        print(f"  {o['id']}  {label(o)!r}  {o.get('sameAs')}")


if __name__ == "__main__":
    main()
