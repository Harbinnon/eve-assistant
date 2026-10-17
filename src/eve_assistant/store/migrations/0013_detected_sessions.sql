-- Sessions noticed from EVE's login and logout times that the player said
-- were not real sessions, so they aren't offered again. Recorded sessions
-- need no entry here: a detected session that overlaps one is never offered.
CREATE TABLE detected_session_dismissals (
    character_id INTEGER NOT NULL REFERENCES characters(character_id),
    login_at     TEXT NOT NULL,          -- ISO UTC, as EVE reported it
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (character_id, login_at)
);
