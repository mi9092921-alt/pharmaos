-- Device first-run state (installer M1, decision 7): the wizard's state
-- machine lives in POSTGRESQL, not the filesystem — `pgdata` existence
-- proves nothing after a cluster swap, while a RESTORED database brings its
-- own state back, so a recovered device skips the wizard automatically.
-- Keys: setup_complete (0/1), setup_version, last_completed_step.
CREATE TABLE installation_state (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO installation_state (key, value) VALUES
    ('setup_complete',      '0'),
    ('setup_version',       '1'),
    ('last_completed_step', 'none');
