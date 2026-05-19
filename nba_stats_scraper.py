"""
nba_stats_scraper.py
--------------------
Pulls NBA Draft Combine measurements from the official NBA Stats API.
Covers body measurements, athletic testing, and strength data.

Usage:
    python nba_stats_scraper.py --start 2000 --end 2025 --db postgresql://localhost/nba_draft

Requires:
    pip install requests pandas psycopg2-binary sqlalchemy tqdm
"""

import argparse
import logging
import time

import requests
import pandas as pd
from sqlalchemy import create_engine, text
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = "https://stats.nba.com/stats"

# The NBA Stats API is picky about headers — these are required to avoid 403s
HEADERS = {
    "Host":             "stats.nba.com",
    "User-Agent":       "Mozilla/5.0",
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token":  "true",
    "Referer":          "https://www.nba.com/",
    "Connection":       "keep-alive",
}

REQUEST_DELAY = 2  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
ENDPOINTS = {
    "body":     "draftcombinestats",        # height, weight, wingspan, etc.
    "agility":  "draftcombinedrillresults", # lane agility, sprint, shuttle
    "strength": "draftcombineplayeranthro", # bench press, standing reach
}

def build_season_str(year: int) -> str:
    """2019 → '2019-20'"""
    return f"{year}-{str(year + 1)[-2:]}"


def fetch_combine_endpoint(endpoint_name: str, season: str) -> pd.DataFrame:
    """
    Hit one NBA Stats combine endpoint and return a normalized DataFrame.
    """
    url = f"{BASE}/{ENDPOINTS[endpoint_name]}"
    params = {
        "LeagueID":  "00",
        "SeasonYear": season,
    }

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            time.sleep(REQUEST_DELAY)

            result_set = data["resultSets"][0]
            columns = [c.lower() for c in result_set["headers"]]
            rows = result_set["rowSet"]

            if not rows:
                return pd.DataFrame()

            return pd.DataFrame(rows, columns=columns)

        except requests.RequestException as e:
            log.warning(f"Attempt {attempt+1} failed ({endpoint_name}, {season}): {e}")
            time.sleep(REQUEST_DELAY * 3)

    log.error(f"All attempts failed: {endpoint_name} / {season}")
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Column normalizers
# ---------------------------------------------------------------------------

BODY_COLS = {
    "player_id":            "nba_person_id",
    "player_name":          "full_name",
    "height_wo_shoes":      "height_no_shoes",
    "height_w_shoes":       "height_with_shoes",
    "wingspan":             "wingspan",
    "standing_reach":       "standing_reach",
    "weight":               "weight_lbs",
    "body_fat_pct":         "body_fat_pct",
    "hand_length":          "hand_length",
    "hand_width":           "hand_width",
}

AGILITY_COLS = {
    "player_id":            "nba_person_id",
    "lane_agility_time":    "lane_agility_time",
    "modified_lane_agility_time": "shuttle_run_time",
    "three_quarter_sprint": "three_quarter_sprint",
    "standing_vertical_leap": "standing_vertical",
    "max_vertical_leap":    "max_vertical",
}

STRENGTH_COLS = {
    "player_id":            "nba_person_id",
    "bench_press":          "bench_press_reps",
}


