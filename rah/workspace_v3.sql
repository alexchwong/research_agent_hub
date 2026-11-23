CREATE TABLE scope (id TEXT PRIMARY KEY) STRICT;
INSERT INTO scope SELECT id FROM project;
INSERT INTO scope SELECT id FROM chat WHERE project_id = '00000000-0000-4000-8000-000000000001';

CREATE TABLE chat_v3 (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES project(id),
    title TEXT NOT NULL,
    app TEXT NOT NULL CHECK (app IN ('ldr','gptr','pqa')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    legacy_project_id TEXT REFERENCES project(id),
    trash_state TEXT CHECK (trash_state IN ('trashing','trashed','restoring')),
    trashed_at TEXT
) STRICT;
INSERT INTO chat_v3 (id, project_id, title, app, created_at, updated_at, legacy_project_id)
SELECT id, NULLIF(project_id, '00000000-0000-4000-8000-000000000001'), title, app, created_at, updated_at,
       CASE WHEN project_id = '00000000-0000-4000-8000-000000000001' THEN project_id END FROM chat;
DROP TABLE chat;
ALTER TABLE chat_v3 RENAME TO chat;
CREATE INDEX chat_project ON chat(project_id, updated_at);

CREATE TABLE artifact_v3 (
    project_id TEXT NOT NULL REFERENCES scope(id),
    id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('report', 'pdf', 'answer', 'native_link')),
    app_id TEXT CHECK (app_id IN ('ldr', 'gptr', 'pqa')),
    app_version TEXT,
    original_name TEXT,
    storage_name TEXT,
    native_url TEXT,
    sha256 TEXT,
    size_bytes INTEGER CHECK (size_bytes >= 0),
    run_id TEXT,
    created_at TEXT NOT NULL,
    chat_id TEXT REFERENCES chat(id),
    PRIMARY KEY (project_id, id),
    UNIQUE (project_id, storage_name),
    UNIQUE (project_id, run_id),
    CHECK (
        (kind = 'native_link' AND native_url IS NOT NULL
         AND storage_name IS NULL AND sha256 IS NULL AND size_bytes IS NULL)
        OR (kind <> 'native_link' AND native_url IS NULL
         AND storage_name IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL)
    )
) STRICT;
INSERT INTO artifact_v3 SELECT * FROM artifact;
DROP TABLE artifact;
ALTER TABLE artifact_v3 RENAME TO artifact;
CREATE INDEX artifact_chat ON artifact(chat_id);
ALTER TABLE project ADD COLUMN legacy_hidden INTEGER NOT NULL DEFAULT 0;
UPDATE project SET legacy_hidden=1 WHERE id = '00000000-0000-4000-8000-000000000001'
    AND NOT EXISTS (SELECT 1 FROM artifact WHERE project_id=project.id AND chat_id IS NULL)
    AND NOT EXISTS (SELECT 1 FROM paper WHERE project_id=project.id);
CREATE TABLE workspace_setting (id INTEGER PRIMARY KEY CHECK(id=1), path TEXT NOT NULL) STRICT;
PRAGMA user_version=2;