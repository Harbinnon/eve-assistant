-- A ship goal can name the fit it is for (roadmap 8.1): JSON with the fit's
-- name (player-written), where it came from (a saved fit or a pasted one) and
-- its items as [type_id, quantity] pairs. NULL for every other goal.
ALTER TABLE goals ADD COLUMN fit TEXT;
