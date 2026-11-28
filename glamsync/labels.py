"""mul- und englische Labels für Museen, Bibliotheken und Archive ohne mul- und ohne en-Label
(ohne Netzzugriff, damit testbar).

Solche Items erscheinen in der englischen Ansicht und in allen Sprachen ohne eigenes Label nur
als QID. Vorgeschlagen werden nur vorhandene Namen, nie eigene Übersetzungen:

  Lmul  das vorhandene Label in einer Landessprache des Items (Amtssprachen des Staates, P37,
        dazu Regionalsprachen, siehe REGIONAL). Mehrere verschiedene Namen in Landessprachen
        (Belgien nl/fr) → nur Hinweis. Nichtlateinische Schrift zählt nur, wenn es keinen
        lateinischen Namen gibt, und dann nur als Hinweis (mul hilft lateinisch Lesenden wenig).
  Len   der offizielle englische Name (P1448, Sprache en), sonst der englische Name der
        verknüpften Europeana-Organisation (nur, wenn Europeana auch einen Namen in der
        Landessprache führt oder der Name schon englischer Alias ist). Nicht, wenn er nur eine
        Schreibvariante des Landesnamens ist oder ihn mit Zusatz enthält (halbe Übersetzung).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .lang import _non_latin
from .orgs import Org, name_check, norm_name
from .registry import name_similarity

TERM_MAX = 250  # Wikidata-Grenze für Labels
EN_VARIANT = 0.95  # ab dieser Ähnlichkeit ist der englische Name nur eine Schreibvariante

# Staaten, deren Einrichtungen P17 = Landesteil tragen (s. Wikidata.EUROPE_EXTRA)
EXTRA_LANGS = {"Q35": {"da"}, "Q55": {"nl"}}
# Regional- und Minderheitensprachen: ein Label nur in dieser Sprache ist meist der eigentliche Name
REGIONAL = {
    "Q29": {"ca", "eu", "gl", "oc", "ast", "an"},  # Spanien
    "Q38": {"de", "fr", "sl", "lld", "fur", "sc"},  # Italien (Südtirol, Aostatal …)
    "Q142": {"br", "oc", "eu", "co", "ca", "gsw"},  # Frankreich
    "Q145": {"cy", "gd", "ga"},  # Vereinigtes Königreich
    "Q183": {"hsb", "dsb", "fy", "frr", "stq", "nds"},  # Deutschland
    "Q55": {"fy", "nds", "li"}, "Q29999": {"fy", "nds", "li"},  # Niederlande
    "Q35": {"fo"}, "Q756617": {"fo"},  # Dänemark (Färöer)
    "Q20": {"se"}, "Q34": {"se", "fi"}, "Q33": {"se"},  # Norwegen, Schweden, Finnland
    "Q40": {"sl", "hr", "hu"},  # Österreich
    "Q36": {"csb", "szl", "de"},  # Polen
    "Q217": {"hu", "de"}, "Q214": {"hu"}, "Q403": {"hu"},  # Rumänien, Slowakei, Serbien
}


def country_langs(official: dict[str, set[str]], countries) -> set[str]:
    """Sprachcodes (Grundform ohne Variante) der Staaten eines Items. `no` steht für nb/nn."""
    out: set[str] = set()
    for c in countries:
        out |= official.get(c, set()) | EXTRA_LANGS.get(c, set()) | REGIONAL.get(c, set())
    if "no" in out:
        out |= {"nb", "nn"}
    return out


def _base(lang: str) -> str:
    return lang.split("-", 1)[0]


def _term_ok(v: str) -> bool:
    return bool(v) and len(v) <= TERM_MAX and not any(c in v for c in '"\t\n\r')


_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2028-\u202e\u2060\ufeff]")
_ENTITY = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")


def _clean(v: str) -> str:
    """Geschützte Leerzeichen (z. B. polnisch „w\u00a0Elblągu“) → normales Leerzeichen."""
    return v.replace("\u00a0", " ")


def label_defect(v: str) -> str:
    """Grund, warum ein vorhandenes Label nicht als mul übernommen werden sollte (erst korrigieren),
    sonst ''."""
    if _INVISIBLE.search(v):
        return "unsichtbares Zeichen"
    if _ENTITY.search(v):
        return "HTML-Entity"
    if v != v.strip() or "  " in v:
        return "Leerzeichen doppelt/am Rand"
    if v.count("(") != v.count(")") or "[" in v or "]" in v:
        return "Klammern"
    if re.search(r"[,;:/(\-–—]$", v):
        return "Satzzeichen am Ende"
    return ""


@dataclass
class LabelProposal:
    qid: str
    countries: list[str] = field(default_factory=list)
    labels_now: dict[str, str] = field(default_factory=dict)
    europeana: list[str] = field(default_factory=list)
    mul: str = ""
    en: str = ""
    en_source: str = ""  # P1448 | europeana
    note: str = ""


def _native(labels: dict[str, str], langs: set[str]) -> tuple[str, list[str]]:
    """(mul-Wert, Hinweise) aus den Labels in Landessprachen."""
    native = {norm_name(v): v for lang, v in labels.items() if _base(lang) in langs}
    latin = {k: v for k, v in native.items() if not _non_latin(v)}
    if len(latin) == 1:
        v = _clean(next(iter(latin.values())))
        if not _term_ok(v):
            return "", [f"Name für QuickStatements ungeeignet (Anführungszeichen/Länge): {v}"]
        if defect := label_defect(v):
            return "", [f"Label fehlerhaft ({defect}) – erst korrigieren: {v!r}"]
        return v, []
    if len(latin) > 1:
        return "", ["mehrere Namen in Landessprachen: " + " | ".join(sorted(latin.values()))]
    if native:
        return "", ["nichtlateinische Schrift (" + " | ".join(native.values()) + ") – mul von Hand prüfen"]
    return "", ["kein Label in einer Landessprache (" + ", ".join(sorted(labels)) + ")"]


def _europeana_en(orgs: list[Org], t: dict) -> tuple[str, str]:
    """(englischer Name, Hinweis) der verknüpften Europeana-Organisationen."""
    wd_names = list(t["labels"].values()) + [a for v in t.get("aliases", {}).values() for a in v]
    ens = {norm_name(o.pref["en"]): o.pref["en"] for o in orgs if o.pref.get("en")}
    if not ens:
        return "", ""
    if not any(name_check(o, wd_names)[0] for o in orgs):
        return "", "Europeana-Verknüpfung: kein Name gleicht einem Label/Alias – prüfen"
    if len(ens) > 1:
        return "", "mehrere englische Europeana-Namen: " + " | ".join(ens.values())
    en = next(iter(ens.values()))
    has_native = any(lang != "en" for o in orgs for lang in o.pref)
    en_alias = norm_name(en) in {norm_name(a) for a in t.get("aliases", {}).get("en", [])}
    if has_native or en_alias:
        return en, ""
    if any(norm_name(en) == norm_name(v) for v in t["labels"].values()):
        return "", ""  # nur falsch als „en“ ausgezeichneter Landesname
    return "", f"Europeana nur englisch („{en}“), unklar ob Übersetzung – en nicht gesetzt"


def label_proposals(items: dict[str, dict], official: dict[str, set[str]],
                    europeana: dict[str, list[Org]] | None = None) -> list[LabelProposal]:
    """items: QID → {labels: {sprache: label}, aliases: {sprache: [alias]}, countries: [QID],
    official_en: [Name]} (live). official: Staat → Amtssprachen (P37/P424).
    europeana: QID → verknüpfte Europeana-Organisationen. Liefert Items mit Vorschlag oder Hinweis."""
    europeana = europeana or {}
    out = []
    for q in sorted(items, key=lambda x: int(x[1:])):
        t = items[q]
        labels = t["labels"]
        if "mul" in labels or "en" in labels:
            continue
        orgs = sorted(europeana.get(q, []), key=lambda o: (-o.record_count, o.num))
        p = LabelProposal(q, sorted(t.get("countries", [])), dict(labels), [o.num for o in orgs])
        if not labels:
            p.note = "Item ohne Label"
            out.append(p)
            continue
        langs = country_langs(official, p.countries)
        p.mul, notes = _native(labels, langs)
        ref = p.mul or next((v for lang, v in labels.items() if _base(lang) in langs and not _non_latin(v)), "")

        cands: list[tuple[str, str]] = []
        offs = {norm_name(v): v for v in t.get("official_en", []) if v}
        if len(offs) == 1:
            cands.append((next(iter(offs.values())), "P1448"))
        elif len(offs) > 1:
            notes.append("mehrere offizielle englische Namen (P1448): " + " | ".join(offs.values()))
        if orgs and not cands:
            en, note = _europeana_en(orgs, t)
            if en:
                cands.append((en, "europeana"))
            if note:
                notes.append(note)
        for en, src in cands[:1]:
            if ref and norm_name(en) == norm_name(ref):
                continue  # gleicher Name: mul genügt
            if _non_latin(en) or not _term_ok(en):
                notes.append(f"englischer Name ungeeignet: {en}")
            elif ref and (name_similarity(norm_name(en), norm_name(ref)) >= EN_VARIANT
                          or f" {norm_name(ref)} " in f" {norm_name(en)} "):
                notes.append(f"englischer Name ({src}) ist nur eine Variante des Namens: {en} – en nicht gesetzt")
            else:
                p.en, p.en_source = en, src
        p.note = "; ".join(notes)
        if p.mul or p.en or p.note:
            out.append(p)
    return out


def label_commands(proposals: list[LabelProposal], kind: str) -> list[list[str]]:
    """QuickStatements je Item: kind = 'Lmul' | 'Len'."""
    attr = {"Lmul": "mul", "Len": "en"}[kind]
    return [[f'{p.qid}\t{kind}\t"{getattr(p, attr)}"'] for p in proposals if getattr(p, attr)]
