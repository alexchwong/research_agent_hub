CREATE TABLE chat_entry_v4 (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chat(id),
    app TEXT NOT NULL CHECK (app IN ('ldr','gptr','pqa','rah')),
    action TEXT NOT NULL CHECK (action IN ('managed','native','checkpoint')),
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
INSERT INTO chat_entry_v4 (id,chat_id,app,action,mode,question,quote,intent,state,input_ids,result_ids,
    operation_id,run_id,preview_id,reason,created_at,updated_at)
SELECT id,chat_id,app,action,mode,question,quote,intent,state,input_ids,result_ids,
    operation_id,run_id,preview_id,reason,created_at,updated_at FROM chat_entry ORDER BY rowid;
DROP TABLE chat_entry;
ALTER TABLE chat_entry_v4 RENAME TO chat_entry;
CREATE INDEX entry_chat ON chat_entry(chat_id);
CREATE UNIQUE INDEX entry_active_managed ON chat_entry(chat_id)
    WHERE action='managed' AND state IN ('Preparing','Awaiting approval','Running');

CREATE TABLE pipeline_run (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES scope(id),
    chat_id TEXT NOT NULL REFERENCES chat(id),
    brief_revision INTEGER NOT NULL CHECK (brief_revision > 0),
    purpose TEXT NOT NULL CHECK (purpose IN ('literature_review','deep_research','rapid_research',
        'independent_corroboration','analyse_my_papers')),
    recipe_version INTEGER NOT NULL CHECK (recipe_version > 0),
    state TEXT NOT NULL CHECK (state IN ('queued','surveying','awaiting_scope','brief_approved','running',
        'waiting_app','waiting_native','awaiting_papers','awaiting_disclosure','reconfirm_recipients',
        'paused','interrupted','succeeded','failed','cancelled')),
    outcome TEXT CHECK (outcome IN ('complete','discovery_only','report_only','survey_as_result')),
    original_question TEXT NOT NULL,
    selected_profile TEXT NOT NULL CHECK (selected_profile IN ('budget','standard','premium')),
    brief_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    UNIQUE (chat_id, brief_revision),
    CHECK ((state = 'succeeded') = (outcome IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX pipeline_run_active ON pipeline_run(chat_id)
    WHERE state NOT IN ('succeeded','failed','cancelled');
CREATE INDEX pipeline_run_chat ON pipeline_run(chat_id, brief_revision);

CREATE TABLE pipeline_step (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_run(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    step_key TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    entry_id TEXT NOT NULL UNIQUE REFERENCES chat_entry(id),
    state TEXT NOT NULL CHECK (state IN ('queued','surveying','running','awaiting_scope','brief_approved',
        'waiting_app','waiting_native','awaiting_papers','awaiting_disclosure','reconfirm_recipients',
        'paused','interrupted','succeeded','failed','cancelled','skipped')),
    queued_at TEXT,
    operation_id TEXT,
    native_result_id TEXT,
    route_fingerprint TEXT,
    approval_id TEXT,
    round INTEGER CHECK (round IS NULL OR round > 0),
    questionnaire_source TEXT CHECK (questionnaire_source IS NULL OR questionnaire_source IN ('model','fallback')),
    error_code TEXT,
    error_detail TEXT CHECK (error_detail IS NULL OR length(error_detail) <= 500),
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (length(detail_json) <= 65536),
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, position, attempt)
) STRICT;
CREATE INDEX pipeline_step_queue ON pipeline_step(state, queued_at, id);

CREATE TABLE pipeline_approval (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_run(id),
    step_id TEXT REFERENCES pipeline_step(id),
    kind TEXT NOT NULL CHECK (kind IN ('public_start','scope','reconfirm','native_launch','native_confirmation',
        'oa_public','oa_private','early_finish','pqa_review','partial_finish','rerun')),
    payload_digest TEXT NOT NULL,
    route_fingerprint TEXT,
    display_json TEXT NOT NULL DEFAULT '{}',
    decision TEXT NOT NULL CHECK (decision IN ('approved','declined')),
    decided_at TEXT NOT NULL,
    expires_at TEXT
) STRICT;
CREATE INDEX pipeline_approval_run ON pipeline_approval(run_id, kind);

CREATE TABLE artifact_v4 (
    project_id TEXT NOT NULL REFERENCES scope(id),
    id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('report', 'pdf', 'answer', 'native_link', 'citations', 'questionnaire')),
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
    step_id TEXT REFERENCES pipeline_step(id),
    role TEXT CHECK (role IS NULL OR role IN ('survey','research_report','discovery_report','citation_export',
        'scope_questionnaire','source_pdf','answer','references','attachment','native_result')),
    media_type TEXT,
    output_key TEXT,
    title TEXT CHECK (title IS NULL OR length(title) <= 300),
    origin_class TEXT NOT NULL DEFAULT 'private_or_unknown'
        CHECK (origin_class IN ('public_native','public_acquired','private_or_unknown','local_generated')),
    PRIMARY KEY (project_id, id),
    UNIQUE (project_id, storage_name),
    CHECK (
        (kind = 'native_link' AND native_url IS NOT NULL
         AND storage_name IS NULL AND sha256 IS NULL AND size_bytes IS NULL)
        OR (kind <> 'native_link' AND native_url IS NULL
         AND storage_name IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL)
    ),
    CHECK ((run_id IS NULL AND step_id IS NULL) OR length(COALESCE(output_key, '')) > 0)
) STRICT;
INSERT INTO artifact_v4 (project_id,id,kind,app_id,app_version,original_name,storage_name,native_url,sha256,
    size_bytes,run_id,created_at,chat_id,output_key)
SELECT project_id,id,kind,app_id,app_version,original_name,storage_name,native_url,sha256,
    size_bytes,run_id,created_at,chat_id,CASE WHEN run_id IS NOT NULL THEN 'primary' END FROM artifact ORDER BY rowid;
DROP TABLE artifact;
ALTER TABLE artifact_v4 RENAME TO artifact;
CREATE INDEX artifact_chat ON artifact(chat_id);
CREATE UNIQUE INDEX artifact_run_output ON artifact(project_id, run_id, output_key) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX artifact_step_output ON artifact(project_id, step_id, output_key) WHERE step_id IS NOT NULL;

CREATE TABLE artifact_parent (
    project_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    PRIMARY KEY (project_id, parent_id, child_id),
    FOREIGN KEY (project_id, parent_id) REFERENCES artifact(project_id, id),
    FOREIGN KEY (project_id, child_id) REFERENCES artifact(project_id, id),
    CHECK (parent_id <> child_id)
) STRICT;

ALTER TABLE paper ADD COLUMN pmcid TEXT;
ALTER TABLE paper ADD COLUMN access_status TEXT NOT NULL DEFAULT 'unresolved'
    CHECK (access_status IN ('unresolved','local_available','oa_acquired','library_link_available','failed_retryable'));
UPDATE paper SET access_status='local_available' WHERE pdf_artifact_id IS NOT NULL;

CREATE TABLE pipeline_paper (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_run(id),
    project_id TEXT NOT NULL REFERENCES scope(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    title TEXT,
    doi TEXT,
    pmid TEXT,
    pmcid TEXT,
    citation_url TEXT,
    note TEXT,
    source_artifact_id TEXT,
    pdf_artifact_id TEXT,
    origin_class TEXT NOT NULL DEFAULT 'private_or_unknown'
        CHECK (origin_class IN ('public_native','public_acquired','private_or_unknown','local_generated')),
    state TEXT NOT NULL DEFAULT 'needed' CHECK (state IN ('needed','supplied','skipped')),
    access_status TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (access_status IN ('unresolved','local_available','oa_acquired','library_link_available','failed_retryable')),
    abstract_only INTEGER NOT NULL DEFAULT 0 CHECK (abstract_only IN (0, 1)),
    confirmed_version INTEGER CHECK (confirmed_version IS NULL OR confirmed_version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, position),
    FOREIGN KEY (project_id, source_artifact_id) REFERENCES artifact(project_id, id),
    FOREIGN KEY (project_id, pdf_artifact_id) REFERENCES artifact(project_id, id),
    CHECK ((state = 'supplied') = (pdf_artifact_id IS NOT NULL)),
    CHECK (title IS NOT NULL OR doi IS NOT NULL OR pmid IS NOT NULL OR pmcid IS NOT NULL
           OR citation_url IS NOT NULL OR note IS NOT NULL)
) STRICT;

CREATE TABLE acquisition (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES scope(id),
    step_id TEXT NOT NULL REFERENCES pipeline_step(id),
    paper_id TEXT NOT NULL REFERENCES pipeline_paper(id),
    attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 100),
    identifier TEXT NOT NULL,
    version INTEGER,
    method TEXT NOT NULL,
    source_url TEXT,
    metadata_url TEXT,
    retrieved_at TEXT NOT NULL,
    access_evidence TEXT,
    licence TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('acquired','existing_match','unresolved','manual_review',
        'choose_version','failed_retryable','rejected')),
    error_code TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','accepted','declined')),
    artifact_id TEXT,
    sha256 TEXT,
    UNIQUE (paper_id, attempt),
    FOREIGN KEY (project_id, artifact_id) REFERENCES artifact(project_id, id)
) STRICT;

CREATE TABLE pipeline_action (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES scope(id),
    chat_id TEXT NOT NULL REFERENCES chat(id),
    run_id TEXT REFERENCES pipeline_run(id),
    action TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE guided_default (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    composer TEXT NOT NULL CHECK (composer IN ('guided')),
    purpose TEXT NOT NULL,
    profile TEXT NOT NULL
) STRICT;
INSERT INTO guided_default VALUES (1, 'guided', 'literature_review', 'standard');
PRAGMA user_version=3