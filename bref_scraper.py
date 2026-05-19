"""
bref_scraper.py
---------------
Scrapes Basketball Reference for:
  1. Draft history (2000–2018)
  2. College stats for each draftee
  3. NBA career stats for each draftee

Usage:
    python bref_scraper.py --start 2000 --end 2018 --db postgresql://localhost/nba_draft

Requires:
    pip install requests beautifulsoup4 pandas psycopg2-binary sqlalchemy tqdm
"""

import argparse
import logging
import time
import re
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup, Comment
from sqlalchemy import create_engine, text
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://www.basketball-reference.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (research project; contact: you@example.com)"}
REQUEST_DELAY = 4  # seconds — bref rate-limits aggressively, be respectful

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_soup(url: str) -> BeautifulSoup:
    """Fetch a URL and return a BeautifulSoup object. Retries once on failure."""
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt+1} failed for {url}: {e}")
            time.sleep(REQUEST_DELAY * 2)
    raise RuntimeError(f"Failed to fetch {url}")


def parse_float(val) -> float | None:
    """Safely coerce a scraped string to float."""
    try:
        return float(str(val).strip().replace("%", ""))
    except (ValueError, TypeError):
        return None


def parse_int(val) -> int | None:
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def extract_hidden_table(soup: BeautifulSoup, table_id: str) -> BeautifulSoup | None:
    """
    Bref wraps many tables in HTML comments to dodge scrapers.
    This finds them by parsing comment nodes.
    """
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if table_id in comment:
            inner = BeautifulSoup(comment, "html.parser")
            return inner.find("table", {"id": table_id})
    return soup.find("table", {"id": table_id})


# ---------------------------------------------------------------------------
# 1. Draft scraper
# ---------------------------------------------------------------------------

def scrape_draft_year(year: int) -> list[dict]:
    """
    Scrape the draft page for a single year.
    Returns a list of dicts, one per pick.
    """
    url = f"{BASE_URL}/draft/NBA_{year}.html"
    log.info(f"Scraping draft: {year}  →  {url}")
    soup = get_soup(url)

    table = extract_hidden_table(soup, "stats")
    if not table:
        log.warning(f"No draft table found for {year}")
        return []

    picks = []
    rows = table.find("tbody").find_all("tr")

    for row in rows:
        # Skip header rows injected mid-table
        if row.get("class") and "thead" in row.get("class"):
            continue

        cells = row.find_all(["td", "th"])
        if len(cells) < 7:
            continue

        def cell(data_stat: str):
            td = row.find(attrs={"data-stat": data_stat})
            return td.get_text(strip=True) if td else None

        # Extract bref player id from the anchor href
        player_link = row.find("td", {"data-stat": "player"})
        bref_id = None
        if player_link and player_link.find("a"):
            href = player_link.find("a")["href"]          # /players/j/jamesle01.html
            bref_id = href.split("/")[-1].replace(".html", "")

        name = cell("player")
        if not name:
            continue  # blank row

        pick = {
            "bref_id":       bref_id,
            "full_name":     name,
            "draft_year":    year,
            "draft_round":   parse_int(cell("round")),
            "pick_overall":  parse_int(cell("pick_overall")),
            "team_abbr":     cell("team_id"),
            "college":       cell("college_name"),
            "age_at_draft":  parse_float(cell("age")),
        }
        picks.append(pick)

    log.info(f"  → {len(picks)} picks found")
    return picks


# ---------------------------------------------------------------------------
# 2. College stats scraper
# ---------------------------------------------------------------------------

def scrape_college_stats(bref_id: str) -> list[dict]:
    """
    Scrape college season stats for a player from their bref page.
    Returns a list of dicts, one per college season.
    """
    url = f"{BASE_URL}/players/{bref_id[0]}/{bref_id}.html"
    soup = get_soup(url)

    table = extract_hidden_table(soup, "college_stats")
    if not table:
        return []

    seasons = []
    for row in table.find("tbody").find_all("tr"):
        if row.get("class") and "thead" in row.get("class"):
            continue

        def cell(stat: str):
            td = row.find(attrs={"data-stat": stat})
            return td.get_text(strip=True) if td else None

        season_str = cell("season")     # e.g. "2018-19"
        if not season_str or "-" not in season_str:
            continue

        # Convert "2018-19" → 2019
        try:
            season_year = int(season_str.split("-")[0]) + 1
        except ValueError:
            continue

        season = {
            "bref_id":          bref_id,
            "season":           season_year,
            "college":          cell("school_name"),
            "conference":       cell("conf_abbr"),
            "games_played":     parse_int(cell("g")),
            "games_started":    parse_int(cell("gs")),
            "minutes_per_game": parse_float(cell("mp_per_g")),
            "pts_per_game":     parse_float(cell("pts_per_g")),
            "reb_per_game":     parse_float(cell("trb_per_g")),
            "ast_per_game":     parse_float(cell("ast_per_g")),
            "stl_per_game":     parse_float(cell("stl_per_g")),
            "blk_per_game":     parse_float(cell("blk_per_g")),
            "tov_per_game":     parse_float(cell("tov_per_g")),
            "fg_pct":           parse_float(cell("fg_pct")),
            "three_pt_pct":     parse_float(cell("fg3_pct")),
            "ft_pct":           parse_float(cell("ft_pct")),
            # Advanced — bref sometimes hides these in a second table
            "per":              parse_float(cell("per")),
            "ts_pct":           parse_float(cell("ts_pct")),
            "ast_pct":          parse_float(cell("ast_pct")),
            "tov_pct":          parse_float(cell("tov_pct")),
            "usg_pct":          parse_float(cell("usg_pct")),
            "win_shares":       parse_float(cell("ws")),
            "bpm":              parse_float(cell("bpm")),
        }
        seasons.append(season)

    return seasons