def normalize_df(df: pd.DataFrame, col_map: dict) -> pd.DataFrame:
    """Select and rename columns, coerce numerics."""
    available = {k: v for k, v in col_map.items() if k in df.columns}
    out = df[list(available.keys())].rename(columns=available)
    for col in out.columns:
        if col not in ("full_name", "nba_person_id"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


# ---------------------------------------------------------------------------
# Fetch + merge all combine data for a season
# ---------------------------------------------------------------------------

def fetch_combine_season(season_year: int) -> pd.DataFrame:
    season = build_season_str(season_year)
    log.info(f"Fetching combine data: {season}")

    body     = fetch_combine_endpoint("body",     season)
    agility  = fetch_combine_endpoint("agility",  season)
    strength = fetch_combine_endpoint("strength", season)

    if body.empty:
        log.warning(f"  No body data for {season}")
        return pd.DataFrame()

    body_norm     = normalize_df(body,     BODY_COLS)
    agility_norm  = normalize_df(agility,  AGILITY_COLS)  if not agility.empty  else pd.DataFrame()
    strength_norm = normalize_df(strength, STRENGTH_COLS) if not strength.empty else pd.DataFrame()

    merged = body_norm
    if not agility_norm.empty:
        merged = merged.merge(agility_norm, on="nba_person_id", how="left")
    if not strength_norm.empty:
        merged = merged.merge(strength_norm, on="nba_person_id", how="left")

    merged["draft_year"] = season_year
    log.info(f"  → {len(merged)} players")
    return merged


# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------

def upsert_combine_data(engine, df: pd.DataFrame):
    """
    Match players by nba_person_id or name, then insert combine measurements.
    Falls back to inserting new player rows if no match found.
    """
    with engine.begin() as conn:
        for _, row in df.iterrows():
            person_id = row.get("nba_person_id")
            name      = row.get("full_name", "")
            year      = int(row["draft_year"])

            # Look up player_id
            player_row = None
            if person_id:
                player_row = conn.execute(
                    text("SELECT player_id FROM nba.players WHERE nba_person_id = :pid"),
                    {"pid": int(person_id)}
                ).fetchone()

            if not player_row and name:
                player_row = conn.execute(
                    text("SELECT player_id FROM nba.players WHERE full_name ILIKE :name"),
                    {"name": name}
                ).fetchone()

            if not player_row:
                # Insert a stub player row — will be enriched by bref scraper
                conn.execute(text("""
                    INSERT INTO nba.players (nba_person_id, full_name)
                    VALUES (:pid, :name)
                    ON CONFLICT DO NOTHING
                """), {"pid": int(person_id) if person_id else None, "name": name})
                player_row = conn.execute(
                    text("SELECT player_id FROM nba.players WHERE full_name ILIKE :name"),
                    {"name": name}
                ).fetchone()

            if not player_row:
                continue

            player_id = player_row[0]

            def val(col):
                v = row.get(col)
                return None if pd.isna(v) else float(v)

            conn.execute(text("""
                INSERT INTO nba.combine_measurements (
                    player_id, draft_year,
                    height_no_shoes, height_with_shoes, wingspan, standing_reach,
                    weight_lbs, body_fat_pct, hand_length, hand_width,
                    standing_vertical, max_vertical,
                    lane_agility_time, shuttle_run_time, three_quarter_sprint,
                    bench_press_reps
                ) VALUES (
                    :player_id, :draft_year,
                    :height_no_shoes, :height_with_shoes, :wingspan, :standing_reach,
                    :weight_lbs, :body_fat_pct, :hand_length, :hand_width,
                    :standing_vertical, :max_vertical,
                    :lane_agility_time, :shuttle_run_time, :three_quarter_sprint,
                    :bench_press_reps
                )
                ON CONFLICT (player_id, draft_year) DO UPDATE SET
                    wingspan            = EXCLUDED.wingspan,
                    max_vertical        = EXCLUDED.max_vertical,
                    lane_agility_time   = EXCLUDED.lane_agility_time
            """), {
                "player_id":            player_id,
                "draft_year":           year,
                "height_no_shoes":      val("height_no_shoes"),
                "height_with_shoes":    val("height_with_shoes"),
                "wingspan":             val("wingspan"),
                "standing_reach":       val("standing_reach"),
                "weight_lbs":           val("weight_lbs"),
                "body_fat_pct":         val("body_fat_pct"),
                "hand_length":          val("hand_length"),
                "hand_width":           val("hand_width"),
                "standing_vertical":    val("standing_vertical"),
                "max_vertical":         val("max_vertical"),
                "lane_agility_time":    val("lane_agility_time"),
                "shuttle_run_time":     val("shuttle_run_time"),
                "three_quarter_sprint": val("three_quarter_sprint"),
                "bench_press_reps":     int(val("bench_press_reps")) if val("bench_press_reps") else None,
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(start_year: int, end_year: int, db_url: str):
    engine = create_engine(db_url)

    log.info("=" * 60)
    log.info(f"NBA Stats Combine Scraper: {start_year}–{end_year}")
    log.info("=" * 60)

    for year in tqdm(range(start_year, end_year + 1), desc="Seasons"):
        df = fetch_combine_season(year)
        if not df.empty:
            upsert_combine_data(engine, df)

    log.info("Combine data load complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NBA Stats combine measurements scraper")
    parser.add_argument("--start", type=int, default=2000)
    parser.add_argument("--end",   type=int, default=2025)
    parser.add_argument("--db",    type=str, default="postgresql://localhost/nba_draft")
    args = parser.parse_args()

    run(args.start, args.end, args.db)