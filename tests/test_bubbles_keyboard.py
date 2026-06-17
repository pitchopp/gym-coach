"""Tests purs : découpage en bulles (coach._split_bubbles) et reply keyboard (telegram)."""

from __future__ import annotations

from app import coach, telegram


def test_reply_keyboard_format():
    markup = telegram.reply_keyboard(["2x", "3x", "4x"])
    assert markup == {
        "keyboard": [["2x", "3x"], ["4x"]],  # 1 à 2 boutons par rangée
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "is_persistent": False,
    }


def test_split_bubbles_simple():
    assert coach._split_bubbles("Salut, ça va ?") == ["Salut, ça va ?"]


def test_split_bubbles_multi():
    text = "Première question ?[[NEXT]]Deuxième question ?"
    assert coach._split_bubbles(text) == ["Première question ?", "Deuxième question ?"]


def test_split_bubbles_markdown_separator_reste_une_bulle():
    # Un `---` markdown (ex. dans un programme) ne doit PAS découper le message.
    program = "Lundi : pecs\n\n---\n\nMardi : dos"
    assert coach._split_bubbles(program) == [program]


def test_split_bubbles_vide():
    assert coach._split_bubbles("") == []
    assert coach._split_bubbles("[[NEXT]]\n\n[[NEXT]]") == []


def test_split_bubbles_plafond_fusionne_le_surplus():
    text = "[[NEXT]]".join(["a", "b", "c", "d", "e", "f"])
    bubbles = coach._split_bubbles(text)
    assert len(bubbles) == 4
    assert bubbles[:3] == ["a", "b", "c"]
    # Le surplus (d, e, f) est fusionné dans la dernière bulle, rien n'est perdu.
    assert bubbles[3] == "d\n\ne\n\nf"
