"""Extractor tests on local HTML fixtures (no network)."""
from __future__ import annotations

from pathlib import Path

from tools.extractor import extract, parse_date

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_article_title_and_body():
    ex = extract(load("article.html"))
    assert ex.title == "City Council Approves New Transit Budget"
    assert "48 million dollars" in ex.text
    assert "forty new electric buses" in ex.text
    assert "independent audit" in ex.text


def test_article_boilerplate_stripped():
    ex = extract(load("article.html"))
    assert "Subscribe now" not in ex.text          # nav
    assert "All rights reserved" not in ex.text    # footer
    assert "Trending" not in ex.text               # sidebar headings/link lists


def test_article_publication_date_from_meta():
    ex = extract(load("article.html"))
    assert ex.date == "2026-05-14"


def test_plain_page_visible_date_and_text():
    ex = extract(load("plain.html"))
    assert ex.title == "Glass Frog Habitats"
    assert "translucent skin" in ex.text
    assert ex.date == "2019-03-03"


def test_extract_handles_malformed_html():
    ex = extract("<div><p>Broken but this paragraph survives the parse just fine "
                 "and is long enough to keep.</div></p></b>")
    assert "paragraph survives" in ex.text


def test_parse_date_formats():
    assert parse_date("2024-01-15").isoformat() == "2024-01-15"
    assert parse_date("March 5, 2024").isoformat() == "2024-03-05"
    assert parse_date("5 March 2024").isoformat() == "2024-03-05"
    assert parse_date("Sep 9, 2023").isoformat() == "2023-09-09"
    assert parse_date("no date here") is None
    assert parse_date("") is None
