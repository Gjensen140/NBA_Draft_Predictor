-- NBA Draft Outcome Predictor: Feature Engineering
-- Populates nba.player_features with one model-usable row per player.


-- Runs after scraping and outcome labeling
-- Usage: psql -d nba_draft -f feature_engineering.sql

-- Re-runnable: uses INSERT ... ON CONFLICT DO UPDATE

SET search_path TO nba;

-- BLOCK 1: FINAL COLLEGE SEASON
-- Each player's most recent college season stats.
-- This is our primary signal decision-making data

CREATE TEMP VIEW IF NOT EXISTS v_final_college_season AS
SELECT DISTINCT ON (cs.player_id)
    cs.player_id,
    cs.season                                               AS final_college_season,
    cs.college,
    cs.conference,
    cs.games_played,
    cs.minutes_per_game,
    cs.pts_per_game,
    cs.reb_per_game,
    cs.ast_per_game,
    cs.stl_per_game,
    cs.blk_per_game,
    cs.tov_per_game,
    cs.fg_pct,
    cs.three_pt_pct,
    cs.ft_pct,
    cs.ts_pct,
    cs.usg_pct,
    cs.ast_pct,
    cs.tov_pct,
    cs.win_shares,
    cs.bpm,
    -- Per-36 minute conversions (normalizes to account for playing time differences)
    -- Guards against divide-by-zero for players with very low minutes
    CASE WHEN cs.minutes_per_game > 5
        THEN ROUND((cs.pts_per_game / cs.minutes_per_game) * 36, 2)
        ELSE NULL
    END                                                     AS pts_per36,
    CASE WHEN cs.minutes_per_game > 5
        THEN ROUND((cs.reb_per_game / cs.minutes_per_game) * 36, 2)
        ELSE NULL
    END                                                     AS reb_per36,
    CASE WHEN cs.minutes_per_game > 5
        THEN ROUND((cs.ast_per_game / cs.minutes_per_game) * 36, 2)
        ELSE NULL
    END                                                     AS ast_per36,
    -- AST/TOV ratio: important for guards/primary ball handlers
    CASE WHEN COALESCE(cs.tov_per_game, 0) > 0
        THEN ROUND(cs.ast_per_game / cs.tov_per_game, 2)
        ELSE NULL
    END                                                     AS ast_to_ratio
FROM college_seasons cs
INNER JOIN draft_picks dp USING (player_id)
-- Only keep college seasons that happened BEFORE the draft
WHERE cs.season <= dp.draft_year
ORDER BY cs.player_id, cs.season DESC, cs.games_played DESC;


-- BLOCK 2: PENULTIMATE COLLEGE SEASON
-- The season before the final one. Used to compute YoY trends.
-- A player improving year to year is a positive signal

CREATE TEMP VIEW IF NOT EXISTS v_penultimate_college_season AS
SELECT DISTINCT ON (cs.player_id)
    cs.player_id,
    cs.season                                               AS penult_season,
    cs.pts_per_game                                         AS penult_pts,
    cs.win_shares                                           AS penult_ws,
    cs.bpm                                                  AS penult_bpm,
    cs.ts_pct                                               AS penult_ts_pct
FROM college_seasons cs
INNER JOIN draft_picks dp USING (player_id)
INNER JOIN v_final_college_season fcs USING (player_id)
-- Penultimate = any season before the final college season
WHERE cs.season < fcs.final_college_season
  AND cs.season <= dp.draft_year
ORDER BY cs.player_id, cs.season DESC, cs.games_played DESC;


-- BLOCK 3: CONFERENCE STRENGTH ADJUSTMENT
-- Multiplies per-36 stats by a conference strength factor.
-- A 20 PPG scorer in the ACC is not the same as 20 PPG in the MAC.
-- Tier 1 = power conferences (factor ~1.0)
-- Tier 2 = mid-majors     (factor ~0.90)
-- Tier 3 = low-majors     (factor ~0.80)
-- =============================================================
-- TODO: seed conference_strength

CREATE TEMP VIEW IF NOT EXISTS v_conference_adjusted AS
SELECT
    fcs.player_id,
    fcs.conference,
    COALESCE(conf.tier, 2)                                  AS conference_tier,
    COALESCE(conf.strength_rating, 1.0)                     AS conf_strength,
    -- Apply strength multiplier to per-36 stats
    ROUND(fcs.pts_per36 * COALESCE(conf.strength_rating, 1.0), 2)  AS adj_pts_per36,
    ROUND(fcs.reb_per36 * COALESCE(conf.strength_rating, 1.0), 2)  AS adj_reb_per36,
    ROUND(fcs.ast_per36 * COALESCE(conf.strength_rating, 1.0), 2)  AS adj_ast_per36
