"""Sprachprüfung für deutsche Aliase über die Hunspell-Wörterbücher des Systems.

Ein Alias gilt als fremdsprachig, wenn er
  - kein Wort enthält, das nur im deutschen Wörterbuch steht („Stadtarchiv“, „der“), und
  - mindestens ein Wort enthält, das fremdsprachig, aber nicht deutsch ist („Jewish“, „Archives“,
    „Bibliothèque“, „Muzeum“),
  - oder überwiegend in nichtlateinischer Schrift geschrieben ist.

Wörter, die in beiden stehen („Museum“, „Galerie“), entscheiden nichts. Unbekannte Wörter
(Eigennamen wie „Folkwang“) ebenso – „Museum Folkwang“ bleibt deshalb deutsch. Abkürzungen in
Großbuchstaben und Einzelbuchstaben werden übergangen; ein Wort aus einem fremden Wörterbuch zählt
nur, wenn es dort kleingeschrieben steht (sonst ist es ein Eigenname wie „Selma“).

„Fremdsprachig“ heißt: in einem installierten fremden Hunspell-Wörterbuch (derzeit meist nur
Englisch), in der Liste typischer Wörter unten oder mit Buchstaben außerhalb des deutschen
Alphabets (é, ł, ř, å …). Geprüft wird stapelweise (`hunspell -G`), weil der Einzelmodus für jedes
unbekannte Wort Korrekturvorschläge berechnet und dadurch sehr langsam ist.
Ohne Hunspell prüft `foreign_reason` nur Schrift, Wortliste und Buchstaben.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

GERMAN = ("de_DE", "de_AT", "de_CH")
OTHER = ("en_US", "en_GB", "fr_FR", "it_IT", "es_ES", "nl_NL", "pl_PL", "cs_CZ", "sv_SE", "da_DK", "pt_PT")
HUNSPELL_DIR = Path("/usr/share/hunspell")
_WORD = re.compile(r"[^\W\d_]+")
# Ergänzung für Sprachen ohne installiertes Wörterbuch: typische Wörter in GLAM-Namen
_FOREIGN_WORDS = {
    "of", "the", "and", "for",  # en
    # englische Exonyme/Adjektive (im Wörterbuch nur großgeschrieben = wie Eigennamen, s. foreign_reason)
    "munich", "nuremberg", "cologne", "hanover", "brunswick", "jewish", "german", "bavarian", "franconian",
    "swabian", "saxon", "saxony", "prussian", "thuringian", "hessian", "westphalian", "silesian",
    "le", "la", "les", "du", "des", "de", "et", "musée", "musee", "bibliothèque", "bibliotheque", "archives",
    "municipale", "municipales", "départementales", "nationale", "centre",  # fr
    "il", "lo", "di", "del", "della", "dei", "degli", "museo", "musei", "biblioteca", "archivio", "civico",
    "civica", "nazionale",  # it
    "el", "los", "las", "y", "archivo", "nacional",  # es
    "het", "en", "van", "voor", "rijksmuseum", "stichting", "gemeentearchief",  # nl
    "muzeum", "biblioteka", "archiwum", "narodowe", "miejskie", "im",  # pl
    "knihovna", "národní", "státní", "okresní",  # cs
    "museu", "arquivo", "do", "da",  # pt
    "museet", "biblioteket", "arkiv", "och",  # sv/da
}
_GERMAN_LETTERS = set("abcdefghijklmnopqrstuvwxyzäöüß")

_known: dict[tuple[str, str], bool] = {}  # (Wörterbuch, Wort) → bekannt


def _dicts(names: Iterable[str]) -> list[str]:
    """Nur installierte Wörterbücher (Verknüpfungen ins Leere zählen nicht)."""
    if not shutil.which("hunspell"):
        return []
    return [d for d in names if (HUNSPELL_DIR / f"{d}.aff").exists() and (HUNSPELL_DIR / f"{d}.dic").exists()]


def _charset(dic: str) -> str:
    m = re.search(r"^SET\s+(\S+)", (HUNSPELL_DIR / f"{dic}.aff").read_text(encoding="latin-1"), re.M)
    return (m.group(1) if m else "ISO8859-1").replace("ISO8859", "iso8859")


def _variants(word: str) -> set[str]:
    return {word, word.lower(), word.capitalize()}


def prime(words: Iterable[str]) -> None:
    """Prüft alle noch unbekannten Wörter in einem Durchlauf je Wörterbuch."""
    words = {v for w in words for v in _variants(w)}
    for dic in _dicts(GERMAN + OTHER):
        todo = sorted(w for w in words if (dic, w) not in _known)
        if not todo:
            continue
        cs = _charset(dic)
        ok_enc = []
        for w in todo:
            try:
                w.encode(cs)
                ok_enc.append(w)
            except (UnicodeEncodeError, LookupError):
                _known[(dic, w)] = False  # im Zeichensatz des Wörterbuchs nicht darstellbar
        if not ok_enc:
            continue
        res = subprocess.run(["hunspell", "-d", dic, "-G", "-i", "utf-8"], input="\n".join(ok_enc) + "\n",
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        good = set(res.stdout.splitlines())
        for w in ok_enc:
            _known[(dic, w)] = w in good


def _known_in(word: str, dics: tuple[str, ...], lower_only: bool = False) -> bool:
    """Wort (in einer Schreibvariante) im Wörterbuch; `lower_only`: nur die Kleinschreibung zählt –
    so gelten Eigennamen im Wörterbuch („Selma“, „Alf“) nicht als fremdsprachige Wörter."""
    installed = _dicts(dics)
    variants = {word.lower()} if lower_only else _variants(word)
    if any((d, v) not in _known for d in installed for v in variants):
        prime([word])
    return any(_known.get((d, v), False) for d in installed for v in variants)


def _non_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    latin = sum(1 for c in letters if "LATIN" in unicodedata.name(c, ""))
    return latin / len(letters) < 0.5


def foreign_reason(alias: str) -> str | None:
    """Begründung, warum der Alias nicht deutsch ist, oder None. Vorher `prime` für viele Aliase."""
    if _non_latin(alias):
        return "nichtlateinische Schrift"
    german_only, foreign_only = [], []
    for w in _WORD.findall(alias):
        if len(w) < 2 or w.isupper():
            continue  # Einzelbuchstaben und Abkürzungen („UB“, „SLUB“) sagen nichts über die Sprache
        de = _known_in(w, GERMAN)
        listed = w.casefold() in _FOREIGN_WORDS or not set(w.casefold()) <= _GERMAN_LETTERS
        # rein deutsch: in keinem fremden Wörterbuch, auch nicht als Name („Berlin“ ist neutral)
        if de and not listed and not _known_in(w, OTHER):
            german_only.append(w)
        # fremdsprachig: dort als gewöhnliches (kleingeschriebenes) Wort, nicht nur als Name („Selma“)
        elif not de and (listed or _known_in(w, OTHER, lower_only=True)):
            foreign_only.append(w)
    if german_only or not foreign_only:
        return None
    return "kein deutsches Wort; fremdsprachig: " + ", ".join(foreign_only)


def words_of(texts: Iterable[str]) -> set[str]:
    return {w for t in texts for w in _WORD.findall(t)}
