-- Multi-session projects: work that takes more than one session.
-- The project row holds its state (open/done/dropped); progress is an
-- append-only log so the history of how long things really took is kept.
CREATE TABLE projects (
    project_id     INTEGER PRIMARY KEY,
    character_id   INTEGER NOT NULL REFERENCES characters(character_id),
    kind           TEXT NOT NULL,          -- haul, courier
    key            TEXT NOT NULL,          -- what it is about (stack, contract)
    title          TEXT NOT NULL,
    total_minutes  REAL NOT NULL,          -- predicted
    isk            REAL NOT NULL,          -- predicted, paid at the end
    deadline       TEXT,                   -- ISO UTC, if any
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'done', 'dropped')),
    created_at     TEXT NOT NULL,
    closed_at      TEXT,
    origin_item_id INTEGER REFERENCES plan_items(item_id),
    details        TEXT NOT NULL           -- JSON: inputs and risk at the start
);

CREATE TABLE project_progress (
    progress_id  INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(project_id),
    recorded_at  TEXT NOT NULL,
    minutes      REAL NOT NULL,
    item_id      INTEGER REFERENCES plan_items(item_id)
);

CREATE TRIGGER project_progress_no_update BEFORE UPDATE ON project_progress
BEGIN SELECT RAISE(ABORT, 'project progress is append-only'); END;
CREATE TRIGGER project_progress_no_delete BEFORE DELETE ON project_progress
BEGIN SELECT RAISE(ABORT, 'project progress is append-only'); END;
