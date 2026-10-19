-- Alerts sent (roadmap 7.1), so each is sent once. Append-only.
CREATE TABLE sent_alerts (
    alert_id INTEGER PRIMARY KEY,
    key      TEXT NOT NULL UNIQUE,     -- what it was about, e.g. job:123
    channel  TEXT NOT NULL CHECK (channel IN ('discord', 'ntfy')),
    text     TEXT NOT NULL,
    sent_at  TEXT NOT NULL
);

CREATE TRIGGER sent_alerts_no_update BEFORE UPDATE ON sent_alerts
BEGIN SELECT RAISE(ABORT, 'sent alerts are append-only'); END;
CREATE TRIGGER sent_alerts_no_delete BEFORE DELETE ON sent_alerts
BEGIN SELECT RAISE(ABORT, 'sent alerts are append-only'); END;
