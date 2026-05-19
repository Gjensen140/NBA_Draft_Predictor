-- DB Schema for the NBA Draft Outcome Predictor
-- Run with: psql -d nba_draft -f schema.sql

CREATE SCHEMA IF NOT EXISTS nba;
SET search_path TO nba;

-- -------------------------------------------------------------
-- PLAYERS
-- Core identity table. One row per player.
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS players (
    player_id       SERIAL PRIMARY KEY,
    bref_id         VARCHAR(50) UNIQUE NOT NULL,   -- Basketball Reference slug (e.g. 'jamesle01')
    nba_person_id   INTEGER UNIQUE,                -- NBA Stats API person_id
    full_name       VARCHAR(100) NOT NULL,
    birthdate       DATE,
    birthplace      VARCHAR(100),
    height_inches   NUMERIC(4,1),
    weight_lbs      INTEGER,
    position        VARCHAR(20),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- DRAFT table: one row per pick

CREATE TABLE IF NOT EXISTS draft_picks (
    pick_id         SERIAL PRIMARY KEY,
    player_id       INTEGER REFERENCES players(player_id),
    draft_year      SMALLINT NOT NULL,
    draft_round     SMALLINT NOT NULL,
    pick_overall    SMALLINT NOT NULL,
    team_abbr       VARCHAR(5),
    college         VARCHAR(100),
    college_years   SMALLINT,              -- how many seasons played in college
    age_at_draft    NUMERIC(4,1),
    UNIQUE (draft_year, pick_overall)
);

-- COMBINE MEASUREMENTS table: Physical/athletic data from NBA Draft Combine

CREATE TABLE IF NOT EXISTS combine_measurements (
    combine_id          SERIAL PRIMARY KEY,
    player_id           INTEGER REFERENCES players(player_id),
    draft_year          SMALLINT NOT NULL,
    height_no_shoes     NUMERIC(4,1),      -- inches
    height_with_shoes   NUMERIC(4,1),
    wingspan            NUMERIC(4,1),
    standing_reach      NUMERIC(4,1),
    weight_lbs          NUMERIC(5,1),
    body_fat_pct        NUMERIC(4,1),
    hand_length         NUMERIC(4,1),      -- inches
    hand_width          NUMERIC(4,1),
    standing_vertical   NUMERIC(4,1),      -- inches
    max_vertical        NUMERIC(4,1),
    lane_agility_time   NUMERIC(5,3),      -- seconds (lower = better)
    shuttle_run_time    NUMERIC(5,3),
    three_quarter_sprint NUMERIC(5,3),
    bench_press_reps    SMALLINT,          -- 185 lb reps
    wingspan_height_ratio NUMERIC(5,3)     -- computed: wingspan / height_with_shoes
        GENERATED ALWAYS AS (wingspan / NULLIF(height_with_shoes, 0)) STORED,
    UNIQUE (player_id, draft_year)
);

-- COLLEGE SEASONS
-- Per-season college stats. Multiple rows per player.

CREATE TABLE IF NOT EXISTS college_seasons (
    college_season_id   SERIAL PRIMARY KEY,
    player_id           INTEGER REFERENCES players(player_id),
    season              SMALLINT NOT NULL,     -- e.g. 2019 = 2018-19 season
    college             VARCHAR(100),
    conference          VARCHAR(50),
    games_played        SMALLINT,
    games_started       SMALLINT,
    minutes_per_game    NUMERIC(4,1),
    -- per-game box score
    pts_per_game        NUMERIC(4,1),
    reb_per_game        NUMERIC(4,1),
    ast_per_game        NUMERIC(4,1),
    stl_per_game        NUMERIC(4,1),
    blk_per_game        NUMERIC(4,1),
    tov_per_game        NUMERIC(4,1),
    -- shooting
    fg_pct              NUMERIC(5,3),
    three_pt_pct        NUMERIC(5,3),
    ft_pct              NUMERIC(5,3),
    -- advanced
    per                 NUMERIC(5,2),          -- Player Efficiency Rating
    ts_pct              NUMERIC(5,3),          -- True Shooting %
    ast_pct             NUMERIC(5,2),          -- Assist %
    tov_pct             NUMERIC(5,2),          -- Turnover %
    usg_pct             NUMERIC(5,2),          -- Usage %
    ow_shares           NUMERIC(5,2),          -- Offensive Win Shares
    dw_shares           NUMERIC(5,2),          -- Defensive Win Shares
    win_shares          NUMERIC(5,2),
    bpm                 NUMERIC(5,2),          -- Box Plus/Minus
    UNIQUE (player_id, season, college)
);

-- -------------------------------------------------------------
-- NBA CAREER SEASONS
-- Per-season NBA stats for each player. Used for outcome labeling.
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nba_seasons (
    nba_season_id   SERIAL PRIMARY KEY,
    player_id       INTEGER REFERENCES players(player_id),
    season          SMALLINT NOT NULL,     -- e.g. 2020 = 2019-20
    team_abbr       VARCHAR(5),
    age             SMALLINT,
    games_played    SMALLINT,
    games_started   SMALLINT,
    minutes_per_game NUMERIC(4,1),
    -- per-game box score
    pts_per_game    NUMERIC(4,1),
    reb_per_game    NUMERIC(4,1),
    ast_per_game    NUMERIC(4,1),
    stl_per_game    NUMERIC(4,1),
    blk_per_game    NUMERIC(4,1),
    tov_per_game    NUMERIC(4,1),
    -- shooting
    fg_pct          NUMERIC(5,3),
    three_pt_pct    NUMERIC(5,3),
    ft_pct          NUMERIC(5,3),
    -- advanced
    per             NUMERIC(5,2),
    ts_pct          NUMERIC(5,3),
    win_shares      NUMERIC(5,2),
    ws_per_48       NUMERIC(6,4),
    bpm             NUMERIC(5,2),
    vorp            NUMERIC(5,2),          -- Value Over Replacement Player
    UNIQUE (player_id, season, team_abbr)
);

-- -------------------------------------------------------------
-- OUTCOME LABELS
-- Ground-truth target variable for the classifier.
-- Populated by a labeling script after career data is loaded.
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcome_labels (
    label_id            SERIAL PRIMARY KEY,
    player_id           INTEGER REFERENCES players(player_id) UNIQUE,
    career_win_shares   NUMERIC(6,2),      -- total WS through age 26 (or career end)
    peak_ws_per_48      NUMERIC(6,4),      -- best single-season WS/48
    all_star_appearances SMALLINT DEFAULT 0,
    seasons_played      SMALLINT,
    outcome             VARCHAR(20)        -- 'bust' | 'rotation' | 'starter' | 'star'
        CHECK (outcome IN ('bust', 'rotation', 'starter', 'star')),
    labeled_at          TIMESTAMPTZ DEFAULT NOW()
);

-- -------------------------------------------------------------
-- CONFERENCE STRENGTH
-- Lookup table for college conference SOS adjustments.
-- Manually seeded or scraped yearly.
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conference_strength (
    conf_strength_id    SERIAL PRIMARY KEY,
    conference          VARCHAR(50) NOT NULL,
    season              SMALLINT NOT NULL,
    strength_rating     NUMERIC(5,3),      -- KenPom Derived
    tier                SMALLINT,          -- 1 = power, 2 = mid, 3 = low-major
    UNIQUE (conference, season)
);

-- FEATURE STORE table (materialized at training time)
-- One row per player: denormalized + model-ready.
-- Populated by feature_engineering.sql
CREATE TABLE IF NOT EXISTS player_features (
    feature_id              SERIAL PRIMARY KEY,
    player_id               INTEGER REFERENCES players(player_id) UNIQUE,
    draft_year              SMALLINT,
    pick_overall            SMALLINT,
    age_at_draft            NUMERIC(4,1),

    -- combine
    wingspan_height_ratio   NUMERIC(5,3),
    max_vertical            NUMERIC(4,1),
    lane_agility_time       NUMERIC(5,3),
    body_fat_pct            NUMERIC(4,1),

    -- college (final season, adjusted)
    adj_pts_per36           NUMERIC(5,2),  -- conference-adjusted per-36
    adj_reb_per36           NUMERIC(5,2),
    adj_ast_per36           NUMERIC(5,2),
    ast_to_ratio            NUMERIC(5,2),
    ts_pct                  NUMERIC(5,3),
    usg_pct                 NUMERIC(5,2),
    win_shares              NUMERIC(5,2),
    bpm                     NUMERIC(5,2),
    conference_tier         SMALLINT,

    -- trend (final vs. penultimate season)
    pts_yoy_delta           NUMERIC(5,2),
    win_shares_yoy_delta    NUMERIC(5,2),

    -- outcome (label)
    outcome                 VARCHAR(20),
    feature_generated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- -------------------------------------------------------------
-- INDEXES
-- -------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_draft_picks_year      ON draft_picks(draft_year);
CREATE INDEX IF NOT EXISTS idx_college_seasons_player ON college_seasons(player_id);
CREATE INDEX IF NOT EXISTS idx_nba_seasons_player     ON nba_seasons(player_id);
CREATE INDEX IF NOT EXISTS idx_outcome_labels_outcome ON outcome_labels(outcome);