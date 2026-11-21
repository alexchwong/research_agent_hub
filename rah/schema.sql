PRAGMA foreign_keys = ON;

CREATE TABLE project (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE artifact (
    project_id TEXT NOT NULL REFERENCES project(id),
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
    PRIMARY KEY (project_id, id),
    UNIQUE (project_id, storage_name),
    UNIQUE (project_id, run_id),
    CHECK (
        (kind = 'native_link' AND native_url IS NOT NULL
         AND storage_name IS NULL AND sha256 IS NULL AND size_bytes IS NULL)
        OR
        (kind <> 'native_link' AND native_url IS NULL
         AND storage_name IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL)
    )
) STRICT;

CREATE TABLE paper (
    project_id TEXT NOT NULL REFERENCES project(id),
    id TEXT NOT NULL,
    title TEXT,
    doi TEXT,
    pmid TEXT,
    citation_url TEXT,
    note TEXT,
    source_artifact_id TEXT,
    pdf_artifact_id TEXT,
    abstract_only INTEGER NOT NULL DEFAULT 0 CHECK (abstract_only IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN ('needed', 'supplied', 'skipped')),
    PRIMARY KEY (project_id, id),
    FOREIGN KEY (project_id, source_artifact_id) REFERENCES artifact(project_id, id),
    FOREIGN KEY (project_id, pdf_artifact_id) REFERENCES artifact(project_id, id),
    CHECK ((state = 'supplied') = (pdf_artifact_id IS NOT NULL)),
    CHECK (title IS NOT NULL OR doi IS NOT NULL OR pmid IS NOT NULL
           OR citation_url IS NOT NULL OR note IS NOT NULL)
) STRICT;

PRAGMA user_version = 1;
