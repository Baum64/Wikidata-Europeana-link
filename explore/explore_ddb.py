"""Erkundung der DDB-API: Über welche Endpunkte bekommt man ISIL und GND?

Aufruf:  python -m explore.explore_ddb [--sample 5] [--isil-sample 200]

Prüft:
 1. Authentifizierung (ohne Schlüssel / Bearer / oauth_consumer_key)
 2. GET /institutions (Baumstruktur, Felder)
 3. Solr-Collection "organization" (Typen, GND in variant_id)
 4. Für einige Institutionen: /items/{id}, /items/{id}/view und ein Objekt
    der Institution (/items/{objekt}) mit provider-info / aggregator-info
 5. Stichprobe: Anteil Institutionen mit ISIL in /items/{id}
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter

import requests

from glamsync import config
from glamsync.http import CachedClient

B = config.DDB_API
ISIL_RE = re.compile(r"\b[A-Z]{2}-[A-Za-z0-9:/\-]{1,13}\b")
GND_RE = re.compile(r"d-nb\.info/gnd/([0-9X\-]+)")

# Bekannte Institutionen als feste Stichprobe (aus Solr ermittelt)
KNOWN = {
    "XJRNQZS3AW2U2BZTHYJQ2MAGF45YT6X2": "Staatliche Kunstsammlungen Dresden",
    "TUY45A7ZET5GOCZYKVIVNDGJAM4KSM2L": "Germanisches Nationalmuseum",
    "IIY6TSEQAORJW4ANCO43OL45ESA4FJG3": "(Museum mit ISIL DE-MUS-924218)",
    "VEU7QGGBBDTBVLHPTFH6476C7LRZ3G47": "(Einrichtung mit ISIL DE-2294)",
}


def h(title: str) -> None:
    print(f"\n=== {title} ===")


def test_auth() -> None:
    h("1. Authentifizierung")
    s = requests.Session()
    s.headers["User-Agent"] = config.user_agent()
    r = s.get(f"{B}/version", timeout=30)
    print(f"GET /version ohne Schlüssel: {r.status_code} {r.text.strip()[:60]!r}")
    r = s.get(f"{B}/institutions", params={"sector": "sec_06"}, timeout=90)
    print(f"GET /institutions ohne Schlüssel: {r.status_code}")
    r = s.get(f"{B}/version/test-auth", timeout=30)
    print(f"GET /version/test-auth ohne Schlüssel: {r.status_code}")
    if config.DDB_API_KEY:
        r = s.get(f"{B}/version/test-auth", headers={"Authorization": f"Bearer {config.DDB_API_KEY}"}, timeout=30)
        print(f"GET /version/test-auth mit Bearer: {r.status_code}")
        r = s.get(f"{B}/version/test-auth", params={"oauth_consumer_key": config.DDB_API_KEY}, timeout=30)
        print(f"GET /version/test-auth mit oauth_consumer_key: {r.status_code}")
    else:
        print("DDB_API_KEY nicht gesetzt – Bearer/oauth_consumer_key nicht getestet.")


def walk(nodes, depth=0):
    for n in nodes:
        yield n, depth
        yield from walk(n.get("children") or [], depth + 1)


def explore_institutions(client: CachedClient) -> None:
    h("2. GET /institutions")
    tree = client.get_json(f"{B}/institutions")
    flat = list(walk(tree))
    keys = Counter(k for n, _ in flat for k in n)
    print(f"Wurzelknoten: {len(tree)}, Knoten gesamt: {len(flat)}, max. Tiefe: {max(d for _, d in flat)}")
    print(f"Felder: {sorted(keys)}")
    print("Sektoren:", dict(Counter(n.get('sector') for n, _ in flat).most_common()))
    print("mit Objekten:", sum(1 for n, _ in flat if n.get("hasItems")))


def solr(client, collection, **params):
    return client.get_json(f"{B}/search/index/{collection}/select", params={"wt": "json", **params})


def explore_solr(client: CachedClient) -> None:
    h("3. Solr-Collection 'organization'")
    d = solr(client, "organization", q="*:*", rows=0, facet="true", **{"facet.field": "type"})
    print("Einträge:", d["response"]["numFound"], "Typen:", d["facet_counts"]["facet_fields"]["type"])
    for q in ["type:ddb-institution", "type:ddb-institution AND variant_id:*",
              "type:ddb-institution AND parent_id:*"]:
        print(f"{q!r}: {solr(client, 'organization', q=q, rows=0)['response']['numFound']}")
    d = solr(client, "organization", q="type:ddb-institution", rows=2000, fl="variant_id")
    prefixes = Counter(re.sub(r"[^/]+$", "", v) for x in d["response"]["docs"] for v in x.get("variant_id", []))
    print("variant_id-Präfixe (2000 Institutionen):", dict(prefixes))
    print("Felder im Dokument:", sorted(k for k in solr(client, "organization", q="type:ddb-institution", rows=1)
                                        ["response"]["docs"][0] if not k.endswith(("_preprocessed", "_autocomplete"))))


def find_ids(obj) -> dict[str, set[str]]:
    """Durchsucht beliebiges JSON nach ISIL- und GND-Mustern (mit Fundstelle)."""
    found: dict[str, set[str]] = {"isil": set(), "gnd": set()}

    def rec(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                rec(v, f"{path}.{k}")
        elif isinstance(o, list):
            for v in o:
                rec(v, f"{path}[]")
        elif isinstance(o, str) and o:
            if "isil" in path.lower() or "organisations/" in o:
                found["isil"].add(f"{path} = {o}")
            for m in GND_RE.findall(o):
                found["gnd"].add(f"{path} = {m}")
    rec(obj, "")
    return found


def explore_sample(client: CachedClient, n: int) -> None:
    h(f"4. Einzelabrufe für {n} Institutionen")
    for inst_id, label in list(KNOWN.items())[:n]:
        print(f"\n--- {inst_id} {label}")
        item = client.get_json(f"{B}/items/{inst_id}", allow_404=True)
        view = client.get_json(f"{B}/items/{inst_id}/view", allow_404=True)
        if item:
            pi = item.get("provider-info", {})
            print("  /items/{id} provider-info.provider-isil:", repr(pi.get("provider-isil")),
                  "| provider-item-id:", pi.get("provider-item-id"),
                  "| cortex-type:", item.get("properties", {}).get("cortex-type"))
            for kind, vals in find_ids(item).items():
                for v in sorted(vals):
                    print(f"  /items {kind}: {v}")
        if view:
            ci = view.get("cortex-institution") or {}
            print("  /view cortex-institution.isil:", repr(ci.get("isil")),
                  "| europeana-name:", repr(ci.get("europeana-name")))
            for kind, vals in find_ids(view).items():
                for v in sorted(vals):
                    print(f"  /view {kind}: {v}")
        # Ein Objekt dieser Institution: provider-info und aggregator-info
        d = solr(client, "search", q=f'provider_id:"{inst_id}"', rows=1, fl="id")
        docs = d["response"]["docs"]
        print(f"  Objekte in Solr 'search': {d['response']['numFound']}")
        if docs:
            obj = client.get_json(f"{B}/items/{docs[0]['id']}", allow_404=True) or {}
            pi = obj.get("provider-info") or {}
            ai = obj.get("aggregator-info") or {}
            print("  Objekt provider-info:", {k: v for k, v in pi.items() if k in (
                "provider-isil", "provider-ddb-id", "provider-europeana-name", "provider-item-id")})
            print("  Objekt aggregator-info:", {k: v for k, v in ai.items() if k in (
                "aggregator-isil", "aggregator-ddb-id", "aggregator-europeana-id",
                "aggregator-europeana-name", "aggregator-name")} if ai else None)


def isil_sample(client: CachedClient, n: int) -> None:
    h(f"5. ISIL-Stichprobe ({n} Institutionen, /items/{{id}} provider-isil)")
    d = solr(client, "organization", q="type:ddb-institution", rows=n, fl="id,variant_id", sort="id asc")
    stats = Counter()
    forms = Counter()
    for doc in d["response"]["docs"]:
        item = client.get_json(f"{B}/items/{doc['id']}", allow_404=True) or {}
        isil = (item.get("provider-info") or {}).get("provider-isil") or ""
        has_gnd = bool(doc.get("variant_id"))
        stats[(bool(isil), has_gnd)] += 1
        if isil:
            forms[re.sub(r"[^/]+$", "…", isil)] += 1
    print("(ISIL, GND) → Anzahl:", dict(stats))
    print("ISIL-Formen:", dict(forms))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=4)
    ap.add_argument("--isil-sample", type=int, default=200)
    args = ap.parse_args()
    client = CachedClient()
    test_auth()
    explore_institutions(client)
    explore_solr(client)
    explore_sample(client, args.sample)
    isil_sample(client, args.isil_sample)


if __name__ == "__main__":
    main()
