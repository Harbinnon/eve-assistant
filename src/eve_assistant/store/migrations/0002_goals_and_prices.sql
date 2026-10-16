-- Goals belong to the install (they can name a character, or not).
CREATE TABLE goals (
    goal_id      INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('fly_ship', 'skill', 'isk')),
    character_id INTEGER REFERENCES characters(character_id),
    type_id      INTEGER,          -- ship or skill, for fly_ship and skill goals
    level        INTEGER,          -- for skill goals
    isk          REAL,             -- for isk goals
    label        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'done', 'dropped')),
    status_at    TEXT
);

-- CCP's daily average prices for every item (GET /markets/prices). Rough,
-- but enough for "about how much does this ship cost". Hub order books come
-- with the planner.
CREATE TABLE market_prices (
    type_id        INTEGER PRIMARY KEY,
    average_price  REAL,
    adjusted_price REAL,
    fetched_at     TEXT NOT NULL
);
