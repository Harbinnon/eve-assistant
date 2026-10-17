-- Your own losses (roadmap 4.2), from your killmails. Only killmails in
-- which one of your characters is the victim are kept, and only what was
-- yours: when, where, the ship and what it carried. Attackers are never
-- kept. Losses are facts: append-only.
CREATE TABLE own_losses (
    killmail_id     INTEGER PRIMARY KEY,
    character_id    INTEGER NOT NULL REFERENCES characters(character_id),
    killed_at       TEXT NOT NULL,       -- ISO UTC
    solar_system_id INTEGER NOT NULL,
    ship_type_id    INTEGER NOT NULL,
    items           TEXT NOT NULL,       -- JSON [[type_id, quantity], ...] aboard
    recorded_at     TEXT NOT NULL
);
CREATE TRIGGER own_losses_no_update BEFORE UPDATE ON own_losses
BEGIN SELECT RAISE(ABORT, 'own losses are append-only'); END;
CREATE TRIGGER own_losses_no_delete BEFORE DELETE ON own_losses
BEGIN SELECT RAISE(ABORT, 'own losses are append-only'); END;

-- Killmails already looked at, losses or not, so none is fetched twice.
CREATE TABLE killmails_seen (
    killmail_id INTEGER PRIMARY KEY,
    seen_at     TEXT NOT NULL
);
