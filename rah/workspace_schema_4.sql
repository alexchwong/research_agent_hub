ALTER TABLE chat_entry ADD COLUMN usage_json TEXT
    CHECK (usage_json IS NULL OR (json_valid(usage_json) AND length(usage_json) <= 8192));

-- Close everything that was in flight under the old build. Artifacts and terminal rows are untouched.
UPDATE pipeline_step SET state='cancelled', error_code='upgrade',
       error_detail='Closed by the RAH upgrade. Start a new run.',
       finished_at=COALESCE(finished_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
 WHERE state NOT IN ('succeeded','failed','cancelled','skipped');
UPDATE chat_entry SET state='Cancelled', reason='Closed by the RAH upgrade. Start a new run.',
       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
 WHERE id IN (SELECT entry_id FROM pipeline_step WHERE error_code='upgrade');
UPDATE chat_entry SET state='Interrupted', reason='Closed by the RAH upgrade. Start a new request.',
       preview_id=NULL, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
 WHERE state IN ('Preparing','Awaiting approval','Running')
   AND id NOT IN (SELECT entry_id FROM pipeline_step);
UPDATE pipeline_run SET state='cancelled', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
 WHERE state NOT IN ('succeeded','failed','cancelled');
PRAGMA user_version = 4
