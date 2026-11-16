"""Scraper for Iraqi Cinema (www.iraqicinema.net) Livewire snapshots."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import html as html_lib
import json
import logging
import re
import urllib.parse
import httpx

logger = logging.getLogger(__name__)

_SNAPSHOT_RE = re.compile(r'wire:snapshot="([^"]+)"')
_CSRF_RE = re.compile(r'data-csrf="([^"]+)"')
_RELEASE_DATE_RE = re.compile(
    r'Release\s+Date\s*</div>\s*<div[^>]*class="description"[^>]*>\s*([^<]+?)\s*</div>',
    re.IGNORECASE,
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}


@dataclass(frozen=True)
class MovieSummary:
    """Basic movie metadata from the Iraqi Cinema listing page."""

    movie_id: int
    title: str
    slug: str
    duration: str
    image_url: str | None
    categories: list[str]

    @property
    def is_bookable(self) -> bool:
        """Return True if the movie is currently showing or newly open."""
        cats = {c.lower() for c in self.categories}
        return bool(cats & {"now-showing", "new-movies"})


@dataclass(frozen=True)
class Showtime:
    """A single screening slot at a cinema branch."""

    time: str
    screen_type: str
    theater_label: str
    available_seats: int | None


@dataclass(frozen=True)
class MovieDetails:
    """Full booking availability for a movie across cinema branches."""

    movie: MovieSummary
    selected_date: str
    branches: dict[str, list[Showtime]]
    detail_url: str


def _unwrap_livewire(value):
    """Unwrap Livewire [payload, {'s': ...}] tuples if present."""
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[1], dict)
        and "s" in value[1]
    ):
        return value[0]
    return value


def extract_snapshots(page_html: str) -> list[dict]:
    """Extract and decode all Livewire wire:snapshot JSON objects from HTML."""
    snapshots: list[dict] = []
    for raw in _SNAPSHOT_RE.findall(page_html):
        try:
            snapshots.append(json.loads(html_lib.unescape(raw)))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode Livewire snapshot: %s", exc)
    return snapshots


def parse_movies_listing(page_html: str) -> list[MovieSummary]:
    """Parse movies from the website.movie-listing Livewire snapshot."""
    for snap in extract_snapshots(page_html):
        if snap.get("memo", {}).get("name") != "website.movie-listing":
            continue
        raw_movies = _unwrap_livewire(snap.get("data", {}).get("movies", []))
        if not isinstance(raw_movies, list):
            return []

        movies: list[MovieSummary] = []
        for entry in raw_movies:
            m = _unwrap_livewire(entry)
            if not isinstance(m, dict):
                continue
            cats = _unwrap_livewire(m.get("categories", []))
            if not isinstance(cats, list):
                cats = []
            movies.append(
                MovieSummary(
                    movie_id=int(m["id"]),
                    title=re.sub(r"\s+", " ", str(m.get("name", ""))).strip(),
                    slug=str(m.get("slug", "")).strip(),
                    duration=str(m.get("duration", "") or "").strip(),
                    image_url=m.get("main_image"),
                    categories=[str(c) for c in cats],
                )
            )
        return movies
    return []


def parse_release_date(page_html: str) -> date | None:
    """Extract the movie's Release Date from its detail page HTML if present."""
    match = _RELEASE_DATE_RE.search(page_html)
    if not match:
        return None
    raw = re.sub(r"\s+", " ", match.group(1)).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_movie_shows_from_snapshot(
    snap: dict,
    movie: MovieSummary,
    detail_url: str,
    branch_filter: list[str] | None = None,
) -> MovieDetails:
    """Parse branches and showtimes from a decoded website.movie-show snapshot."""
    data = snap.get("data", {})
    selected_date = str(data.get("selectedDate", "") or "")
    branches: dict[str, list[Showtime]] = {}

    raw_shows = _unwrap_livewire(data.get("movieShows", {}))
    if not isinstance(raw_shows, dict):
        return MovieDetails(
            movie=movie,
            selected_date=selected_date,
            branches=branches,
            detail_url=detail_url,
        )

    for raw_branch_name, wrapped_screens in raw_shows.items():
        branch_name = re.sub(r"\s+", " ", str(raw_branch_name)).strip()
        if branch_filter and not any(
            flt in branch_name.lower() for flt in branch_filter
        ):
            continue

        screens_dict = _unwrap_livewire(wrapped_screens)
        if not isinstance(screens_dict, dict):
            continue

        branch_showtimes: list[Showtime] = []
        for screen_name, wrapped_slots in screens_dict.items():
            slots = _unwrap_livewire(wrapped_slots)
            if not isinstance(slots, list):
                continue
            for wrapped_slot in slots:
                slot = _unwrap_livewire(wrapped_slot)
                if not isinstance(slot, dict):
                    continue
                if slot.get("visibility", 1) == 0:
                    continue
                time_str = str(slot.get("time", "")).strip()
                if not time_str:
                    continue
                screen_type = str(
                    slot.get("screen_type") or slot.get("price_group") or screen_name
                ).strip()
                theater_label = str(slot.get("theater_label", "") or "").strip()
                seats = slot.get("available_seats")
                branch_showtimes.append(
                    Showtime(
                        time=time_str,
                        screen_type=screen_type,
                        theater_label=theater_label,
                        available_seats=int(seats) if seats is not None else None,
                    )
                )

        if branch_showtimes:
            branches[branch_name] = branch_showtimes

    return MovieDetails(
        movie=movie,
        selected_date=selected_date,
        branches=branches,
        detail_url=detail_url,
    )


