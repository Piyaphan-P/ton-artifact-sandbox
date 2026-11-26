-- Reference only: generated empty schema, no data. App init owns migrations.

CREATE TABLE app_resources(project TEXT PRIMARY KEY,updated INTEGER,cpu REAL,memory INTEGER,memory_limit INTEGER,state TEXT);

CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created INTEGER NOT NULL);

CREATE TABLE clients(id TEXT PRIMARY KEY, name TEXT NOT NULL, redirects TEXT NOT NULL, created INTEGER NOT NULL);

CREATE TABLE deployments(id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), upload TEXT REFERENCES uploads(id), email TEXT NOT NULL, status TEXT NOT NULL, idem TEXT NOT NULL, container TEXT, error TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL, env_snapshot TEXT, UNIQUE(project,email,idem));

CREATE TABLE flows(id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, expires INTEGER NOT NULL);

CREATE TABLE limits(key TEXT PRIMARY KEY, n INTEGER NOT NULL, expires INTEGER NOT NULL);

CREATE TABLE members(project TEXT REFERENCES projects(id), email TEXT REFERENCES users(email), role TEXT NOT NULL, PRIMARY KEY(project,email));

CREATE TABLE platform_usage(hour INTEGER,email TEXT,channel TEXT,calls INTEGER DEFAULT 0,PRIMARY KEY(hour,email,channel));

CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, runtime TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'private', active_release TEXT, env TEXT, created INTEGER NOT NULL, owner_email TEXT);

CREATE TABLE telemetry_meta(key TEXT PRIMARY KEY,value INTEGER);

CREATE TABLE tokens(hash TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL, client TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '', resource TEXT NOT NULL DEFAULT '', project TEXT, expires INTEGER NOT NULL);

CREATE TABLE traffic(hour INTEGER,project TEXT,email TEXT,requests INTEGER DEFAULT 0,bytes INTEGER DEFAULT 0,errors4 INTEGER DEFAULT 0,errors5 INTEGER DEFAULT 0,duration_ms REAL DEFAULT 0,PRIMARY KEY(hour,project,email));

CREATE TABLE traffic_cursor(container TEXT PRIMARY KEY,updated INTEGER);

CREATE TABLE traffic_seen(id TEXT PRIMARY KEY,created INTEGER);

CREATE TABLE uploads(id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), email TEXT NOT NULL, token_hash TEXT NOT NULL, status TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, sha256 TEXT, expires INTEGER NOT NULL, created INTEGER NOT NULL);

CREATE TABLE users(email TEXT PRIMARY KEY, sub TEXT UNIQUE, role TEXT NOT NULL DEFAULT 'member', enabled INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL, permissions TEXT);

CREATE TABLE worker(id TEXT PRIMARY KEY, heartbeat INTEGER NOT NULL);
