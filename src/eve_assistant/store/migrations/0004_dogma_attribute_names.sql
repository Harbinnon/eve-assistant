-- Dogma attribute names, so rules can refer to attributes by name
-- (e.g. miningBargeBonusOreMiningYield) instead of magic numbers.
CREATE TABLE sde_dogma_attributes (
    attribute_id INTEGER PRIMARY KEY,
    name         TEXT NOT NULL
);
CREATE INDEX sde_dogma_attributes_name ON sde_dogma_attributes (name);
