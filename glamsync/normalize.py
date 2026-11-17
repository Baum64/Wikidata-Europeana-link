"""Normalisierung von Identifikatoren (ID-Formate wie in Wikidata erwartet).

Jede normalize_*-Funktion nimmt einen Rohwert (auch URIs) und liefert den
kanonischen Wikidata-Wert oder None, wenn der Wert ungültig ist.
`check(prop, raw)` bewertet zusätzlich einen bereits in Wikidata stehenden Wert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Wikidata-QID
# ---------------------------------------------------------------------------
_QID_RE = re.compile(r"^(?:https?://(?:www\.)?wikidata\.org/(?:entity|wiki)/)?(Q[1-9]\d*)/?$", re.I)


def qid_from_uri(value: str | None) -> str | None:
    """'http://www.wikidata.org/entity/Q123' → 'Q123'."""
    if not value:
        return None
    m = _QID_RE.match(value.strip())
    return m.group(1).upper() if m else None


# ---------------------------------------------------------------------------
# P213 ISNI: 16 Zeichen, Prüfziffer ISO 7064 MOD 11-2.
# Wikidata speichert ISNI OHNE Leerzeichen (Format-Constraint 0{7}\d{8}[\dX];
# Stand 15.11.2026: 2.583.174 Werte, davon nur 138 mit Leerzeichen).
# Die gruppierte Schreibweise gibt isni_display() aus.
# ---------------------------------------------------------------------------
def _isni_checksum_ok(digits: str) -> bool:
    total = 0
    for ch in digits[:15]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return digits[15] == ("X" if check == 10 else str(check))


def normalize_isni(value: str | None) -> str | None:
    """'http://isni.org/isni/000000012097381X' bzw. '0000 0001 2097 381X' → '000000012097381X'."""
    if not value:
        return None
    v = value.strip()
    v = re.sub(r"^https?://(?:www\.)?isni(?:\.oclc)?\.org/(?:isni/)?", "", v, flags=re.I)
    digits = re.sub(r"[\s\-]", "", v).upper()
    if not re.fullmatch(r"\d{15}[\dX]", digits) or not _isni_checksum_ok(digits):
        return None
    return digits


def isni_display(value: str) -> str:
    """Lesbare Form in 4er-Gruppen: '0000 0001 2097 381X'."""
    return " ".join(value[i : i + 4] for i in range(0, 16, 4))


# ---------------------------------------------------------------------------
# P791 ISIL
# ---------------------------------------------------------------------------
# Ländercode (2 Buchstaben) oder Sonderpräfix (bis 4 Zeichen), Bindestrich, max. 11 Zeichen.
_ISIL_RE = re.compile(r"^([A-Za-z]{1,4})-([A-Za-z0-9:/\-]{1,11})$")
_ISIL_URI_PREFIX = re.compile(
    r"^https?://(?:ld\.zdb-services\.de/resource/organisations/|sigel\.staatsbibliothek-berlin\.de/"
    r"(?:resource/organisations/)?|isil\.org/|lobid\.org/organisations/)",
    re.I,
)


def normalize_isil(value: str | None) -> str | None:
    """Ländercode bzw. Präfix (und 'MUS'-Teil) groß, Rest unverändert.

    ISIL sind nach ISO 15511 nicht case-sensitiv, deutsche ISIL werden aber
    in gemischter Schreibung geführt (z. B. 'DE-Ks18'). Daher wird nur der
    Präfix vereinheitlicht; verglichen wird mit isil_key().
    """
    if not value:
        return None
    v = _ISIL_URI_PREFIX.sub("", value.strip()).strip("/").split("#")[0]
    m = _ISIL_RE.match(v)
    if not m:
        return None
    prefix, rest = m.group(1).upper(), m.group(2)
    if re.match(r"^mus(?:-|$)", rest, re.I):
        # Museums-ISIL: DE-MUS-<Ziffern>; 'DE-MUS' allein oder ohne Nummer ist ungültig
        if not re.fullmatch(r"(?i)mus-\d+", rest):
            return None
        rest = "MUS-" + rest[4:]
    return f"{prefix}-{rest}"


def isil_key(value: str) -> str:
    """Vergleichsschlüssel für ISIL (Groß-/Kleinschreibung egal)."""
    return value.upper()


# ---------------------------------------------------------------------------
# P227 GND. Wikidata-Constraint (Stand 15.11.2026): 1[01234]?\d{7}[0-9X]|… – der Nummernkreis
# wächst laufend (14…), daher hier bewusst 1\d?\d{7}[0-9X], damit gültige neue GND nie als
# „ungültig“ zur Löschung vorgeschlagen werden.
# ---------------------------------------------------------------------------
_GND_RE = re.compile(r"^(?:1\d?\d{7}[0-9X]|[47]\d{6}-\d|[1-9]\d{0,7}-[0-9X]|3\d{7}[0-9X])$")


def normalize_gnd(value: str | None) -> str | None:
    """'https://d-nb.info/gnd/2022341-9' → '2022341-9'."""
    if not value:
        return None
    v = re.sub(r"^https?://d-nb\.info/gnd/", "", value.strip(), flags=re.I).strip("/")
    v = v.split("/")[0].upper()
    return v if _GND_RE.match(v) else None


# ---------------------------------------------------------------------------
# P214 VIAF
# ---------------------------------------------------------------------------
_VIAF_RE = re.compile(r"^[1-9]\d(?:\d{0,7}|\d{17,20})$")


def normalize_viaf(value: str | None) -> str | None:
    """'http://viaf.org/viaf/148328332' → '148328332'."""
    if not value:
        return None
    v = re.sub(r"^https?://(?:www\.)?viaf\.org/viaf/", "", value.strip(), flags=re.I).strip("/")
    v = v.split("/")[0]
    return v if _VIAF_RE.match(v) else None


# ---------------------------------------------------------------------------
# P7704 Europeana entity
# ---------------------------------------------------------------------------
# 'organization/<n>' (amerikanisch) löst bei Europeana ebenso auf wie 'organisation/<n>' und gilt
# daher als korrekt – dafür wird keine Korrektur vorgeschlagen.
_P7704_OK = re.compile(r"^(?:organi[sz]ation/\d+|(?:agent|place|concept|timespan)/base/\d+)$")
# Ab dieser Länge gilt eine Organisations-ID als alte CRM-ID (z. B. 1482250000004502115)
LEGACY_ORG_ID_MIN_LEN = 13


def normalize_p7704(value: str | None) -> str | None:
    """Kanonische P7704-Form oder None.

    'organization/123'                              → 'organisation/123'
    'http://data.europeana.eu/organization/123'     → 'organisation/123'
    'http://data.europeana.eu/agent/base/1' bzw. 'agent/1' → 'agent/base/1'
    Objekt-IDs ('item/…', '90402/…' = P727), reine Zahlen u. Ä. → None
    """
    if not value:
        return None
    v = value.strip()
    v = re.sub(r"^https?://(?:data|www|api)\.europeana\.eu/(?:entity/)?", "", v, flags=re.I)
    v = v.split("?")[0].split("#")[0]
    v = re.sub(r"\.json(?:ld)?$", "", v).strip("/")
    # Portal-URLs: [<sprache>/]collections/<typ>/<n>[-slug]
    m = re.fullmatch(r"(?i)(?:[a-z]{2}/)?collections/(organi[sz]ation|person|place|topic)/(\d+)(?:-[^/]*)?", v)
    if m:
        kind = {"person": "agent", "topic": "concept"}.get(m.group(1).lower(), m.group(1).lower())
        v = f"{kind}/{m.group(2)}"
    m = re.fullmatch(r"(?i)organi[sz]ation/(\d+)", v)
    if m:
        return f"organisation/{m.group(1)}"
    m = re.fullmatch(r"(?i)(agent|place|concept|timespan)/(?:base/)?(\d+)", v)
    if m:
        return f"{m.group(1).lower()}/base/{m.group(2)}"
    return None


def p7704_problem(value: str) -> str | None:
    """Erläuterung, warum ein P7704-Wert falsch ist (für den Bericht)."""
    v = value.strip()
    if _P7704_OK.match(v):
        n = v.rsplit("/", 1)[-1]
        if v.startswith("organi") and len(n) >= LEGACY_ORG_ID_MIN_LEN:
            return "veraltete Organisations-ID (CRM)"
        return None
    if v.lower().startswith("http"):
        return "volle URL statt ID"
    if v.startswith("collections/"):
        return "Portal-Pfad statt ID"
    if re.match(r"^(?:[a-z]{2}/)?item/", v) or re.match(r"^\d+/", v):
        return "Objekt-ID (gehört zu P727 Europeana ID)"
    if re.fullmatch(r"\d+", v):
        return "Wert ohne Präfix"
    return "unbekanntes Format"


# ---------------------------------------------------------------------------
# P7917 Museums in Austria Code: 'M' + 5 Ziffern
# ---------------------------------------------------------------------------
def normalize_mure(value: str | None) -> str | None:
    """' m10072 ' → 'M10072'; alles andere → None."""
    if not value:
        return None
    v = value.strip().upper()
    return v if re.fullmatch(r"M\d{5}", v) else None


# ---------------------------------------------------------------------------
# Generische Schnittstelle
# ---------------------------------------------------------------------------
NORMALIZERS = {
    "P7704": normalize_p7704,
    "P791": normalize_isil,
    "P227": normalize_gnd,
    "P214": normalize_viaf,
    "P213": normalize_isni,
}

PROPERTIES = list(NORMALIZERS)  # Properties des Europeana/DDB-Abgleichs
ALL_NORMALIZERS = {**NORMALIZERS, "P7917": normalize_mure}  # inkl. Registerabgleich


def normalize(prop: str, value: str | None) -> str | None:
    return ALL_NORMALIZERS[prop](value)


def compare_key(prop: str, value: str) -> str:
    """Schlüssel für den Gleichheitsvergleich normalisierter Werte."""
    return isil_key(value) if prop == "P791" else value


@dataclass(frozen=True)
class Check:
    raw: str
    normalized: str | None  # None = nicht reparierbar
    format_ok: bool  # raw entspricht bereits der kanonischen Form
    problem: str | None = None


def check(prop: str, raw: str) -> Check:
    """Bewertet einen Wikidata-Wert: korrekt, reparierbarer Formatfehler oder ungültig."""
    norm = normalize(prop, raw)
    if prop == "P7704":
        problem = p7704_problem(raw)
        # gültig, wenn kein Problem vorliegt (auch 'organization/<n>', normalisiert nur für den Vergleich)
        return Check(raw, norm, norm is not None and problem is None, problem)
    if norm is None:
        return Check(raw, None, False, "ungültiges Format")
    if norm == raw:
        return Check(raw, norm, True)
    return Check(raw, norm, False, f"Formatfehler, korrekt: {norm}")
