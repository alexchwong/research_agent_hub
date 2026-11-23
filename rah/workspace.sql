CREATE TABLE chat (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(id),
    title TEXT NOT NULL,
    app TEXT NOT NULL CHECK (app IN ('ldr','gptr','pqa')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, id)
) STRICT;

CREATE TABLE chat_entry (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chat(id),
    app TEXT NOT NULL CHECK (app IN ('ldr','gptr','pqa')),
    action TEXT NOT NULL CHECK (action IN ('managed','native')),
    mode TEXT,
    question TEXT NOT NULL,
    quote TEXT NOT NULL,
    intent TEXT NOT NULL,
    state TEXT NOT NULL,
    input_ids TEXT NOT NULL DEFAULT '[]',
    result_ids TEXT NOT NULL DEFAULT '[]',
    operation_id TEXT,
    run_id TEXT,
    preview_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX chat_project ON chat(project_id, updated_at);
CREATE INDEX entry_chat ON chat_entry(chat_id);
CREATE UNIQUE INDEX entry_active_managed ON chat_entry(chat_id)
    WHERE action='managed' AND state IN ('Preparing','Awaiting approval','Running');

ALTER TABLE artifact ADD COLUMN chat_id TEXT REFERENCES chat(id);
