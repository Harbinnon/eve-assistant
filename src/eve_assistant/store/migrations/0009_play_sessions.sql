-- Play sessions: when the player started and stopped, optionally following
-- a plan. A snapshot is taken at both ends, so what happened in between can
-- be worked out later. Both tables are append-only: a session is open until
-- it has a row in play_session_ends.
CREATE TABLE play_sessions (
    session_id     INTEGER PRIMARY KEY,
    character_id   INTEGER NOT NULL REFERENCES characters(character_id),
    plan_id        INTEGER REFERENCES plans(plan_id),
    started_at     TEXT NOT NULL,       -- ISO UTC, when the player pressed start
    start_problems TEXT NOT NULL        -- JSON list: snapshot problems at the start
);

CREATE TABLE play_session_ends (
    session_id   INTEGER PRIMARY KEY REFERENCES play_sessions(session_id),
    ended_at     TEXT NOT NULL,         -- ISO UTC, when play stopped
    recorded_at  TEXT NOT NULL,         -- when the end was recorded (later, if stale)
    how          TEXT NOT NULL CHECK (how IN ('finished', 'cancelled')),
    end_problems TEXT NOT NULL          -- JSON list: snapshot problems at the end
);

CREATE INDEX play_sessions_character ON play_sessions (character_id, started_at);

CREATE TRIGGER play_sessions_no_update BEFORE UPDATE ON play_sessions
BEGIN SELECT RAISE(ABORT, 'play sessions are append-only'); END;
CREATE TRIGGER play_sessions_no_delete BEFORE DELETE ON play_sessions
BEGIN SELECT RAISE(ABORT, 'play sessions are append-only'); END;
CREATE TRIGGER play_session_ends_no_update BEFORE UPDATE ON play_session_ends
BEGIN SELECT RAISE(ABORT, 'play session ends are append-only'); END;
CREATE TRIGGER play_session_ends_no_delete BEFORE DELETE ON play_session_ends
BEGIN SELECT RAISE(ABORT, 'play session ends are append-only'); END;
