"""Personenabgleich Europeana-Agents ↔ Wikidata (ohne Netzzugriff, damit testbar).

Schlüssel: sameAs → QID (Europeana-Agents sind fast vollständig mit Wikidata verknüpft).
Die GND aus dem sameAs dient als Gegenprobe und als Quelle für fehlende P227.

P7704 (agent/base/<n>) je Wikidata-Item:
  identisch          Wikidata führt einen der Europeana-Agents dieses Items
  konflikt           Wikidata führt einen anderen Agent (P7704 ist einwertig) – nur Bericht
  kein_treffer       QID existiert nicht (mehr)
  kein_mensch        Item ist kein Mensch (P31 ≠ Q5) – nur Bericht
  mehrdeutig         Europeana-Dublette ohne klaren Favoriten, Agent hängt schon an einem
                     anderen Item oder die GND zeigt in Wikidata auf ein anderes Item
  fehlt_in_wikidata  → QuickStatement

P227 (GND) nur für sicher zugeordnete Menschen ohne P227, wenn Europeana genau eine GND
nennt, diese in Wikidata frei ist und lobid sie als aktuelle Personen-GND bestätigt.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import FEHLT, IDENTISCH, KEIN_TREFFER, KONFLIKT, MEHRDEUTIG, Finding, SourceRecord, WikidataItem

KEIN_MENSCH = "kein_mensch"
GND_PROBLEM = "gnd_problem"
HUMAN = "Q5"


def agent_value(rec: SourceRecord) -> str:
    return rec.ids["P7704"][0]


def choose_agent(recs: list[SourceRecord], counts: dict[str, int]) -> tuple[SourceRecord | None, str]:
    """Bei Europeana-Dubletten den Agent mit eindeutig den meisten verknüpften Objekten wählen."""
    if len(recs) == 1:
        return recs[0], ""
    ranked = sorted(recs, key=lambda r: counts.get(r.source_id, 0), reverse=True)
    detail = ", ".join(f"{agent_value(r)}: {counts.get(r.source_id, 0)} Objekte" for r in ranked)
    if counts.get(ranked[0].source_id, 0) > counts.get(ranked[1].source_id, 0):
        return ranked[0], f"Europeana-Dublette, gewählt nach verknüpften Objekten ({detail})"
    return None, f"Europeana-Dublette ohne klaren Favoriten ({detail}) – manuell wählen"


def classify_person(
    qid: str,
    recs: list[SourceRecord],
    present: set[str],
    item: WikidataItem | None,
    value_owner: dict[str, set[str]],
    gnd_owner: dict[str, set[str]],
    counts: dict[str, int] | None = None,
) -> tuple[Finding, SourceRecord | None]:
    """P7704-Befund für ein Wikidata-Item und die Europeana-Agents mit sameAs auf dieses Item.

    `present` = vorhandene agent/base/…-Werte des Items (ohne deprecated).
    Rückgabe: (Befund, zugeordneter Agent oder None).
    """
    counts = counts or {}
    ids = [agent_value(r) for r in recs]
    f = Finding("P7704", "", qid, "europeana", ",".join(r.source_id for r in recs), "|".join(ids), sorted(present))

    hit = [r for r in recs if agent_value(r) in present]
    if hit:
        f.status, f.source_id, f.source_value = IDENTISCH, hit[0].source_id, agent_value(hit[0])
        if len(recs) > 1:
            f.note = f"Europeana-Dublette: {', '.join(ids)}"
        return f, hit[0]
    if present:
        f.status = KONFLIKT
        f.note = f"Wikidata führt {', '.join(sorted(present))}, Europeana verknüpft {', '.join(ids)}"
        return f, None
    if item is None:
        f.status, f.note = KEIN_TREFFER, "QID existiert nicht (gelöscht?)"
        return f, None
    if HUMAN not in item.p31:
        f.status, f.note = KEIN_MENSCH, f"P31: {', '.join(item.p31) or '–'} – nur Bericht"
        return f, None

    rec, note = choose_agent(recs, counts)
    f.note = note
    if rec is None:
        f.status = MEHRDEUTIG
        return f, None
    value = agent_value(rec)
    f.source_id, f.source_value = rec.source_id, value

    others = value_owner.get(value, set()) - {qid}
    if others:
        f.status = MEHRDEUTIG
        f.note = _join(f.note, f"{value} hängt bereits an {', '.join(sorted(others))}")
        return f, None
    for g in rec.ids.get("P227", []):
        owners = gnd_owner.get(g, set())
        if owners and qid not in owners:
            f.status = MEHRDEUTIG
            f.note = _join(f.note, f"GND {g} gehört in Wikidata zu {', '.join(sorted(owners))}")
            return f, None

    wd_gnd = {s.value for s in item.values("P227")}
    eu_gnd = set(rec.ids.get("P227", []))
    if wd_gnd and eu_gnd and not wd_gnd & eu_gnd:
        f.note = _join(f.note, f"GND abweichend: Wikidata {', '.join(sorted(wd_gnd))}, "
                               f"Europeana {', '.join(sorted(eu_gnd))}")
    f.status, f.action = FEHLT, "new"
    return f, rec


def person_gnd(
    qid: str,
    rec: SourceRecord,
    item: WikidataItem | None,
    gnd_owner: dict[str, set[str]],
    problems: dict[str, str | None],
) -> Finding | None:
    """P227-Vorschlag für einen sicher zugeordneten Menschen ohne GND in Wikidata."""
    gnds = rec.ids.get("P227", [])
    if item is None or HUMAN not in item.p31 or item.values("P227") or not gnds:
        return None
    f = Finding("P227", "", qid, "europeana", rec.source_id, "|".join(gnds), [])
    if len(gnds) > 1:
        f.status, f.note = MEHRDEUTIG, "Europeana nennt mehrere GND"
        return f
    g = gnds[0]
    owners = gnd_owner.get(g, set()) - {qid}
    if owners:
        f.status, f.note = MEHRDEUTIG, f"GND bereits an {', '.join(sorted(owners))}"
        return f
    problem = problems.get(g, "GND nicht geprüft")
    if problem:
        f.status, f.note = GND_PROBLEM, problem
        return f
    f.status, f.action = FEHLT, "new"
    return f


@dataclass
class AgentCleanup:
    """Wikidata-Wert agent/base/<n>, den Europeana nicht (mehr) unter dieser Nummer führt."""

    qid: str
    value: str
    kind: str  # nicht_in_europeana | zusammengefuehrt
    new_value: str | None = None
    action: str | None = None  # delete | fix | None (nur Bericht)
    note: str = ""
    old_raw: str = ""  # Wert, wie er in Wikidata steht (für den Löschbefehl)


def agent_cleanup(qid: str, value: str, resolved: dict | None, has_refs: bool, has_qualifiers: bool,
                  value_owner: dict[str, set[str]]) -> AgentCleanup | None:
    """Bewertet einen Wikidata-Wert, dessen Nummer in der Europeana-Liste fehlt.

    `resolved` = Antwort von /entity/agent/<n>.json (None = 404). Europeana lenkt
    zusammengeführte Agents auf die neue ID um.
    """
    num = value.rsplit("/", 1)[-1]
    guarded = has_refs or has_qualifiers
    if resolved is None:
        c = AgentCleanup(qid, value, "nicht_in_europeana", note="Europeana: 404")
        if guarded:
            c.note += "; Aussage hat Quellen/Qualifier – manuell prüfen"
        else:
            c.action = "delete"
        return c
    new_num = (resolved.get("id") or "").rsplit("/", 1)[-1]
    if not new_num or new_num == num:
        return None  # existiert, fehlte nur in der Suchliste
    new_value = f"agent/base/{new_num}"
    c = AgentCleanup(qid, value, "zusammengefuehrt", new_value, note=f"Europeana leitet um auf {new_value}")
    others = value_owner.get(new_value, set()) - {qid}
    if others:
        c.note += f"; {new_value} hängt bereits an {', '.join(sorted(others))}"
    elif guarded:
        c.note += "; Aussage hat Quellen/Qualifier – manuell prüfen"
    else:
        c.action = "fix"
    return c


def _join(a: str, b: str) -> str:
    return "; ".join(x for x in (a, b) if x)
