-- What a loyalty point is worth at each NPC corporation's LP store (roadmap
-- 5.3), from ESI's public offers and CCP's average prices; refreshed daily
-- when a plan needs it. The latest reading only.
CREATE TABLE lp_store_values (
    corporation_id INTEGER PRIMARY KEY,
    isk_per_lp     REAL,              -- NULL when no offer could be priced
    offers_priced  INTEGER NOT NULL,
    example        TEXT,              -- JSON: a typical good offer, for "why?"
    fetched_at     TEXT NOT NULL
) WITHOUT ROWID;
