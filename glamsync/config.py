"""Zentrale Konfiguration: Umgebungsvariablen, Pfade, Konstanten.

API-Schlüssel werden ausschließlich aus Umgebungsvariablen (bzw. einer lokalen
.env-Datei) gelesen und niemals ausgegeben oder geloggt.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"
DATA_DIR = PROJECT_ROOT / "data"
OUT_DIR = PROJECT_ROOT / "out"

# Parameternamen, deren Werte geheim sind: Sie werden aus Cache-Schlüsseln und
# Logausgaben entfernt.
SECRET_PARAMS = frozenset({"wskey", "oauth_consumer_key", "api_key", "apikey"})


def load_dotenv(path: Path | None = None) -> None:
    """Liest eine einfache KEY=VALUE-.env-Datei, ohne bestehende Variablen zu überschreiben."""
    path = path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


load_dotenv()

EUROPEANA_API_KEY: str | None = os.environ.get("EUROPEANA_API_KEY") or None
# Für die öffentlichen DDB-Endpunkte (Institutionen, Items, Suche) ist kein Schlüssel nötig.
DDB_API_KEY: str | None = os.environ.get("DDB_API_KEY") or None
REPOSITORY = "https://github.com/Baum64/Wikidata-Europeana-link"
# Kontaktangabe für den User-Agent (Wikimedia-Policy): URL oder E-Mail; Standard: Repository.
CONTACT: str = os.environ.get("GLAMSYNC_CONTACT", "").strip() or REPOSITORY

# Lebensdauer des Wikidata-Caches (HTTP-Cache und data/wikidata_*.json) in Stunden
WIKIDATA_CACHE_HOURS: float = float(os.environ.get("GLAMSYNC_WIKIDATA_CACHE_HOURS", "12"))

VERSION = "0.1.0"


def user_agent() -> str:
    contact = f" ({CONTACT})" if CONTACT else ""
    return f"glamsync/{VERSION}{contact} python-requests"


# Endpunkte
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
EUROPEANA_ENTITY = "https://api.europeana.eu/entity"
DDB_API = "https://api.deutsche-digitale-bibliothek.de/2"
