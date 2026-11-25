"""Ergänzungen aus der GND für Items, die schon eine GND (P227) tragen.

Nur wo Wikidata noch nichts hat; Quelle immer S248 GND (Q36578) + Abrufdatum.

- P214 VIAF und P213 ISNI: Wert aus dem GND-Satz, wenn das Item keinen hat und der Wert in
  Wikidata nirgends sonst steht.
- P571 Gründung: nur genaues Jahr (GND start_year), Genauigkeit Jahr.
- P131 Ort: nur wenn das Item keinen hat und die GND genau einen Ort mit QID nennt. Nennt der Name
  (Item-Label oder GND-Name) einen Ortsteil dieses Ortes, aber nicht den Ort selbst, kein Vorschlag
  („Heimatmuseum Kirchheim“ liegt in Kirchheim, nicht allgemein in Heidelberg).
- P31 Art: der GND-Oberbegriff ersetzt eine allgemeine Klasse (Museum/Bibliothek/Archiv), über
  eine freigegebene Zuordnungstabelle (data/gnd_oberbegriff_p31.csv, Spalte freigegeben = ja).
  Die allgemeine Aussage wird nur entfernt, wenn sie weder Fundstellen noch Qualifikatoren hat.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .gnd import GND_ITEM
from .normalize import normalize
from .sources.gnd import GndBody

GENERIC = {"Q33506": "Museum", "Q7075": "Bibliothek", "Q166118": "Archiv"}
PROPS = ("P227", "P214", "P213", "P571", "P131", "P31")


@dataclass
class Proposal:
    qid: str
    prop: str
    value: str  # Item-ID, Zeichenkette (ohne Anführungszeichen) oder QS-Zeit
    note: str = ""
    remove: str = ""  # P31: zu ersetzende allgemeine Klasse (leer = nur ergänzen)
    action: str = "neu"  # neu | bericht

    def commands(self, date: str) -> list[str]:
        ref = f"\tS248\t{GND_ITEM}\tS813\t+{date}T00:00:00Z/11"
        v = f'"{self.value}"' if self.prop in ("P214", "P213") else self.value
        out = [f"-{self.qid}\tP31\t{self.remove}"] if self.remove else []
        return out + [f"{self.qid}\t{self.prop}\t{v}{ref}"]


def _values(entity: dict, prop: str) -> list:
    return [s["value"] for s in entity["claims"].get(prop, []) if s["rank"] != "deprecated"]


def _words(text: str) -> str:
    return " " + re.sub(r"[^\w]+", " ", text.casefold()) + " "


def _place_core(place: str) -> str:
    """„Polling (Landkreis Weilheim-Schongau)“ → „Polling“, „Schwarzenberg/Erzgeb.“ → „Schwarzenberg“."""
    return re.split(r"\s*[(/,]", place, maxsplit=1)[0].strip()


def _mentions(names: list[str], place: str) -> bool:
    p = _words(_place_core(place)).strip()
    return bool(p) and any(f" {p} " in _words(n) for n in names)


def id_proposals(entity_qid: str, entity: dict, b: GndBody, owners: dict[str, dict[str, list[str]]]) -> list[Proposal]:
    """P214/P213 aus der GND; `owners` = {P: {wert: [QIDs]}} über ganz Wikidata."""
    out = []
    for prop, raw in (("P214", b.viaf[:1]), ("P213", b.isni[:1])):
        if not raw or _values(entity, prop):
            continue
        v = normalize(prop, raw[0])
        if not v:
            continue
        others = set(owners.get(prop, {}).get(v, [])) - {entity_qid}
        if others:
            out.append(Proposal(entity_qid, prop, v, f"schon an {', '.join(sorted(others))}", action="bericht"))
        else:
            out.append(Proposal(entity_qid, prop, v))
    return out


def inception_proposal(qid: str, entity: dict, b: GndBody) -> Proposal | None:
    if b.start_year and not _values(entity, "P571"):
        return Proposal(qid, "P571", f"+{b.start_year:04d}-00-00T00:00:00Z/9", f"GND: {b.period_raw or b.established}")
    return None


def place_proposal(qid: str, entity: dict, b: GndBody, districts: dict[str, set[str]]) -> Proposal | None:
    """P131 aus dem einzigen GND-Ort; Ortsteil im Namen (ohne den Ort selbst) → Bericht."""
    if _values(entity, "P131"):
        return None
    places = {p["qid"]: p.get("name", "") for p in b.places if p.get("qid")}
    if len(places) != 1:
        return None
    pq, pname = next(iter(places.items()))
    names = list(entity["labels"].values()) + [b.name, *b.variants]
    if not _mentions(names, pname):
        core = _place_core(pname).casefold()
        hit = sorted(d for d in districts.get(pq, ())
                     if len(d) >= 4 and _place_core(d).casefold() != core and _mentions(names, d))
        if hit:
            return Proposal(qid, "P131", pq, f"Ortsteil im Namen: {', '.join(hit)} (GND-Ort {pname})", action="bericht")
    return Proposal(qid, "P131", pq, f"GND-Ort {pname}")


# --- P31 über die Zuordnungstabelle ----------------------------------------------------
MAP_COLUMNS = ["oberbegriff", "anzahl", "anzahl_Q183", "anzahl_Q40", "anzahl_Q39", "qid", "wurzel", "alternativen",
               "freigegeben"]


def load_class_map(path: Path) -> tuple[dict[str, str], dict[str, str], dict[str, dict]]:
    """(Oberbegriff → Klassen-QID, Klassen-QID → Wurzel) nur für freigegebene Zeilen, dazu alle Zeilen."""
    if not path.exists():
        return {}, {}, {}
    with path.open(encoding="utf-8") as fh:
        rows = {r["oberbegriff"]: r for r in csv.DictReader(fh, delimiter=";")}
    ok = {t: r for t, r in rows.items()
          if r.get("qid") and r.get("wurzel") in GENERIC and r.get("freigegeben", "").strip().casefold() in ("ja", "x", "1")}
    return {t: r["qid"] for t, r in ok.items()}, {r["qid"]: r["wurzel"] for r in ok.values()}, rows


def write_class_map(path: Path, rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MAP_COLUMNS, delimiter=";")
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: -int(r.get("anzahl") or 0)):
            w.writerow({k: r.get(k, "") for k in MAP_COLUMNS})


def class_proposal(qid: str, entity: dict, b: GndBody, class_map: dict[str, str],
                   class_root: dict[str, str]) -> Proposal | None:
    """Erster GND-Oberbegriff mit freigegebener Klasse ersetzt die passende allgemeine P31-Aussage."""
    sts = [s for s in entity["claims"].get("P31", []) if s["rank"] != "deprecated"]
    if not sts or any(s["value"] not in GENERIC for s in sts):
        return None  # schon spezifisch oder gemischt (z. B. Gebäude + Museum) → nicht anfassen
    for term in b.broader:
        c = class_map.get(term)
        if not c:
            continue
        root = class_root.get(c)
        target = next((s for s in sts if s["value"] == root), None)
        if target is None:
            continue  # Oberbegriff passt nicht zur Art (z. B. Archiv-Begriff an einem Museum)
        if target["refs"] or target["quals"]:
            return Proposal(qid, "P31", c, f"{term}; {GENERIC[root]} bleibt (hat Fundstellen/Qualifikatoren)")
        return Proposal(qid, "P31", c, f"{term} ersetzt {GENERIC[root]}", remove=root)
    return None
