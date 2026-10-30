-- Goals in sequence (roadmap 8.2): a goal can follow another of the same
-- character's goals ("a Retriever, then a month of Omega"). Its ISK and
-- training then add to those of the goals before it. NULL: it stands alone.
ALTER TABLE goals ADD COLUMN after_goal_id INTEGER REFERENCES goals(goal_id);
