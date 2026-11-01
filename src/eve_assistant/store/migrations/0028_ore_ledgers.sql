-- Shared ore ledgers (design 4.11): the owner's own bookkeeping of ore mined
-- with friends, pooled and sold in one trip, so the profit can be split.
-- Every name, role and ledger title here is owner-written: typed by the
-- owner, never read from EVE. A ledger can be corrected while it's open.
CREATE TABLE ore_ledgers (
    ledger_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,      -- owner-written title
    opened_at TEXT NOT NULL,
    closed_at TEXT,               -- when the sale was entered; NULL while open
    sold_for  REAL                -- ISK the pooled ore sold for (owner-entered)
);

-- Who handed over what ore, and how much (units).
CREATE TABLE ore_ledger_entries (
    entry_id  INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ore_ledgers (ledger_id),
    person    TEXT NOT NULL,      -- owner-written name
    type_id   INTEGER NOT NULL,
    quantity  INTEGER NOT NULL CHECK (quantity > 0),
    added_at  TEXT NOT NULL
);
CREATE INDEX ore_ledger_entries_by_ledger ON ore_ledger_entries (ledger_id);

-- Optional roles (booster, hauler, escort...); a person may hold several.
CREATE TABLE ore_ledger_roles (
    ledger_id INTEGER NOT NULL REFERENCES ore_ledgers (ledger_id),
    person    TEXT NOT NULL,
    role      TEXT NOT NULL,
    PRIMARY KEY (ledger_id, person, role)
);

-- Optional: a role's share of the whole profit, in percent.
CREATE TABLE ore_ledger_shares (
    ledger_id INTEGER NOT NULL REFERENCES ore_ledgers (ledger_id),
    role      TEXT NOT NULL,
    percent   REAL NOT NULL CHECK (percent > 0 AND percent <= 100),
    PRIMARY KEY (ledger_id, role)
);

-- Payments ticked off once the ore is sold.
CREATE TABLE ore_ledger_payments (
    ledger_id INTEGER NOT NULL REFERENCES ore_ledgers (ledger_id),
    person    TEXT NOT NULL,
    paid_at   TEXT NOT NULL,
    PRIMARY KEY (ledger_id, person)
);