# ---------------------------------------------------------------------------
# 3. NBA career stats scraper
# ---------------------------------------------------------------------------

def scrape_nba_career_stats(bref_id: str) -> list[dict]:
    """
    Scrape NBA per-game and advanced stats from a player's bref page.
    Returns one dict per season.
    """
    url = f"{BASE_URL}/players/{bref_id[0]}/{bref_id}.html"
    soup = get_soup(url)

    # Per-game table
    pg_table = extract_hidden_table(soup, "per_game")
    adv_table = extract_hidden_table(soup, "advanced")

    def parse_table(table, key_stat="season") -> dict[str, dict]:
        """Parse a stats table into {season_key: {stats}}"""
        result = {}
        if not table:
            return result
        for row in table.find("tbody").find_all("tr"):
            if row.get("class") and "thead" in row.get("class"):
                continue

            def cell(stat):
                td = row.find(attrs={"data-stat": stat})
                return td.get_text(strip=True) if td else None

            season_str = cell("season")
            team = cell("team_id")
            if not season_str or not team:
                continue

            try:
                season_year = int(season_str.split("-")[0]) + 1
            except ValueError:
                continue

            key = f"{season_year}_{team}"
            result[key] = {
                "season":    season_year,
                "team_abbr": team,
                "age":       parse_int(cell("age")),
                "g":         parse_int(cell("g")),
                "gs":        parse_int(cell("gs")),
                "mp":        parse_float(cell("mp_per_g")),
                "pts":       parse_float(cell("pts_per_g")),
                "reb":       parse_float(cell("trb_per_g")),
                "ast":       parse_float(cell("ast_per_g")),
                "stl":       parse_float(cell("stl_per_g")),
                "blk":       parse_float(cell("blk_per_g")),
                "tov":       parse_float(cell("tov_per_g")),
                "fg_pct":    parse_float(cell("fg_pct")),
                "fg3_pct":   parse_float(cell("fg3_pct")),
                "ft_pct":    parse_float(cell("ft_pct")),
                "per":       parse_float(cell("per")),
                "ts_pct":    parse_float(cell("ts_pct")),
                "ws":        parse_float(cell("ws")),
                "ws_48":     parse_float(cell("ws_per_48")),
                "bpm":       parse_float(cell("bpm")),
                "vorp":      parse_float(cell("vorp")),
            }
        return result

    pg_data = parse_table(pg_table)
    adv_data = parse_table(adv_table)

    # Merge per-game + advanced on season/team key
    all_keys = set(pg_data) | set(adv_data)
    seasons = []
    for key in all_keys:
        merged = {**pg_data.get(key, {}), **adv_data.get(key, {})}
        merged["bref_id"] = bref_id
        seasons.append(merged)

    return seasons


# ---------------------------------------------------------------------------
# 4. Database writer
# ---------------------------------------------------------------------------

def upsert_players_and_picks(engine, picks: list[dict]):
    with engine.begin() as conn:
        for p in picks:
            # Upsert player
            conn.execute(text("""
                INSERT INTO nba.players (bref_id, full_name)
                VALUES (:bref_id, :full_name)
                ON CONFLICT (bref_id) DO UPDATE SET full_name = EXCLUDED.full_name
            """), {"bref_id": p["bref_id"], "full_name": p["full_name"]})

            # Get player_id
            row = conn.execute(
                text("SELECT player_id FROM nba.players WHERE bref_id = :bref_id"),
                {"bref_id": p["bref_id"]}
            ).fetchone()
            if not row:
                continue
            player_id = row[0]

            # Upsert draft pick
            conn.execute(text("""
                INSERT INTO nba.draft_picks
                    (player_id, draft_year, draft_round, pick_overall, team_abbr, college, age_at_draft)
                VALUES
                    (:player_id, :draft_year, :draft_round, :pick_overall, :team_abbr, :college, :age_at_draft)
                ON CONFLICT (draft_year, pick_overall) DO NOTHING
            """), {**p, "player_id": player_id})


