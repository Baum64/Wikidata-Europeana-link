"""Gemeinsamer HTTP-Client mit Dateicache, Drosselung und Retry.

- Jede erfolgreiche Antwort wird als JSON-Datei unter cache/<host>/<hash>.json
  abgelegt. Der Hash wird aus Methode, URL, Parametern und Body gebildet –
  ohne geheime Parameter (wskey usw.), damit Schlüssel nie auf der Platte landen.
- HTTP 429 und 503: Retry-After wird respektiert, sonst exponentielles Backoff.
- Pro Host wird ein Mindestabstand zwischen Anfragen eingehalten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from . import config

log = logging.getLogger(__name__)

# Mindestabstand in Sekunden zwischen zwei Anfragen an denselben Host
MIN_INTERVAL = {
    "query.wikidata.org": 1.0,
    "api.europeana.eu": 0.1,
    "api.deutsche-digitale-bibliothek.de": 0.2,
    "isil.staatsbibliothek-berlin.de": 0.5,
    "museen-in-oesterreich.at": 0.5,
}


# Maximales Alter von Cache-Einträgen in Sekunden (None = unbegrenzt).
# Wikidata ändert sich durch eigene QuickStatements laufend → kurze Lebensdauer,
# sonst würden bereits eingespielte Vorschläge erneut erzeugt.
MAX_AGE = {"query.wikidata.org": config.WIKIDATA_CACHE_HOURS * 3600}


def cache_fresh(path, host: str) -> bool:
    max_age = MAX_AGE.get(host)
    return path.is_file() and (max_age is None or time.time() - path.stat().st_mtime < max_age)


def _redact(params: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (params or {}).items() if k not in config.SECRET_PARAMS}


class CachedClient:
    def __init__(self, use_cache: bool = True, max_retries: int = 5) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.user_agent()
        self.use_cache = use_cache
        self.max_retries = max_retries
        self._last_request: dict[str, float] = {}

    # --- Cache -----------------------------------------------------------------
    def _cache_path(self, method: str, url: str, params: dict | None, data: Any):
        key = json.dumps(
            [method, url, sorted(_redact(params).items()), data], sort_keys=True, default=str
        )
        digest = hashlib.sha256(key.encode()).hexdigest()
        host = urlsplit(url).netloc
        return config.CACHE_DIR / host / digest[:2] / f"{digest}.json"

    # --- Drosselung -----------------------------------------------------------
    def _throttle(self, host: str) -> None:
        interval = MIN_INTERVAL.get(host, 0.2)
        wait = self._last_request.get(host, 0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()

    # --- Abruf ----------------------------------------------------------------
    def request_json(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 90,
        allow_404: bool = False,
    ) -> Any:
        """Liefert die JSON-Antwort (aus dem Cache, falls vorhanden).

        Bei allow_404=True wird für 404 None zurückgegeben (und ebenfalls gecacht).
        """
        path = self._cache_path(method, url, params, data)
        host = urlsplit(url).netloc
        if self.use_cache and cache_fresh(path, host):
            return json.loads(path.read_text(encoding="utf-8"))["body"]

        hdrs = {"Accept": "application/json", **(headers or {})}
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            try:
                resp = self.session.request(
                    method, url, params=params, data=data, headers=hdrs, timeout=timeout
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise
                delay = 2**attempt
                log.warning("Netzwerkfehler bei %s (%s), neuer Versuch in %ss", url, exc, delay)
                time.sleep(delay)
                continue

            if resp.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
                log.warning("HTTP %s von %s, warte %ss", resp.status_code, host, delay)
                time.sleep(delay)
                continue
            break

        if resp.status_code == 404 and allow_404:
            body = None
        else:
            if not resp.ok:
                # URL ohne Query ausgeben, damit kein Schlüssel im Log landet
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} für {method} {url} "
                    f"params={_redact(params)}: {resp.text[:300]}",
                    response=resp,
                )
            body = resp.json()

        if self.use_cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"url": url, "params": _redact(params), "body": body}, ensure_ascii=False),
                encoding="utf-8",
            )
        return body

    def resolve(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Folgt keiner Weiterleitung, sondern liefert {status, location, body} (gecacht).

        Für Auflösungsdienste, die mit 300/301 antworten (z. B. Europeana /entity/resolve).
        """
        path = self._cache_path("RESOLVE", url, params, None)
        host = urlsplit(url).netloc
        if self.use_cache and cache_fresh(path, host):
            return json.loads(path.read_text(encoding="utf-8"))["body"]
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            resp = self.session.get(url, params=params, allow_redirects=False, timeout=60,
                                    headers={"Accept": "application/json"})
            if resp.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(2 ** (attempt + 1))
                continue
            break
        if resp.status_code >= 500 or resp.status_code in (401, 403, 429):
            raise requests.HTTPError(f"HTTP {resp.status_code} für {url} params={_redact(params)}", response=resp)
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        body = {"status": resp.status_code, "location": resp.headers.get("Location"), "body": payload}
        if self.use_cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"url": url, "params": _redact(params), "body": body}, ensure_ascii=False),
                            encoding="utf-8")
        return body

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kw) -> Any:
        return self.request_json("GET", url, params=params, **kw)

    def post_json(self, url: str, data: Any, **kw) -> Any:
        return self.request_json("POST", url, data=data, **kw)
