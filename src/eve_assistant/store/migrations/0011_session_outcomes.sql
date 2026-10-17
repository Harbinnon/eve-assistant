-- What actually happened in a finished play session, worked out from the
-- snapshots either side and the timestamped wallet entries in between.
-- Append-only: an outcome is recomputed (a new row) while EVE's cached data
-- is still catching up, and stops changing once it is settled.
CREATE TABLE session_outcomes (
    outcome_id   INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES play_sessions(session_id),
    computed_at  TEXT NOT NULL,
    settled      INTEGER NOT NULL CHECK (settled IN (0, 1)),
    content_hash TEXT NOT NULL,         -- of details, to skip identical recomputes
    details      TEXT NOT NULL          -- JSON: facts, plan comparison, data notes
);

CREATE INDEX session_outcomes_latest ON session_outcomes (session_id, outcome_id DESC);

CREATE TRIGGER session_outcomes_no_update BEFORE UPDATE ON session_outcomes
BEGIN SELECT RAISE(ABORT, 'session outcomes are append-only'); END;
CREATE TRIGGER session_outcomes_no_delete BEFORE DELETE ON session_outcomes
BEGIN SELECT RAISE(ABORT, 'session outcomes are append-only'); END;
