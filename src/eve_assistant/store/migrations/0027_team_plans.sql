-- Plans for characters played together (roadmap 8.4). Each character's part
-- is an ordinary plan (so sessions, outcomes and learning work unchanged);
-- a team plan links them, with the shape chosen (side by side, or one mining
-- while another hauls) and what the other shape would have made.
-- Append-only, like plans.
CREATE TABLE team_plans (
    team_id           INTEGER PRIMARY KEY,
    created_at        TEXT NOT NULL,
    minutes_available REAL NOT NULL,
    shape             TEXT NOT NULL,     -- side_by_side or mine_and_haul
    details           TEXT NOT NULL      -- JSON: value, the other shape, notes, options
);

CREATE TABLE team_plan_members (
    team_id  INTEGER NOT NULL REFERENCES team_plans(team_id),
    plan_id  INTEGER NOT NULL UNIQUE REFERENCES plans(plan_id),
    role     TEXT NOT NULL,              -- solo, miner or hauler
    PRIMARY KEY (team_id, plan_id)
);

CREATE TRIGGER team_plans_no_update BEFORE UPDATE ON team_plans
BEGIN SELECT RAISE(ABORT, 'team plans are append-only'); END;
CREATE TRIGGER team_plans_no_delete BEFORE DELETE ON team_plans
BEGIN SELECT RAISE(ABORT, 'team plans are append-only'); END;
CREATE TRIGGER team_plan_members_no_update BEFORE UPDATE ON team_plan_members
BEGIN SELECT RAISE(ABORT, 'team plans are append-only'); END;
CREATE TRIGGER team_plan_members_no_delete BEFORE DELETE ON team_plan_members
BEGIN SELECT RAISE(ABORT, 'team plans are append-only'); END;
