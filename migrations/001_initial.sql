CREATE TABLE IF NOT EXISTS repos (
    id SERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT UNIQUE NOT NULL,
    stars INTEGER,
    tier INTEGER NOT NULL DEFAULT 2,
    score FLOAT DEFAULT 0.5,
    last_scanned_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gaps (
    id SERIAL PRIMARY KEY,
    repo_id INTEGER REFERENCES repos(id),
    wedge_type TEXT NOT NULL,
    description TEXT NOT NULL,
    effort TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prs (
    id SERIAL PRIMARY KEY,
    gap_id INTEGER REFERENCES gaps(id),
    repo_id INTEGER REFERENCES repos(id),
    pr_url TEXT UNIQUE NOT NULL,
    pr_number INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    wedge_type TEXT NOT NULL,
    submitted_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ramp_state (
    id SERIAL PRIMARY KEY,
    date DATE UNIQUE NOT NULL DEFAULT CURRENT_DATE,
    cap INTEGER NOT NULL DEFAULT 5,
    submitted_today INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wedge_hypotheses (
    id SERIAL PRIMARY KEY,
    wedge_type TEXT UNIQUE NOT NULL,
    submitted_count INTEGER DEFAULT 0,
    accepted_count INTEGER DEFAULT 0,
    acceptance_rate FLOAT DEFAULT 0.0,
    avg_effort TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS failures (
    id SERIAL PRIMARY KEY,
    component TEXT NOT NULL,
    repo_full_name TEXT,
    gap_id INTEGER REFERENCES gaps(id),
    error_type TEXT NOT NULL,
    error_detail TEXT,
    attempts INTEGER DEFAULT 1,
    escalated BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed initial ramp state
INSERT INTO ramp_state (date, cap, submitted_today) VALUES (CURRENT_DATE, 5, 0) ON CONFLICT (date) DO NOTHING;

-- Seed litellm wedge hypothesis
INSERT INTO wedge_hypotheses (wedge_type) VALUES ('provider_integration') ON CONFLICT (wedge_type) DO NOTHING;
