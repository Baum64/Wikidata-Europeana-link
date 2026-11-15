"""Datenmodelle für Quell-Datensätze, Wikidata-Items und Abgleichsergebnisse."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SourceRecord:
    """Eine Einrichtung aus Europeana oder DDB mit normalisierten Identifikatoren."""

    source: str  # "europeana" | "ddb"
    source_id: str  # z. B. "4373" (Europeana) oder DDB-ID
    label: str
    url: str
    country: str | None = None
    sector: str | None = None  # DDB-Sparte (sec_01 …)
    qids: list[str] = field(default_factory=list)  # Wikidata-QIDs aus sameAs
    # Identifikatoren je Property (normalisierte Werte, ggf. mehrere)
    ids: dict[str, list[str]] = field(default_factory=dict)
    legacy_p7704: list[str] = field(default_factory=list)  # alte Europeana-IDs aus sameAs

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceRecord":
        return cls(**d)


@dataclass
class Statement:
    """Eine Wikidata-Aussage für eine der Ziel-Properties."""

    value: str
    statement_id: str  # z. B. "Q1$ABC-…"
    rank: str  # normal | preferred | deprecated
    has_refs: bool
    has_qualifiers: bool


@dataclass
class WikidataItem:
    qid: str
    labels: dict[str, str] = field(default_factory=dict)  # de/en
    p31: list[str] = field(default_factory=list)
    p17: list[str] = field(default_factory=list)
    claims: dict[str, list[Statement]] = field(default_factory=dict)

    def values(self, prop: str, include_deprecated: bool = False) -> list[Statement]:
        return [s for s in self.claims.get(prop, []) if include_deprecated or s.rank != "deprecated"]

    @property
    def label(self) -> str:
        return self.labels.get("de") or self.labels.get("en") or ""


# Klassifikationen je Property
FEHLT = "fehlt_in_wikidata"
IDENTISCH = "identisch"
FORMATFEHLER = "formatfehler"
KONFLIKT = "konflikt"
MEHRDEUTIG = "mehrdeutig"
KEINE_QUELLE = "keine_quelle"  # Quelle liefert keinen Wert für diese Property
KEIN_TREFFER = "kein_treffer"  # Einrichtung nicht in Wikidata gefunden


@dataclass
class Finding:
    """Ergebnis des Vergleichs einer Property für ein (Einrichtung, QID)-Paar."""

    prop: str
    status: str
    qid: str | None
    source: str  # europeana | ddb | wikidata (reine Formatprüfung)
    source_id: str
    source_value: str | None = None
    wikidata_values: list[str] = field(default_factory=list)
    # Für Korrekturen: der zu ersetzende Rohwert und ob er automatisch ersetzt werden darf
    old_value: str | None = None
    action: str | None = None  # "new" | "fix" | None (nur Bericht)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
