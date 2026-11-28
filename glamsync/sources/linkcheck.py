"""Linkprüfung für Websites (P856): Erreichbarkeit, Umleitungen, Park-/Fehlerseiten, Titel, Sprache.

- GET mit Umleitungen (höchstens MAX_REDIRECTS) und einer Stufe Meta-Refresh. Gelesen werden
  höchstens MAX_BYTES, das reicht für <title>, <html lang> und die Parkseiten-Merkmale.
- Neutraler User-Agent ohne Kontaktangabe: Die Anfragen gehen an die Websites der Einrichtungen,
  nicht an Wikimedia.
- Derselbe Host wird nacheinander abgefragt (Pause HOST_INTERVAL), verschiedene Hosts parallel.
- Zertifikatsfehler: zweiter Versuch ohne Zertifikatsprüfung, vermerkt als ssl_ok = False.
- Ergebnisse liegen unter cache/linkcheck/ (MAX_AGE_DAYS; unklare Ergebnisse nur UNCLEAR_AGE_HOURS).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from html import unescape
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

from .. import config

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; linkcheck/1.0)"
TIMEOUT = (8, 20)  # Verbindungsaufbau, Lesen (Sekunden)
MAX_BYTES = 300_000
MAX_REDIRECTS = 10
HOST_INTERVAL = 1.0
WORKERS = 16
MAX_AGE_DAYS = 7
UNCLEAR_AGE_HOURS = 24
CACHE = config.CACHE_DIR / "linkcheck"


@dataclass
class Check:
    url: str  # geprüfte Adresse (wie in der Quelle, ohne Leerzeichen am Rand)
    status: int | None = None
    final: str = ""  # Adresse nach allen Umleitungen
    error: str = ""  # dns | timeout | verbindung | ssl | umleitungen | ungueltig
    detail: str = ""
    title: str = ""
    lang: str = ""  # <html lang>, z. B. de-DE
    parked: bool = False  # Parkseite / Domain zu verkaufen / Platzhalter des Hosters
    soft404: bool = False  # Fehlerseite mit HTTP 200
    challenge: bool = False  # Bot-Schutz („Just a moment…“) – Inhalt nicht prüfbar
    refresh: bool = False  # einem Meta-Refresh gefolgt
    ssl_ok: bool = True

    @property
    def unclear(self) -> bool:
        if self.error:
            return self.error not in ("dns", "ungueltig")
        return self.challenge or self.status is None or (self.status >= 400 and self.status not in (404, 410))


# --- Seiteninhalt ---------------------------------------------------------------------
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_LANG = re.compile(r"<html\b[^>]*?\blang\s*=\s*[\"']?([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*)", re.I)
_META = re.compile(r"<meta\b[^>]*>", re.I)
_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", re.I)
PARKED = re.compile(
    r"domain (?:is )?(?:for sale|kaufen|zu verkaufen|steht zum verkauf)|sedoparking|sedo\.com/|parkingcrew|"
    r"this domain (?:has been|is|may be) (?:registered|parked|for sale)|domain parking|bodis\.com|"
    r"dan\.com/|hugedomains|afternic|mydomaincontact|fallback-term-link|"
    r"diese domain (?:wurde|ist) (?:reserviert|registriert)|webhosting.{0,40}reserviert|"
    r"domain (?:wurde )?(?:erfolgreich )?(?:registriert|reserviert)", re.I)
PLACEHOLDER_TITLE = re.compile(
    r"^(?:host europe|strato|ionos|1&1|all-inkl|domainfactory|hetzner|netcup|plesk|apache2? (?:ubuntu|debian)?\s*default"
    r"|it works|welcome to nginx|test page|default (?:web )?page|baustelle|under construction|coming soon)\b", re.I)
SOFT404_TITLE = re.compile(
    r"(?:^|\W)(?:404|seite nicht gefunden|page not found|nicht gefunden|not found|fehlerseite|"
    r"page introuvable|pagina non trovata|seite existiert nicht)(?:\W|$)", re.I)
CHALLENGE_TITLE = re.compile(r"^(?:just a moment|verifying your browser|attention required|access denied|"
                             r"ddos-guard|please wait)", re.I)


def _refresh_target(html: str) -> str:
    for tag in _META.findall(html[:20000]):
        if re.search(r"http-equiv\s*=\s*[\"']?refresh", tag, re.I):
            m = re.search(r"content\s*=\s*[\"']\s*(\d+)\s*[;,]\s*url\s*=\s*['\"]?([^'\">]+)", tag, re.I)
            if m and int(m.group(1)) <= 10:
                return unescape(m.group(2).strip())
    return ""


def parse_page(html: str) -> dict:
    """Titel, Sprache und Merkmale einer HTML-Seite (ohne Netz, getestet)."""
    t = _TITLE.search(html)
    title = re.sub(r"\s+", " ", unescape(t.group(1))).strip()[:200] if t else ""
    lg = _LANG.search(html[:20000])
    return {
        "title": title,
        "lang": lg.group(1).replace("_", "-") if lg else "",
        "parked": bool(PARKED.search(html[:150_000]) or PLACEHOLDER_TITLE.search(title)),
        "soft404": bool(SOFT404_TITLE.search(title)),
        "challenge": bool(CHALLENGE_TITLE.search(title)),
        "refresh": _refresh_target(html),
    }


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = m.group(1) if m else None
    if not enc:
        mm = _CHARSET.search(body[:4096])
        enc = mm.group(1).decode("ascii", "ignore") if mm else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def with_scheme(u: str) -> str:
    u = u.strip()
    return u if re.match(r"^https?://", u, re.I) else "http://" + u


def _error_kind(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "umleitungen"
    if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
                        requests.exceptions.MissingSchema, urllib3.exceptions.LocationParseError)):
        return "ungueltig"
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl"
    if re.search(r"NameResolution|Name or service not known|nodename nor servname|getaddrinfo failed|"
                 r"No address associated|Temporary failure in name resolution", text):
        return "dns"
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in text:
        return "timeout"
    return "verbindung"


def _get(session: requests.Session, url: str, verify: bool) -> tuple[requests.Response, bytes]:
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True, verify=verify)
    body = b""
    try:
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) >= MAX_BYTES:
                break
    finally:
        r.close()
    return r, body


def fetch(url: str, session: requests.Session | None = None) -> Check:
    """Ruft eine Adresse ab (mit Umleitungen und einer Stufe Meta-Refresh)."""
    session = session or _session()
    c = Check(url=url.strip())
    target = with_scheme(url)
    for hop in range(2):  # zweiter Durchlauf nur für Meta-Refresh
        resp = body = None
        for verify in (True, False):
            try:
                resp, body = _get(session, target, verify)
                c.ssl_ok = c.ssl_ok and verify
                break
            except requests.exceptions.SSLError as exc:
                c.error, c.detail = "ssl", str(exc)[:200]
            except (requests.RequestException, urllib3.exceptions.HTTPError, ValueError) as exc:
                c.error, c.detail = _error_kind(exc), str(exc)[:200]
                return c
        if resp is None:
            return c
        c.error = c.detail = ""
        c.status, c.final = resp.status_code, resp.url
        ctype = resp.headers.get("content-type", "")
        page = parse_page(_decode(body, ctype)) if "html" in ctype.lower() or body[:200].lstrip().startswith(b"<") else {}
        c.title, c.lang = page.get("title", ""), page.get("lang", "")
        c.parked, c.soft404, c.challenge = page.get("parked", False), page.get("soft404", False), page.get("challenge", False)
        nxt = page.get("refresh")
        if hop == 0 and nxt and resp.ok:
            nxt = urljoin(resp.url, nxt)
            if nxt.rstrip("/") != resp.url.rstrip("/"):
                c.refresh, target = True, nxt
                continue
        break
    return c


def _session() -> requests.Session:
    s = requests.Session()
    s.max_redirects = MAX_REDIRECTS
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                      "Accept-Language": "de,en;q=0.7,fr;q=0.5,it;q=0.5"})
    return s


# --- Cache und Ablauf -------------------------------------------------------------------
def _cache_path(url: str):
    digest = hashlib.sha256(url.strip().encode()).hexdigest()
    return CACHE / digest[:2] / f"{digest}.json"


def _cached(url: str) -> Check | None:
    p = _cache_path(url)
    if not p.is_file():
        return None
    c = Check(**json.loads(p.read_text(encoding="utf-8")))
    age = time.time() - p.stat().st_mtime
    return c if age < (UNCLEAR_AGE_HOURS * 3600 if c.unclear else MAX_AGE_DAYS * 86400) else None


def _store(c: Check) -> None:
    p = _cache_path(c.url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(c), ensure_ascii=False), encoding="utf-8")


def check_urls(urls, use_cache: bool = True, workers: int = WORKERS) -> dict[str, Check]:
    """Prüft alle Adressen; Schlüssel ist die Adresse ohne Leerzeichen am Rand."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    urls = sorted({u.strip() for u in urls if u and u.strip()})
    out: dict[str, Check] = {}
    by_host: dict[str, list[str]] = defaultdict(list)
    for u in urls:
        c = _cached(u) if use_cache else None
        if c:
            out[u] = c
        else:
            by_host[(urlsplit(with_scheme(u)).hostname or "").lower()].append(u)
    todo = sum(len(v) for v in by_host.values())
    log.info("Linkprüfung: %d Adressen, davon %d aus dem Cache, %d abzurufen (%d Hosts)",
             len(urls), len(out), todo, len(by_host))
    lock = threading.Lock()
    done = [0]

    def run(host_urls: list[str]) -> None:
        session = _session()
        for i, u in enumerate(host_urls):
            if i:
                time.sleep(HOST_INTERVAL)
            c = fetch(u, session)
            _store(c)
            with lock:
                out[u] = c
                done[0] += 1
                if done[0] % 200 == 0:
                    log.info("Linkprüfung: %d/%d", done[0], todo)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run, by_host.values()))
    return out
