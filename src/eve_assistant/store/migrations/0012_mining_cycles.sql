-- Mining cycles read from EVE's local game logs (local mode, optional).
-- Only the owner's own mining lines are kept: no other line of the log, and
-- only logs whose listener is one of the owner's characters.

-- How far each log file has been read. Logs grow while the game runs, so a
-- file is read again from where it stopped. This is bookkeeping, not history.
-- Files whose listener isn't one of the owner's characters are not read
-- past the header; they are looked at again if that character logs in later.
CREATE TABLE gamelog_files (
    file_name    TEXT PRIMARY KEY,
    listener     TEXT,                   -- character name from the log header
    lines_read   INTEGER NOT NULL,       -- complete lines read so far
    read_at      TEXT NOT NULL
);

CREATE TABLE mining_cycles (
    cycle_id     INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES characters(character_id),
    at           TEXT NOT NULL,          -- ISO UTC (EVE time)
    kind         TEXT NOT NULL CHECK (kind IN ('mined', 'critical', 'residue')),
    ore          TEXT,                   -- ore name as logged; NULL for residue lines
    type_id      INTEGER,                -- from the static data, if the name is known
    units        INTEGER NOT NULL,
    file_name    TEXT NOT NULL,
    line_number  INTEGER NOT NULL,
    UNIQUE (file_name, line_number, kind)
);

CREATE INDEX mining_cycles_character ON mining_cycles (character_id, at);

CREATE TRIGGER mining_cycles_no_update BEFORE UPDATE ON mining_cycles
BEGIN SELECT RAISE(ABORT, 'mining cycles are append-only'); END;
CREATE TRIGGER mining_cycles_no_delete BEFORE DELETE ON mining_cycles
BEGIN SELECT RAISE(ABORT, 'mining cycles are append-only'); END;
