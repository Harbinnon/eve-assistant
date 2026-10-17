-- Plan steps ticked off in the session view while playing. Append-only:
-- un-ticking a step adds a row, and the latest row for a step in a session
-- says whether it is done.
CREATE TABLE session_steps (
    mark_id    INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES play_sessions(session_id),
    item_id    INTEGER NOT NULL REFERENCES plan_items(item_id),
    done       INTEGER NOT NULL CHECK (done IN (0, 1)),
    marked_at  TEXT NOT NULL
);
CREATE INDEX session_steps_by_session ON session_steps (session_id, mark_id);

CREATE TRIGGER session_steps_no_update BEFORE UPDATE ON session_steps
BEGIN SELECT RAISE(ABORT, 'session steps are append-only'); END;
CREATE TRIGGER session_steps_no_delete BEFORE DELETE ON session_steps
BEGIN SELECT RAISE(ABORT, 'session steps are append-only'); END;