FROM v_final_college_season fcs
LEFT JOIN conference_strength conf
    ON  conf.conference = fcs.conference
    AND conf.season     = fcs.final_college_season;

-- BLOCK 4: YEAR-OVER-YEAR TRENDS
-- Change between final and penultimate college season.
-- Positive delta = player is continually improving
-- Negative delta = player overplayed or regressed

CREATE TEMP VIEW IF NOT EXISTS v_yoy_trends AS
SELECT
    fcs.player_id,
    -- Scoring trend
    ROUND(fcs.pts_per_game - COALESCE(pcs.penult_pts, fcs.pts_per_game), 2)
                                                            AS pts_yoy_delta,
    -- Win shares trend (most holistic improvement signal)
    ROUND(fcs.win_shares - COALESCE(pcs.penult_ws, fcs.win_shares), 2)
                                                            AS ws_yoy_delta,
    -- BPM trend
    ROUND(fcs.bpm - COALESCE(pcs.penult_bpm, fcs.bpm), 2)  AS bpm_yoy_delta,
    -- True shooting trend (shooting development is predictive)
    ROUND(fcs.ts_pct - COALESCE(pcs.penult_ts_pct, fcs.ts_pct), 4)
                                                            AS ts_yoy_delta,
    -- Flag: only one college season available (freshman or one-and-done)
    CASE WHEN pcs.player_id IS NULL THEN TRUE ELSE FALSE END AS is_one_and_done
FROM v_final_college_season fcs
LEFT JOIN v_penultimate_college_season pcs USING (player_id);



-- BLOCK 5: COMBINE PHYSICAL PROFILE
-- Athletic/physical measurements from the draft combine.
-- Not all players attend: NULLs are expected and handled downstream
-- by the model with imputation.

CREATE TEMP VIEW IF NOT EXISTS v_combine AS
SELECT
    cm.player_id,
    cm.wingspan_height_ratio,               -- already computed column in schema
    cm.max_vertical,
    cm.standing_vertical,
    cm.lane_agility_time,
    cm.shuttle_run_time,
    cm.three_quarter_sprint,
    cm.body_fat_pct,
    cm.bench_press_reps,
    cm.hand_length,
    cm.hand_width,
    -- Derived: reach advantage (standing reach vs. height, important for bigs)
    CASE WHEN cm.height_with_shoes > 0
        THEN ROUND(cm.standing_reach - cm.height_with_shoes, 1)
        ELSE NULL
    END                                     AS reach_advantage,
    -- Derived: explosive vs. trained vertical gap
    CASE WHEN cm.standing_vertical IS NOT NULL AND cm.max_vertical IS NOT NULL
        THEN ROUND(cm.max_vertical - cm.standing_vertical, 1)
        ELSE NULL
    END                                     AS vert_explosiveness_gap
FROM combine_measurements cm;


-- BLOCK 6: DRAFT CONTEXT FEATURES
-- Pick number and age encode a lot of prior probability.
-- A 19-year-old at pick 5 has a very different baseline than
-- a 23-year-old at pick 45 with the same college stats.

CREATE TEMP VIEW IF NOT EXISTS v_draft_context AS
SELECT
    dp.player_id,
    dp.draft_year,
    dp.draft_round,
    dp.pick_overall,
    dp.age_at_draft,
    -- Log-transform pick number: the difference between picks 1 and 5
    -- matters more than the difference between picks 55 and 60
    ROUND(LN(dp.pick_overall + 1)::NUMERIC, 4)              AS log_pick,
    -- Age relative to typical draft age (~21.5)
    ROUND(dp.age_at_draft - 21.5, 2)                        AS age_vs_avg,
    -- Number of college seasons played (experience proxy)
    (
        SELECT COUNT(DISTINCT cs.season)
        FROM college_seasons cs
        WHERE cs.player_id = dp.player_id
          AND cs.season <= dp.draft_year
    )                                                       AS college_seasons_played
FROM draft_picks dp;

-- BLOCK 7: FINAL ASSEMBLY: POPULATE player_features
-- Joins all the above views into a single model-ready row.