def upsert_college_seasons(engine, bref_id: str, seasons: list[dict]):
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT player_id FROM nba.players WHERE bref_id = :bref_id"),
            {"bref_id": bref_id}
        ).fetchone()
        if not row:
            return
        player_id = row[0]

        for s in seasons:
            conn.execute(text("""
                INSERT INTO nba.college_seasons
                    (player_id, season, college, conference, games_played, games_started,
                     minutes_per_game, pts_per_game, reb_per_game, ast_per_game,
                     stl_per_game, blk_per_game, tov_per_game,
                     fg_pct, three_pt_pct, ft_pct,
                     per, ts_pct, ast_pct, tov_pct, usg_pct, win_shares, bpm)
                VALUES
                    (:player_id, :season, :college, :conference, :games_played, :games_started,
                     :minutes_per_game, :pts_per_game, :reb_per_game, :ast_per_game,
                     :stl_per_game, :blk_per_game, :tov_per_game,
                     :fg_pct, :three_pt_pct, :ft_pct,
                     :per, :ts_pct, :ast_pct, :tov_pct, :usg_pct, :win_shares, :bpm)
                ON CONFLICT (player_id, season, college) DO NOTHING
            """), {**s, "player_id": player_id})


def upsert_nba_seasons(engine, bref_id: str, seasons: list[dict]):
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT player_id FROM nba.players WHERE bref_id = :bref_id"),
            {"bref_id": bref_id}
        ).fetchone()
        if not row:
            return
        player_id = row[0]

        for s in seasons:
            conn.execute(text("""
                INSERT INTO nba.nba_seasons
                    (player_id, season, team_abbr, age, games_played, games_started,
                     minutes_per_game, pts_per_game, reb_per_game, ast_per_game,
                     stl_per_game, blk_per_game, tov_per_game,
                     fg_pct, three_pt_pct, ft_pct,
                     per, ts_pct, win_shares, ws_per_48, bpm, vorp)
                VALUES
                    (:player_id, :season, :team_abbr, :age, :g, :gs,
                     :mp, :pts, :reb, :ast,
                     :stl, :blk, :tov,
                     :fg_pct, :fg3_pct, :ft_pct,
                     :per, :ts_pct, :ws, :ws_48, :bpm, :vorp)
                ON CONFLICT (player_id, season, team_abbr) DO NOTHING
            """), {**s, "player_id": player_id})


# ---------------------------------------------------------------------------
# 5. Main pipeline
# ---------------------------------------------------------------------------

def run(start_year: int, end_year: int, db_url: str):
    engine = create_engine(db_url)

    # --- Step 1: Scrape and store all draft picks ---
    log.info("=" * 60)
    log.info(f"Phase 1: Draft history {start_year}–{end_year}")
    log.info("=" * 60)

    all_picks = []
    for year in range(start_year, end_year + 1):
        picks = scrape_draft_year(year)
        upsert_players_and_picks(engine, picks)
        all_picks.extend(picks)

    # --- Step 2: Scrape college + NBA stats per player ---
    bref_ids = [p["bref_id"] for p in all_picks if p["bref_id"]]
    bref_ids = list(dict.fromkeys(bref_ids))  # dedupe, preserve order

    log.info("=" * 60)
    log.info(f"Phase 2: Player stats for {len(bref_ids)} players")
    log.info("=" * 60)

    failed = []
    for bref_id in tqdm(bref_ids, desc="Players"):
        try:
            college = scrape_college_stats(bref_id)
            upsert_college_seasons(engine, bref_id, college)

            nba = scrape_nba_career_stats(bref_id)
            upsert_nba_seasons(engine, bref_id, nba)
        except Exception as e:
            log.warning(f"Failed on {bref_id}: {e}")
            failed.append(bref_id)

    if failed:
        log.warning(f"\n{len(failed)} players failed — check logs:")
        for f in failed:
            log.warning(f"  {f}")

    log.info("Done. Run label_outcomes.py next to assign outcome buckets.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape bref draft + player stats")
    parser.add_argument("--start", type=int, default=2000)
    parser.add_argument("--end",   type=int, default=2018)
    parser.add_argument("--db",    type=str, default="postgresql://localhost/nba_draft")
    args = parser.parse_args()

    run(args.start, args.end, args.db)