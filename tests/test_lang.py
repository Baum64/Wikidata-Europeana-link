"""Sprachprüfung deutscher Aliase (Hunspell; ohne Hunspell nur Schrift/Wortliste)."""

import pytest

from glamsync import lang
from glamsync.lang import foreign_reason


def test_script_and_word_list_without_dictionary():
    assert foreign_reason("ハンブルク装飾工芸美術館") == "nichtlateinische Schrift"
    assert foreign_reason("Бамбершка државна библиотека") == "nichtlateinische Schrift"
    assert foreign_reason("Galleria di Stato Stoccarda")  # 'di' aus der Wortliste
    assert foreign_reason("Muzeum Śląskie")  # Buchstaben außerhalb des deutschen Alphabets


@pytest.mark.skipif(not lang._dicts(lang.GERMAN) or not lang._dicts(("en_US",)), reason="Hunspell de/en fehlt")
@pytest.mark.parametrize("alias,foreign", [
    ("German Historical Museum", True),
    ("Jewish Museum Munich", True),
    ("Museum for Art and Design in Nuremberg", True),
    ("Picture Gallery Berlin", True),
    ("Museum Folkwang", False),        # Eigenname, „Museum“ ist neutral
    ("Museum Selma", False),           # „Selma“ steht im en-Wörterbuch nur als Name
    ("Stadtarchiv Mannheim", False),
    ("Haus der Geschichte", False),
    ("SLUB Dresden", False),           # Abkürzungen entscheiden nichts
    ("Bauhaus-Archiv", False),
    ("Staatliche Kunstsammlungen Dresden", False),
])
def test_foreign_reason(alias, foreign):
    assert bool(foreign_reason(alias)) is foreign
