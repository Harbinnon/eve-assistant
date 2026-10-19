-- The trade tool's market data (roadmap 5.5), in its own file next to the
-- main database (var/market.sqlite3), attached as "market". All of it can be
-- fetched again from ESI, so it isn't migrated or backed up: a newer layout
-- is simply recreated (MARKET_SCHEMA_VERSION in store/__init__.py).

-- Every order at the trade hubs (and accessible player structures in their
-- systems), as of the region's last scan. ESI's regional orders never name
-- who placed them.
CREATE TABLE IF NOT EXISTS market.orders (
    order_id      INTEGER PRIMARY KEY,
    region_id     INTEGER NOT NULL,
    location_id   INTEGER NOT NULL,
    type_id       INTEGER NOT NULL,
    is_buy        INTEGER NOT NULL,
    price         REAL NOT NULL,
    volume_remain INTEGER NOT NULL,
    min_volume    INTEGER NOT NULL,
    issued        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS market.orders_book ON orders (location_id, type_id, is_buy, price);
CREATE INDEX IF NOT EXISTS market.orders_region ON orders (region_id);

CREATE TABLE IF NOT EXISTS market.scans (
    region_id  INTEGER PRIMARY KEY,
    scanned_at TEXT NOT NULL,
    orders     INTEGER NOT NULL,
    pages      INTEGER NOT NULL
);

-- What trades in a region a day, from ESI's market history (last 30 days).
CREATE TABLE IF NOT EXISTS market.volumes (
    region_id     INTEGER NOT NULL,
    type_id       INTEGER NOT NULL,
    daily_volume  REAL NOT NULL,
    average_price REAL,
    days          INTEGER NOT NULL,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (region_id, type_id)
);

CREATE TABLE IF NOT EXISTS market.meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
