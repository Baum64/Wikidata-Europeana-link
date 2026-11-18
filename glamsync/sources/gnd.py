"""GND-Prüfung über lobid-gnd (https://lobid.org/gnd), ohne Schlüssel.

Vor einem P227-Vorschlag wird geprüft, ob die GND existiert, eine Person
(DifferentiatedPerson) beschreibt und nicht in einen anderen Datensatz
umgelenkt wurde.
"""

from __future__ import annotations

import requests

from ..http import CachedClient
from ..normalize import normalize_gnd

LOBID = "https://lobid.org/gnd"


def _compact(d: dict) -> dict:
    return {"id": normalize_gnd(d.get("id") or "") or d.get("gndIdentifier"),
            "types": d.get("type") or [], "name": d.get("preferredName") or ""}


def gnd_info(client: CachedClient, gnd: str) -> dict | None:
    """{id, types, name}; None, wenn die GND nicht existiert; {"error": …}, wenn lobid scheitert.

    lobid antwortet auf unbekannte IDs teils mit HTTP 500 statt 404 – das gilt als
    „nicht prüfbar“, sodass kein Vorschlag entsteht.
    """
    try:
        return client.get_json(f"{LOBID}/{gnd}.json", allow_404=True, transform=_compact, cache_tag=":compact")
    except requests.HTTPError as exc:
        return {"error": exc.response.status_code if exc.response is not None else "?"}


def gnd_problem(gnd: str, info: dict | None) -> str | None:
    """None = GND ist eine gültige, aktuelle Personen-GND; sonst Begründung."""
    if info is None:
        return "GND existiert nicht (lobid 404)"
    if "error" in info:
        return f"GND nicht prüfbar (lobid HTTP {info['error']})"
    if info.get("id") and info["id"] != gnd:
        return f"GND umgelenkt auf {info['id']}"
    if "DifferentiatedPerson" not in info.get("types", []):
        return f"keine Person in der GND ({', '.join(info.get('types', [])) or 'unbekannt'})"
    return None
