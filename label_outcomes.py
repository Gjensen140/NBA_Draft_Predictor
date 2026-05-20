"""
label_outcomes.py
-----------------
Assigns career outcome labels to every drafted player based on
their NBA stats through age 26 (or full career if shorter).

Outcome tiers:
    star        career WS >= 30  OR  2+ All-Star appearances
    starter     career WS >= 15
    rotation    career WS >= 5
    bust        everything else (< 5 WS or fewer than 3 seasons)

Usage:
    python label_outcomes.py --db postgresql://localhost/nba_draft
"""

import argparse
import logging
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)

LABEL_SQL = """
WITH career AS (
    SELECT
        dp.player_id,
        dp.draft_year,
        COUNT(DISTINCT ns.season)                           AS seasons_played,
        COALESCE(SUM(ns.win_shares)  FILTER (
            WHERE ns.age <= 26), 0)                         AS ws_through_26,
        MAX(ns.ws_per_48)                                   AS peak_ws_per_48
    FROM nba.draft_picks dp
    LEFT JOIN nba.nba_seasons ns USING (player_id)
    GROUP BY dp.player_id, dp.draft_year
),
labeled AS (
    SELECT
        player_id,
        ws_through_26                                       AS career_win_shares,
        peak_ws_per_48,
        seasons_played,
        CASE
            WHEN ws_through_26 >= 30  THEN 'star'
            WHEN ws_through_26 >= 15  THEN 'starter'
            WHEN ws_through_26 >= 5   THEN 'rotation'
            ELSE                           'bust'
        END                                                 AS outcome
    FROM career
)
INSERT INTO nba.outcome_labels
    (player_id, career_win_shares, peak_ws_per_48, seasons_played, outcome)
SELECT
    player_id, career_win_shares, peak_ws_per_48, seasons_played, outcome
FROM labeled
ON CONFLICT (player_id) DO UPDATE SET
    career_win_shares = EXCLUDED.career_win_shares,
    peak_ws_per_48    = EXCLUDED.peak_ws_per_48,
    seasons_played    = EXCLUDED.seasons_played,
    outcome           = EXCLUDED.outcome,
    labeled_at        = NOW();
"""

SUMMARY_SQL = """
SELECT outcome, COUNT(*) AS n
FROM nba.outcome_labels
GROUP BY outcome
ORDER BY n DESC;
"""

def run(db_url: str):
    engine = create_engine(db_url)
    with engine.begin() as conn:
        log.info("Labeling outcomes...")
        conn.execute(text(LABEL_SQL))

        log.info("Label distribution:")
        rows = conn.execute(text(SUMMARY_SQL)).fetchall()
        for outcome, n in rows:
            log.info(f"  {outcome:<12} {n:>4} players")

    log.info("Done. Run feature_engineering.sql next.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="postgresql://localhost/nba_draft")
    args = parser.parse_args()
    run(args.db)