def parse_movie_shows(
    page_html: str,
    movie: MovieSummary,
    detail_url: str,
    branch_filter: list[str] | None = None,
) -> MovieDetails:
    """Parse branches and showtimes from the website.movie-show snapshot."""
    for snap in extract_snapshots(page_html):
        if snap.get("memo", {}).get("name") == "website.movie-show":
            return parse_movie_shows_from_snapshot(
                snap, movie=movie, detail_url=detail_url, branch_filter=branch_filter
            )

    return MovieDetails(
        movie=movie,
        selected_date="",
        branches={},
        detail_url=detail_url,
    )


class IraqiCinemaClient:
    """HTTP client for fetching Iraqi Cinema movies and showtimes."""

    def __init__(
        self,
        base_url: str = "https://www.iraqicinema.net/Harith/en",
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch_movies(self) -> list[MovieSummary]:
        url = f"{self.base_url}/movies/listing"
        with httpx.Client(headers=_HEADERS, timeout=self.timeout, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return parse_movies_listing(resp.text)

    def fetch_movie_details(
        self,
        movie: MovieSummary,
        branch_filter: list[str] | None = None,
        check_advance_dates: bool = False,
    ) -> MovieDetails:
        encoded_slug = urllib.parse.quote(movie.slug, safe=":-_")
        detail_url = f"{self.base_url}/movie-details/{encoded_slug}"
        with httpx.Client(headers=_HEADERS, timeout=self.timeout, follow_redirects=True) as client:
            resp = client.get(detail_url)
            resp.raise_for_status()
            details = parse_movie_shows(
                resp.text, movie=movie, detail_url=detail_url, branch_filter=branch_filter
            )
            if details.branches or not check_advance_dates:
                return details

            csrf_match = _CSRF_RE.search(resp.text)
            raw_snaps = _SNAPSHOT_RE.findall(resp.text)
            if not csrf_match or not raw_snaps:
                return details

            csrf = csrf_match.group(1)
            target_snap_str: str | None = None
            for raw in raw_snaps:
                decoded = html_lib.unescape(raw)
                if '"website.movie-show"' in decoded:
                    target_snap_str = decoded
                    break
            if not target_snap_str:
                return details

            candidate_dates: list[str] = []
            rel_date = parse_release_date(resp.text)
            today = date.today()
            if rel_date and rel_date >= today - timedelta(days=2):
                for delta in (-1, 0, 1):
                    d_str = (rel_date + timedelta(days=delta)).isoformat()
                    if d_str not in candidate_dates:
                        candidate_dates.append(d_str)
            tomorrow_str = (today + timedelta(days=1)).isoformat()
            if tomorrow_str not in candidate_dates:
                candidate_dates.append(tomorrow_str)

            root_origin = urllib.parse.urljoin(self.base_url, "/livewire/update")
            for d_str in candidate_dates:
                try:
                    lw_resp = client.post(
                        root_origin,
                        json={
                            "_token": csrf,
                            "components": [
                                {
                                    "snapshot": target_snap_str,
                                    "updates": {},
                                    "calls": [
                                        {
                                            "path": "",
                                            "method": "selectDate",
                                            "params": [d_str],
                                        }
                                    ],
                                }
                            ],
                        },
                        headers={
                            "X-Livewire": "true",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "Referer": detail_url,
                        },
                    )
                    if lw_resp.status_code != 200:
                        continue
                    lw_json = lw_resp.json()
                    new_snap_str = lw_json["components"][0]["snapshot"]
                    target_snap_str = new_snap_str
                    adv_details = parse_movie_shows_from_snapshot(
                        json.loads(new_snap_str),
                        movie=movie,
                        detail_url=detail_url,
                        branch_filter=branch_filter,
                    )
                    if adv_details.branches:
                        return adv_details
                except Exception as exc:
                    logger.debug("Advance date check failed for %s on %s: %s", movie.slug, d_str, exc)

            return details