INSERT INTO player_features (
    player_id,
    draft_year,
    pick_overall,
    age_at_draft,

    -- combine
    wingspan_height_ratio,
    max_vertical,
    lane_agility_time,
    body_fat_pct,

    -- college (final season, conference-adjusted)
    adj_pts_per36,
    adj_reb_per36,
    adj_ast_per36,
    ast_to_ratio,
    ts_pct,
    usg_pct,
    win_shares,
    bpm,
    conference_tier,

    -- trends
    pts_yoy_delta,
    win_shares_yoy_delta,

    -- outcome label
    outcome
)
SELECT
    dc.player_id,
    dc.draft_year,
    dc.pick_overall,
    dc.age_at_draft,

    -- combine (NULL if player didn't attend)
    vc.wingspan_height_ratio,
    vc.max_vertical,
    vc.lane_agility_time,
    vc.body_fat_pct,

    -- conference-adjusted college stats
    ca.adj_pts_per36,
    ca.adj_reb_per36,
    ca.adj_ast_per36,
    fcs.ast_to_ratio,
    fcs.ts_pct,
    fcs.usg_pct,
    fcs.win_shares,
    fcs.bpm,
    ca.conference_tier,

    -- trends
    yoy.pts_yoy_delta,
    yoy.ws_yoy_delta,

    -- label
    ol.outcome

FROM v_draft_context             dc
LEFT JOIN v_final_college_season fcs  USING (player_id)
LEFT JOIN v_conference_adjusted  ca   USING (player_id)
LEFT JOIN v_yoy_trends           yoy  USING (player_id)
LEFT JOIN v_combine              vc   USING (player_id)
LEFT JOIN outcome_labels         ol   USING (player_id)

-- Only include players who have at least some college data
-- (filters out foreign players drafted without NCAA stats)
-- (Way to improve model in future is to extend to international backgrounds)
WHERE fcs.player_id IS NOT NULL
  AND ol.outcome IS NOT NULL

ON CONFLICT (player_id) DO UPDATE SET
    draft_year              = EXCLUDED.draft_year,
    pick_overall            = EXCLUDED.pick_overall,
    age_at_draft            = EXCLUDED.age_at_draft,
    wingspan_height_ratio   = EXCLUDED.wingspan_height_ratio,
    max_vertical            = EXCLUDED.max_vertical,
    lane_agility_time       = EXCLUDED.lane_agility_time,
    body_fat_pct            = EXCLUDED.body_fat_pct,
    adj_pts_per36           = EXCLUDED.adj_pts_per36,
    adj_reb_per36           = EXCLUDED.adj_reb_per36,
    adj_ast_per36           = EXCLUDED.adj_ast_per36,
    ast_to_ratio            = EXCLUDED.ast_to_ratio,
    ts_pct                  = EXCLUDED.ts_pct,
    usg_pct                 = EXCLUDED.usg_pct,
    win_shares              = EXCLUDED.win_shares,
    bpm                     = EXCLUDED.bpm,
    conference_tier         = EXCLUDED.conference_tier,
    pts_yoy_delta           = EXCLUDED.pts_yoy_delta,
    win_shares_yoy_delta    = EXCLUDED.win_shares_yoy_delta,
    outcome                 = EXCLUDED.outcome,
    feature_generated_at    = NOW();


-- SANITY CHECKS: Ways to verify the resulting feature set looks okay

-- 1. Row count and outcome distribution
SELECT
    outcome,
    COUNT(*)                                AS n,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM player_features
GROUP BY outcome
ORDER BY n DESC;

-- 2. Null rates per feature column
-- High nulls in combine columns are expected (~40-50% don't attend).
-- High nulls in college columns signal a scraping problem.
SELECT
    COUNT(*)                                                    AS total_players,
    COUNT(*) FILTER (WHERE adj_pts_per36       IS NULL)         AS null_pts_per36,
    COUNT(*) FILTER (WHERE ts_pct              IS NULL)         AS null_ts_pct,
    COUNT(*) FILTER (WHERE bpm                 IS NULL)         AS null_bpm,
    COUNT(*) FILTER (WHERE wingspan_height_ratio IS NULL)       AS null_wingspan,
    COUNT(*) FILTER (WHERE max_vertical        IS NULL)         AS null_vertical,
    COUNT(*) FILTER (WHERE pts_yoy_delta       IS NULL)         AS null_yoy_delta
FROM player_features;

-- 3. Feature distribution by outcome
-- Stars should have higher BPM, better TS%, lower pick number than busts.
SELECT
    outcome,
    ROUND(AVG(pick_overall),       1)   AS avg_pick,
    ROUND(AVG(age_at_draft),       2)   AS avg_age,
    ROUND(AVG(adj_pts_per36),      2)   AS avg_adj_pts36,
    ROUND(AVG(ts_pct),             3)   AS avg_ts,
    ROUND(AVG(bpm),                2)   AS avg_bpm,
    ROUND(AVG(win_shares),         2)   AS avg_college_ws,
    ROUND(AVG(wingspan_height_ratio), 3) AS avg_wng_ratio,
    ROUND(AVG(max_vertical),       1)   AS avg_vert
FROM player_features
GROUP BY outcome
ORDER BY avg_pick ASC;

-- 4. Draft year coverage (make sure all years loaded)
SELECT draft_year, COUNT(*) AS players
FROM player_features
GROUP BY draft_year
ORDER BY draft_year;