-- Initial schema. One copy belongs to one person, so there is no user column:
-- data is tagged by character, by account, or belongs to the install.

-- Install-level settings (goals across characters, preferences, ...).
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON
);

-- EVE accounts, as the person groups their characters. ESI does not say
-- which account a character is on.
CREATE TABLE accounts (
    account_id  INTEGER PRIMARY KEY,
    label       TEXT NOT NULL UNIQUE,
    clone_state TEXT NOT NULL DEFAULT 'unknown'
                CHECK (clone_state IN ('unknown', 'alpha', 'omega'))
);

CREATE TABLE characters (
    character_id INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    account_id   INTEGER REFERENCES accounts(account_id) ON DELETE SET NULL,
    first_seen   TEXT NOT NULL      -- ISO 8601 UTC
);

-- Append-only history of what ESI said about a character. A new row is
-- written only when the content changed since the last row of that kind.
CREATE TABLE snapshots (
    snapshot_id  INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES characters(character_id),
    kind         TEXT NOT NULL,     -- skills, skillqueue, wallet, assets, ...
    taken_at     TEXT NOT NULL,     -- ISO 8601 UTC
    content_hash TEXT NOT NULL,
    payload      TEXT NOT NULL      -- JSON as returned by ESI
);
CREATE INDEX snapshots_latest ON snapshots (character_id, kind, snapshot_id DESC);

CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are append-only'); END;
CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are append-only'); END;

-- Every collection attempt, including "unchanged" and failures, so gaps in
-- the history can be told apart from "nothing changed".
CREATE TABLE snapshot_checks (
    check_id     INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES characters(character_id),
    kind         TEXT NOT NULL,
    checked_at   TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('changed', 'unchanged', 'failed')),
    detail       TEXT
);

-- HTTP cache for ESI: ETags and bodies, so repeat requests can be
-- conditional (cheaper on rate limits).
CREATE TABLE esi_cache (
    cache_key  TEXT PRIMARY KEY,   -- method, URL, character
    etag       TEXT,
    expires_at REAL,               -- unix time
    body       TEXT NOT NULL       -- JSON (all pages joined)
);

-- Static data: reference tables replaced as a whole on each SDE update.
CREATE TABLE sde_meta (
    build_number INTEGER NOT NULL,
    release_date TEXT NOT NULL,
    loaded_at    TEXT NOT NULL
);

CREATE TABLE sde_categories (
    category_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    published   INTEGER NOT NULL
);

CREATE TABLE sde_groups (
    group_id    INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL,
    name        TEXT NOT NULL,
    published   INTEGER NOT NULL
);

CREATE TABLE sde_types (
    type_id         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    group_id        INTEGER NOT NULL,
    published       INTEGER NOT NULL,
    volume          REAL,
    packaged_volume REAL,
    capacity        REAL,
    portion_size    INTEGER,
    base_price      REAL,
    market_group_id INTEGER,
    meta_group_id   INTEGER,
    race_id         INTEGER
);
CREATE INDEX sde_types_name ON sde_types (name COLLATE NOCASE);
CREATE INDEX sde_types_group ON sde_types (group_id);

-- Only the dogma attributes the tool uses (see sde.ATTRIBUTE_IDS).
CREATE TABLE sde_type_attributes (
    type_id      INTEGER NOT NULL,
    attribute_id INTEGER NOT NULL,
    value        REAL NOT NULL,
    PRIMARY KEY (type_id, attribute_id)
) WITHOUT ROWID;

-- Alpha clone skill caps, per race grade.
CREATE TABLE sde_clone_grade_skills (
    grade_id    INTEGER NOT NULL,
    grade_name  TEXT NOT NULL,
    skill_id    INTEGER NOT NULL,
    max_level   INTEGER NOT NULL,
    PRIMARY KEY (grade_id, skill_id)
) WITHOUT ROWID;

CREATE TABLE sde_compressible_types (
    type_id            INTEGER PRIMARY KEY,
    compressed_type_id INTEGER NOT NULL
);

CREATE TABLE sde_regions (
    region_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL
);

CREATE TABLE sde_solar_systems (
    system_id        INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    region_id        INTEGER NOT NULL,
    constellation_id INTEGER NOT NULL,
    security_status  REAL NOT NULL,
    security_class   TEXT
);
CREATE INDEX sde_systems_name ON sde_solar_systems (name COLLATE NOCASE);

CREATE TABLE sde_npc_stations (
    station_id               INTEGER PRIMARY KEY,
    system_id                INTEGER NOT NULL,
    owner_id                 INTEGER,
    type_id                  INTEGER,
    reprocessing_efficiency  REAL,
    reprocessing_station_take REAL
);
