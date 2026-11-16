"""Unit tests for Iraqi Cinema scraper, storage, and Telegram formatting."""

import html as html_lib
import json
from pathlib import Path

from app.scraper import (
    MovieSummary,
    parse_movie_shows,
    parse_movies_listing,
)
from app.storage import Storage
from app.telegram_bot import format_movie_notification, split_message


def _wrap_snapshot(name: str, data: dict) -> str:
    payload = json.dumps({"memo": {"name": name}, "data": data})
    return f'<div wire:snapshot="{html_lib.escape(payload, quote=True)}"></div>'


def test_parse_movies_listing_distinguishes_bookable_and_coming_soon() -> None:
    snap_html = _wrap_snapshot(
        "website.movie-listing",
        {
            "movies": [
                [
                    [
                        {
                            "id": 441,
                            "name": "Godzilla Minus Zero",
                            "slug": "godzilla-minus-zero",
                            "duration": "2h 10m",
                            "main_image": "https://example.com/g.webp",
                            "categories": [["now-showing", "new-movies"], {"s": "arr"}],
                        },
                        {"s": "arr"},
                    ],
                    [
                        {
                            "id": 391,
                            "name": "Avengers: Doomsday",
                            "slug": "avengers:-doomsday",
                            "duration": "2h 45m",
                            "main_image": "https://example.com/a.webp",
                            "categories": [["coming-soon"], {"s": "arr"}],
                        },
                        {"s": "arr"},
                    ],
                ],
                {"s": "arr"},
            ]
        },
    )

    movies = parse_movies_listing(snap_html)
    assert len(movies) == 2
    assert movies[0].movie_id == 441
    assert movies[0].is_bookable is True
    assert movies[1].movie_id == 391
    assert movies[1].is_bookable is False


def test_parse_movie_shows_and_branch_filter() -> None:
    movie = MovieSummary(
        movie_id=441,
        title="Godzilla Minus Zero",
        slug="godzilla-minus-zero",
        duration="2h 10m",
        image_url=None,
        categories=["now-showing"],
    )
    snap_html = _wrap_snapshot(
        "website.movie-show",
        {
            "selectedDate": "2026-11-16",
            "movieShows": [
                {
                    "Cinema Mansour Mall": [
                        {
                            "REG": [
                                [
                                    [
                                        {
                                            "visibility": 1,
                                            "time": "11:30 AM",
                                            "theater_label": "Theater 2",
                                            "screen_type": "REG",
                                            "available_seats": 91,
                                        },
                                        {"s": "arr"},
                                    ],
                                    [
                                        {
                                            "visibility": 0,
                                            "time": "01:00 AM",
                                            "theater_label": "Theater 2",
                                            "screen_type": "REG",
                                            "available_seats": 50,
                                        },
                                        {"s": "arr"},
                                    ],
                                ],
                                {"s": "arr"},
                            ]
                        },
                        {"s": "arr"},
                    ],
                    "Cinema Iraq Mall ": [
                        {
                            "IMAX": [
                                [
                                    [
                                        {
                                            "visibility": 1,
                                            "time": "05:15 PM",
                                            "theater_label": "Theater IMAX",
                                            "screen_type": "IMAX",
                                            "available_seats": 270,
                                        },
                                        {"s": "arr"},
                                    ]
                                ],
                                {"s": "arr"},
                            ]
                        },
                        {"s": "arr"},
                    ],
                },
                {"s": "arr"},
            ],
        },
    )

    all_details = parse_movie_shows(
        snap_html, movie, "https://www.iraqicinema.net/Harith/en/movie-details/godzilla-minus-zero"
    )
    assert set(all_details.branches.keys()) == {"Cinema Mansour Mall", "Cinema Iraq Mall"}
    assert len(all_details.branches["Cinema Mansour Mall"]) == 1
    assert all_details.branches["Cinema Mansour Mall"][0].time == "11:30 AM"

    filtered = parse_movie_shows(
        snap_html,
        movie,
        "https://www.iraqicinema.net/Harith/en/movie-details/godzilla-minus-zero",
        branch_filter=["iraq mall"],
    )
    assert list(filtered.branches.keys()) == ["Cinema Iraq Mall"]

    msg = format_movie_notification(all_details, is_new=True)
    assert "Godzilla Minus Zero" in msg
    assert "Cinema Mansour Mall" in msg
    assert "11:30 AM (91 مقعد)" in msg
    assert "Cinema Iraq Mall" in msg
    assert "IMAX" in msg


def test_storage_lifecycle(tmp_path: Path) -> None:
    db = Storage(tmp_path / "test.db")
    assert db.is_initialized() is False
    db.mark_initialized()
    assert db.is_initialized() is True

    db.mark_movie_seen(441, "Godzilla", "godzilla", ["now-showing"])
    assert 441 in db.get_seen_movie_ids()

    assert db.add_subscriber("12345") is True
    assert db.add_subscriber("12345") is False
    assert db.get_subscribers("99999") == ["12345", "99999"]
    assert db.remove_subscriber("12345") is True
    assert db.get_subscribers() == []


def test_split_message_respects_limit() -> None:
    text = "\n".join(f"line {i}" for i in range(100))
    chunks = split_message(text, limit=120)
    assert len(chunks) > 1
    assert all(len(c) <= 120 for c in chunks)
    assert "\n".join(chunks) == text
