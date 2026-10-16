-- Plans the planner produced, and the person's responses. Append-only:
-- this is the history the learning in phase 3 depends on.
CREATE TABLE plans (
    plan_id           INTEGER PRIMARY KEY,
    character_id      INTEGER NOT NULL REFERENCES characters(character_id),
    created_at        TEXT NOT NULL,
    minutes_available REAL NOT NULL,
    inputs            TEXT NOT NULL     -- JSON: options the planner was given
);

CREATE TABLE plan_items (
    item_id        INTEGER PRIMARY KEY,
    plan_id        INTEGER NOT NULL REFERENCES plans(plan_id),
    position       INTEGER,            -- order in the plan; NULL for alternatives
    kind           TEXT NOT NULL,      -- sell_hangar, mining, courier, skill
    key            TEXT NOT NULL,      -- stable identifier within the kind
    title          TEXT NOT NULL,
    isk            REAL NOT NULL,      -- predicted, before expected losses
    minutes        REAL NOT NULL,      -- predicted keyboard minutes
    risk_rating    TEXT NOT NULL,      -- green, amber, red
    expected_loss  REAL NOT NULL,
    details        TEXT NOT NULL       -- JSON: structured reasons and inputs
);

-- What the person did with a suggestion: do, not_now, mute.
CREATE TABLE plan_responses (
    response_id  INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES plan_items(item_id),
    response     TEXT NOT NULL CHECK (response IN ('do', 'not_now', 'mute')),
    responded_at TEXT NOT NULL
);

CREATE TRIGGER plans_no_update BEFORE UPDATE ON plans
BEGIN SELECT RAISE(ABORT, 'plans are append-only'); END;
CREATE TRIGGER plans_no_delete BEFORE DELETE ON plans
BEGIN SELECT RAISE(ABORT, 'plans are append-only'); END;
CREATE TRIGGER plan_items_no_update BEFORE UPDATE ON plan_items
BEGIN SELECT RAISE(ABORT, 'plan items are append-only'); END;
CREATE TRIGGER plan_items_no_delete BEFORE DELETE ON plan_items
BEGIN SELECT RAISE(ABORT, 'plan items are append-only'); END;
CREATE TRIGGER plan_responses_no_update BEFORE UPDATE ON plan_responses
BEGIN SELECT RAISE(ABORT, 'plan responses are append-only'); END;
CREATE TRIGGER plan_responses_no_delete BEFORE DELETE ON plan_responses
BEGIN SELECT RAISE(ABORT, 'plan responses are append-only'); END;

-- Muted categories (install-level). Unmuting sets removed_at, so the
-- history of what was muted and when is kept.
CREATE TABLE mutes (
    mute_id    INTEGER PRIMARY KEY,
    category   TEXT NOT NULL,          -- e.g. kind:courier, route:lowsec
    created_at TEXT NOT NULL,
    removed_at TEXT
);
