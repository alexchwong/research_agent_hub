import json
import os
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib import resources
from pathlib import Path, PurePath
import re
import shutil
import sqlite3
import stat
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from threading import RLock
from uuid import NAMESPACE_URL, uuid4, uuid5

from .common import Problem, atomic, child, file_digest, id_value, is_link, private_directory, read_json, verify_windows_root, write_json


REPORT_LIMIT = 32 * 1024 * 1024
PDF_LIMIT = 256 * 1024 * 1024
FIELDS = ("title", "doi", "pmid", "citation_url", "note")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\[\d+\]\s*|\d+[.)]\s+)(.*)")
DOI = re.compile(r"\b10\.\d{4,9}/[^\s<>]+", re.I)
PMID = re.compile(r"\bPMID\s*:?\s*(\d+)\b", re.I)
URL = re.compile(r"https?://[^\s<>]+", re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def valid_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password and not any(
            char.isspace() or ord(char) < 32 for char in value
        )
    except ValueError:
        return False


def citations(text):
    if not isinstance(text, str) or len(text) > 150_000:
        raise Problem("citations_input", "Paste at most 150,000 characters.")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    markers = any(LIST_ITEM.match(line) for line in lines)
    rows = []
    for line in lines:
        marker = LIST_ITEM.match(line)
        if markers and not marker and rows:
            rows[-1] += " " + line
        else:
            rows.append(marker[1] if marker else line)
    return [{
        "note": line,
        "doi": item(DOI, line),
        "pmid": item(PMID, line),
        "citation_url": item(URL, line),
    } for line in rows[:250]]


def item(pattern, line):
    matches = {match.group(1 if pattern is PMID else 0).rstrip(".,);]") for match in pattern.finditer(line)}
    return next(iter(matches)) if len(matches) == 1 else None


@dataclass(frozen=True)
class CitationParse:
    rows: list
    total: int
    truncated: bool


def structured_citations(path, *, limit=250, overflow="error"):
    """Read citation fields emitted by the pinned LDR RIS exporter.

    Every record in the file is validated; only the first ``limit`` rows are kept. With ``overflow="error"`` more
    than ``limit`` records is rejected (the original behaviour); with ``"truncate"`` the result is marked truncated.
    """
    if overflow not in ("error", "truncate"):
        raise ValueError("overflow")
    rows = []
    row = None
    total = 0
    fields = {"TI": "title", "DO": "doi", "UR": "citation_url", "N1": "note", "AB": "note"}
    try:
        if path.stat().st_size > REPORT_LIMIT:
            raise ValueError
        with path.open(encoding="utf-8-sig") as source:
            for line in source:
                line = line.rstrip()
                if not line:
                    continue
                match = re.fullmatch(r"([0-9A-Z]{2})  -(?: (.*))?", line)
                if not match:
                    raise ValueError
                tag, value = match.group(1), (match.group(2) or "").strip()
                if tag == "TY":
                    if row is not None or not value:
                        raise ValueError
                    row = {}
                elif row is None:
                    raise ValueError
                elif tag == "ER":
                    if not row:
                        raise ValueError
                    url = row.get("citation_url", "")
                    if url:
                        if not valid_url(url):
                            raise ValueError
                        address = urlsplit(url)
                        if address.hostname == "pubmed.ncbi.nlm.nih.gov":
                            match = re.fullmatch(r"/([0-9]+)/?", address.path)
                            if match:
                                row["pmid"] = match.group(1)
                    total += 1
                    if total > limit and overflow == "error":
                        raise ValueError
                    if len(rows) < limit:
                        rows.append(row)
                    row = None
                elif tag in fields and value:
                    field = fields[tag]
                    row[field] = row.get(field, "") + (" " if field in row else "") + value
        if row is not None or not rows:
            raise ValueError
        return CitationParse(rows, total, total > len(rows))
    except (OSError, ValueError) as error:
        raise Problem("unrecognized_export", "Use an LDR RIS export with up to 250 citations, or paste citations instead.") from error


def usable_citation(row):
    """A citation Papers Needed can act on: a title plus a web address or a DOI."""
    return bool((row.get("title") or "").strip() and ((row.get("citation_url") and valid_url(row["citation_url"])) or row.get("doi")))


UNFILED = "00000000-0000-4000-8000-000000000001"
SCHEMA_VERSION = 4
PRESERVED_TABLES = ("project", "scope", "chat", "chat_entry", "artifact", "paper", "workspace_setting")
KINDS = {"pdf": {".pdf"}, "report": {".md", ".txt", ".json", ".ris"}, "answer": {".md"},
         "citations": {".ris", ".bib"}, "questionnaire": {".json"}}
MEDIA = {".md": "text/markdown", ".txt": "text/plain", ".json": "application/json", ".pdf": "application/pdf",
         ".ris": "application/x-research-info-systems", ".bib": "application/x-bibtex"}
ROLES = {"survey", "research_report", "discovery_report", "citation_export", "scope_questionnaire",
         "source_pdf", "answer", "references", "attachment", "native_result"}
ORIGINS = {"public_native", "public_acquired", "private_or_unknown", "local_generated"}
OUTPUT_KEY = re.compile(r"(?:primary|report|citations|questionnaire|answer|references|survey|pdf:[0-9a-f-]{36})")


def stored_version(database):
    """Read the schema version without creating or changing the database."""
    if not database.exists():
        return 0
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def output_id(step, key):
    """Deterministic artifact identity for one output key of one step attempt."""
    return str(uuid5(NAMESPACE_URL, f"rah:step-output:{id_value(step)}:{key}"))


class Projects:
    def __init__(self, root, *, migration_hook=None):
        self.root = root
        self.writer = RLock()
        self.database = child(root, "hub", "state.sqlite3")
        version = stored_version(self.database)
        if version > SCHEMA_VERSION:
            raise Problem("workspace_version", "This workspace was upgraded by a newer RAH release. "
                          "Use that release, or restore the pre-upgrade backup with its matching files.", 409)
        if not self.database.exists():
            self.database.touch(mode=0o600)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                sql = resources.files("rah").joinpath("schema.sql").read_text(encoding="utf-8")
                db.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")

        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='chat'").fetchone():
                migration = resources.files("rah").joinpath("workspace.sql").read_text(encoding="utf-8")
                for statement in migration.split(";"):
                    if statement.strip():
                        db.execute(statement)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 1:
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("BEGIN IMMEDIATE")
                sql = resources.files("rah").joinpath("workspace_v3.sql").read_text(encoding="utf-8")
                for statement in sql.split(";"):
                    if statement.strip():
                        db.execute(statement)
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise Problem("workspace_migration", "Workspace migration found invalid file references.")
        self.upgrade_v3(migration_hook)
        self.upgrade_v4()
        with self.connect() as db:
            saved = db.execute("SELECT path FROM workspace_setting WHERE id=1").fetchone()
            self.workspace_root = self.check_root(Path(saved[0])) if saved else root
            self.prepare_workspace(self.workspace_root)
            for row in db.execute("SELECT * FROM chat"):
                if row["legacy_project_id"] and not row["trash_state"]:
                    source = child(self.directory(UNFILED), "chats", row["id"])
                    target = self.chat_directory(row)
                    if source.exists():
                        if target.exists():
                            raise Problem("workspace_migration", "Both legacy and new chat folders exist. Resolve the conflict before reopening RAH.")
                        source.rename(target)
                if not row["trash_state"]:
                    directory = self.chat_directory(row)
                    # Old versions created this intermediate directory implicitly,
                    # under the already-private workspace, using the caller's umask.
                    if os.name != "nt" and directory.is_dir() and directory.stat().st_uid == os.getuid():
                        directory.chmod(0o700)
                    self.prepare_chat(row)
        self.reconcile_trash()

    def backup_pre_v3(self):
        directory = private_directory(child(self.root, "hub", "backups"))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = child(directory, f"state-pre-v3-{stamp}.sqlite3")
        with closing(sqlite3.connect(self.database)) as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
        os.chmod(target, 0o600)
        with closing(sqlite3.connect(target)) as copy:
            integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
            version = copy.execute("PRAGMA user_version").fetchone()[0]
        if integrity != "ok" or version != 2:
            target.unlink(missing_ok=True)
            raise Problem("workspace_migration", "RAH could not verify a pre-upgrade workspace backup. Nothing was changed.")
        write_json(child(directory, target.stem + ".json"), {
            "label": "Workspace database before schema 3", "created_at": now(), "user_version": version,
            "integrity_check": integrity, "sha256": file_digest(target), "workspace": str(self.root)})
        return target

    def upgrade_v3(self, hook=None):
        """Upgrade schema 2 to 3 in one immediate transaction after a verified backup."""
        with closing(sqlite3.connect(self.database, isolation_level=None, timeout=30)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                return None
            db.execute("PRAGMA foreign_keys=OFF")
            # The reserved lock excludes other writers from the backup through to the commit.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                db.execute("ROLLBACK")
                return None
            backup = None
            try:
                backup = self.backup_pre_v3()
                counts = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in PRESERVED_TABLES}
                identities = {name: db.execute(f"SELECT group_concat(id, ',') FROM (SELECT id FROM {name} ORDER BY id)").fetchone()[0]
                              for name in ("chat_entry", "artifact", "paper")}
                sql = resources.files("rah").joinpath("workspace_schema_3.sql").read_text(encoding="utf-8")
                for statement in sql.split(";"):
                    if statement.strip():
                        db.execute(statement)
                if hook:
                    hook(db)
                after = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in PRESERVED_TABLES}
                moved = {name: db.execute(f"SELECT group_concat(id, ',') FROM (SELECT id FROM {name} ORDER BY id)").fetchone()[0]
                         for name in identities}
                if (after != counts or moved != identities or db.execute("PRAGMA foreign_key_check").fetchone()
                        or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                        or db.execute("PRAGMA user_version").fetchone()[0] != 3):
                    raise Problem("workspace_migration", "Workspace upgrade verification failed.")
                db.execute("COMMIT")
            except BaseException as error:
                db.execute("ROLLBACK")
                location = f" A verified backup is at {backup}." if backup else ""
                raise Problem("workspace_migration", "RAH could not upgrade the workspace database and left it unchanged."
                              + location + " Close other RAH instances and retry, or restore the backup.", 503) from error
        with self.connect() as db:
            if db.execute("PRAGMA foreign_key_check").fetchone() or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise Problem("workspace_migration", f"The upgraded workspace failed its checks. Restore {backup}.", 503)
        return backup

    def upgrade_v4(self):
        """Schema 3 to 4: per-entry usage, and close work left in flight by the previous build. No downgrade path."""
        with closing(sqlite3.connect(self.database, isolation_level=None, timeout=30)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 3:
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("PRAGMA user_version").fetchone()[0] != 3:
                    db.execute("ROLLBACK")
                    return
                sql = resources.files("rah").joinpath("workspace_schema_4.sql").read_text(encoding="utf-8")
                for statement in sql.split(";"):
                    if statement.strip():
                        db.execute(statement)
                if (db.execute("PRAGMA foreign_key_check").fetchone()
                        or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                        or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
                    raise Problem("workspace_migration", "Workspace upgrade verification failed.")
                db.execute("COMMIT")
            except BaseException as error:
                db.execute("ROLLBACK")
                raise Problem("workspace_migration", "RAH could not upgrade the workspace database to schema 4 and left it "
                              "unchanged. Close other RAH instances and retry.", 503) from error

    @staticmethod
    def check_root(path):
        if not path.is_absolute() or ".." in path.parts or path.drive.startswith("\\\\"):
            raise Problem("workspace_path", "Choose an absolute, local folder path.")
        if any((item.exists() or item.is_symlink()) and is_link(item) for item in (path, *path.parents)):
            raise Problem("workspace_path", "Choose a workspace folder without filesystem redirects.")
        private_directory(path)
        verify_windows_root(path)
        return path

    @staticmethod
    def prepare_workspace(root):
        for name in ("projects", "chats", "trash"):
            verify_windows_root(private_directory(child(root, name)))

    def move_workspace(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\0" in value:
            raise Problem("workspace_path", "Enter an absolute, local workspace folder path.")
        target = Path(value).expanduser()
        source = self.workspace_root
        if target == source:
            return
        if target != self.root and (target.is_relative_to(self.root) or self.root.is_relative_to(target)):
            raise Problem("workspace_path", "Choose a folder outside the RAH installation and data root.")
        if source != self.root and (target.is_relative_to(source) or source.is_relative_to(target)):
            raise Problem("workspace_path", "Choose a folder separate from the current workspace.")
        self.check_root(target)
        names = ("projects", "chats", "trash")
        if target != self.root and any(not re.fullmatch(r"\.rah-backup-[0-9a-f]{32}", item.name) for item in target.iterdir()):
            raise Problem("workspace_path", "Choose an empty workspace folder or a previous RAH workspace location.")
        destinations = [child(target, name) for name in names]
        if any(path.exists() and (not path.is_dir() or any(path.iterdir())) for path in destinations):
            raise Problem("workspace_path", "The destination already has workspace files. Choose an empty folder.")
        stage = private_directory(child(target, ".rah-move-" + uuid4().hex))

        def copy_file(src, dest):
            if is_link(Path(src)) or not stat.S_ISREG(Path(src).lstat().st_mode):
                raise Problem("workspace_path", "Remove linked or special files from the workspace before moving it.")
            shutil.copy2(src, dest)

        def reject_links(folder, entries):
            if any(is_link(Path(folder) / name) for name in entries):
                raise Problem("workspace_path", "Remove linked files from the workspace before moving it.")
            return []

        placed = []
        try:
            for name in names:
                shutil.copytree(child(source, name), child(stage, name), ignore=reject_links, copy_function=copy_file)
            for name, path in zip(names, destinations):
                if path.exists():
                    path.rmdir()
                child(stage, name).rename(path)
                placed.append(path)
            with self.connect(write=True) as db:
                db.execute("INSERT OR REPLACE INTO workspace_setting VALUES (1, ?)", (str(target),))
        except BaseException:
            for path in placed:
                shutil.rmtree(path, ignore_errors=True)
            if target == self.root:
                self.prepare_workspace(target)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        self.workspace_root = target
        # The new location is authoritative. Archive the old copy, never delete it.
        backup = None
        try:
            backup = private_directory(child(source, ".rah-backup-" + uuid4().hex))
            for name in names:
                child(source, name).rename(child(backup, name))
        except OSError:
            return {"backup": str(backup or source), "previous": str(source), "backup_split": True}
        return {"backup": str(backup)}

    def list_chats(self):
        with self.connect() as db:
            return [self.chat_data(row) for row in db.execute(
                "SELECT * FROM chat WHERE trash_state IS NULL ORDER BY updated_at DESC, rowid DESC")]

    @staticmethod
    def chat_data(row):
        return {key: row[key] for key in ("id", "project_id", "title", "app", "created_at", "updated_at")} | {
            "scope_id": row["project_id"] or row["id"], "legacy_project_id": row["legacy_project_id"]}

    def chat_directory(self, row):
        return (child(self.directory(row["project_id"]), "chats", row["id"]) if row["project_id"]
                else child(self.workspace_root, "chats", row["id"]))

    def prepare_chat(self, row):
        directory = self.chat_directory(row)
        private_directory(directory)
        private_directory(child(directory, "files"))
        return directory

    def insert_chat(self, db, project, title, app):
        chat, date = str(uuid4()), now()
        if project is None:
            db.execute("INSERT INTO scope VALUES (?)", (chat,))
        db.execute("INSERT INTO chat (id,project_id,title,app,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                   (chat, project, title, app, date, date))
        row = db.execute("SELECT * FROM chat WHERE id=?", (chat,)).fetchone()
        directory = self.chat_directory(row)
        try:
            if project:
                private_directory(directory.parent)
            self.prepare_chat(row)
        except (OSError, Problem):
            if directory.is_dir() and not is_link(directory):
                shutil.rmtree(directory, ignore_errors=True)
            raise
        return row

    def create_chat(self, project, title="New chat", app="gptr"):
        if not isinstance(title, str) or not title.strip() or len(title) > 160 or app not in ("ldr", "gptr", "pqa"):
            raise Problem("chat_title", "Choose an app and a chat title of 1 to 160 characters.")
        with self.connect(write=True) as db:
            if project is not None:
                self.require(db, project)
            return self.chat_data(self.insert_chat(db, project, title.strip(), app))

    def require_chat(self, db, project, chat):
        row = db.execute("""SELECT * FROM chat WHERE id=? AND trash_state IS NULL
                         AND (COALESCE(project_id,id)=? OR legacy_project_id=?)""",
                         (id_value(chat), project, project)).fetchone()
        if row is None:
            raise Problem("chat_missing", "Chat not found in this project.", 404)
        return dict(row)

    def workspace_entry(self, db, project, entry):
        row = db.execute("""SELECT e.*, COALESCE(c.project_id,c.id) AS project_id FROM chat_entry e JOIN chat c ON c.id=e.chat_id
                            WHERE e.id=? AND c.trash_state IS NULL AND (COALESCE(c.project_id,c.id)=? OR c.legacy_project_id=?)""",
                         (id_value(entry), project, project)).fetchone()
        if not row:
            raise Problem("entry_missing", "Request not found in this project.", 404)
        return self.entry_data(row)

    @staticmethod
    def entry_data(row):
        result = dict(row)
        result.pop("intent")
        for key in ("input_ids", "result_ids"):
            result[key] = json.loads(result[key])
        raw = result.pop("usage_json", None)
        result["usage"] = json.loads(raw) if raw else None
        stamp = datetime.now(timezone.utc)
        result["server_time"] = stamp.isoformat()
        result["elapsed_seconds"] = None
        if result.get("state") in ("Running", "Surveying"):
            try:
                result["elapsed_seconds"] = max(0, int((stamp - datetime.fromisoformat(result["updated_at"])).total_seconds()))
            except (TypeError, ValueError):
                pass
        return result

    def record_usage(self, entry, summary):
        """Store one entry's usage summary (chat_entry.usage_json). Never derived from shared-key totals."""
        from .usage import encode
        if not entry:
            return
        with self.connect(write=True) as db:
            db.execute("UPDATE chat_entry SET usage_json=? WHERE id=?", (encode(summary), id_value(entry)))

    def entry(self, project, entry):
        with self.connect() as db:
            return self.workspace_entry(db, project, entry)

    def get_chat(self, project, chat):
        with self.connect() as db:
            data = self.chat_data(self.require_chat(db, project, chat))
            data["entries"] = [self.entry_data(row) for row in db.execute(
                "SELECT * FROM chat_entry WHERE chat_id=? ORDER BY rowid", (chat,))]
            return data

    def update_chat(self, project, chat, *, title=None, app=None):
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 160):
            raise Problem("chat_title", "Enter a chat title of 1 to 160 characters.")
        if app is not None and app not in ("ldr", "gptr", "pqa"):
            raise Problem("request_app", "Select an available application.")
        with self.connect(write=True) as db:
            self.require_chat(db, project, chat)
            if title is not None:
                db.execute("UPDATE chat SET title=? WHERE id=?", (title.strip(), chat))
            if app is not None:
                db.execute("UPDATE chat SET app=? WHERE id=?", (app, chat))

    def replay(self, db, entry, intent):
        previous = db.execute("SELECT * FROM chat_entry WHERE id=?", (entry,)).fetchone()
        if previous and previous["intent"] != intent:
            raise Problem("request_replay", "This request ID belongs to a different request.", 409)
        return self.entry_data(previous) if previous else None

    def accept(self, project, chat, entry, app, action, mode, question, quote, title, pdfs, ready):
        project = id_value(project) if project is not None else None
        chat = id_value(chat) if chat is not None else None
        entry = id_value(entry)
        intent = json.dumps([project, chat, app, action, mode, question, quote, pdfs], ensure_ascii=False)
        with self.connect(write=True) as db:
            if previous := self.replay(db, entry, intent):
                scope = project or previous["chat_id"]
                return self.workspace_entry(db, scope, entry) | {"replay": True}
            if chat:
                self.require_chat(db, project or chat, chat)
                if action == "managed" and (db.execute(
                    """SELECT 1 FROM chat_entry WHERE chat_id=? AND action='managed'
                       AND state IN ('Preparing','Awaiting approval','Running')""", (chat,)
                ).fetchone() or self.active_run(db, chat)):
                    raise Problem("chat_busy", "Finish or discard the active request in this chat.", 409)
            ready()
            date = now()
            if not chat and project is not None:
                self.require(db, project)
            if pdfs:
                if not (project or chat):
                    raise Problem("pdf_selection", "Create a chat before selecting saved PDFs.")
                self.select_pdf_rows(db, project or chat, pdfs)
            if not chat:
                chat = self.insert_chat(db, project, title[:160], app)["id"]
            project = project or chat
            self.require_scope(db, project)
            db.execute("""INSERT INTO chat_entry
                (id,chat_id,app,action,mode,question,quote,intent,state,input_ids,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'Preparing',?,?,?)""",
                (entry, chat, app, action, mode, question, quote, intent, json.dumps(pdfs), date, date))
            db.execute("UPDATE chat SET app=?, updated_at=? WHERE id=?", (app, date, chat))
            return self.workspace_entry(db, project, entry) | {"replay": False}

    def transition(self, project, entry, allowed, state, **values):
        with self.connect(write=True) as db:
            row = self.workspace_entry(db, project, entry)
            if row["state"] not in allowed:
                raise Problem("request_state", "This request has already advanced. Reopen it to see its status.", 409)
            assignments = ["state=?", "updated_at=?"] + [f"{key}=?" for key in values]
            columns = [json.dumps(value) if isinstance(value, list) else value for value in values.values()]
            db.execute(f"UPDATE chat_entry SET {','.join(assignments)} WHERE id=?", (state, now(), *columns, entry))
            return self.workspace_entry(db, project, entry)

    def interrupt(self):
        with self.connect(write=True) as db:
            db.execute("""UPDATE chat_entry SET state='Interrupted', reason='The hub restarted before this request finished.',
                        updated_at=? WHERE (state='Running' OR (state='Preparing' AND action='native'))
                        AND id NOT IN (SELECT entry_id FROM pipeline_step)""", (now(),))

    def set_preview(self, project, entry, preview_id):
        with self.connect(write=True) as db:
            self.workspace_entry(db, project, entry)
            db.execute("UPDATE chat_entry SET preview_id=?, updated_at=? WHERE id=?", (preview_id, now(), entry))

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.database)
        db.execute("PRAGMA foreign_keys = ON")
        db.row_factory = sqlite3.Row
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def require(self, db, project, revision=None):
        row = db.execute("SELECT * FROM project WHERE id=?", (id_value(project),)).fetchone()
        if not row:
            raise Problem("project_missing", "Project not found.", 404)
        if revision is not None and (type(revision) is not int or row["revision"] != revision):
            raise Problem("stale_project", "Project changed. Reload and try again.", 409)
        return dict(row)

    def require_scope(self, db, scope):
        scope = id_value(scope)
        project = db.execute("SELECT * FROM project WHERE id=?", (scope,)).fetchone()
        if project:
            return dict(project)
        row = db.execute("SELECT * FROM chat WHERE id=? AND project_id IS NULL AND trash_state IS NULL", (scope,)).fetchone()
        if not row:
            raise Problem("workspace_missing", "Project or standalone chat not found.", 404)
        return self.chat_data(row) | {"name": row["title"], "revision": 0}

    def folder(self, scope, chat=None):
        with self.connect() as db:
            self.require_scope(db, scope)
            if chat or not db.execute("SELECT 1 FROM project WHERE id=?", (scope,)).fetchone():
                return self.chat_directory(self.require_chat(db, scope, chat or scope))
            return self.directory(scope)

    def native_outputs(self, scope, chat=None):
        folder = self.folder(scope, chat)
        output = child(folder, "gptr", "outputs")
        if not output.exists():
            return []
        return [{"name": item.name, "size": item.stat().st_size} for item in sorted(output.iterdir())[:500]
                if not is_link(item) and item.is_file() and item.suffix.lower() in {".md", ".pdf", ".docx", ".txt", ".json"}]

    def native_output(self, scope, chat, filename):
        folder = self.folder(scope, chat)
        if not filename or filename not in {item["name"] for item in self.native_outputs(scope, chat)}:
            raise Problem("artifact_missing", "Native output not found.", 404)
        return child(folder, "gptr", "outputs", filename)

    def trash_chat(self, chat):
        chat = id_value(chat)
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM chat WHERE id=? AND trash_state IS NULL", (chat,)).fetchone()
            if not row:
                raise Problem("chat_missing", "Chat not found.", 404)
            if db.execute("SELECT 1 FROM chat_entry WHERE chat_id=? AND state IN ('Preparing','Awaiting approval','Running')", (chat,)).fetchone():
                raise Problem("chat_busy", "Finish or discard active work before deleting this chat.", 409)
            if self.active_run(db, chat):
                raise Problem("chat_busy", "Cancel or finish the active guided research before deleting this chat.", 409)
            if db.execute("""SELECT 1 FROM paper p JOIN artifact a ON a.project_id=p.project_id
                             AND a.id IN (p.source_artifact_id,p.pdf_artifact_id) WHERE a.chat_id=?""", (chat,)).fetchone():
                raise Problem("chat_referenced", "Remove or replace Papers Needed references to this chat's files before deleting it.", 409)
            if db.execute("""SELECT 1 FROM chat_entry e JOIN chat c ON c.id=e.chat_id JOIN json_each(e.input_ids) j
                              JOIN artifact a ON a.id=j.value AND a.project_id IN (COALESCE(c.project_id,c.id),c.legacy_project_id)
                              WHERE e.chat_id<>? AND a.chat_id=? AND c.trash_state IS NULL""", (chat, chat)).fetchone():
                raise Problem("chat_referenced", "Another chat references one of these files. Keep this chat or delete the dependent chat first.", 409)
            db.execute("UPDATE chat SET trash_state='trashing',trashed_at=? WHERE id=?", (now(), chat))
        self.reconcile_trash(chat)

    def trash_list(self):
        with self.connect() as db:
            return [self.chat_data(row) | {"trashed_at": row["trashed_at"]} for row in db.execute(
                "SELECT * FROM chat WHERE trash_state='trashed' ORDER BY trashed_at DESC")]

    def has_active(self):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM chat_entry WHERE state IN ('Preparing','Awaiting approval','Running')").fetchone()
                        or self.active_run(db))

    @staticmethod
    def active_run(db, chat=None):
        return db.execute("""SELECT id FROM pipeline_run WHERE state NOT IN ('succeeded','failed','cancelled')
                             AND chat_id=COALESCE(?, chat_id) LIMIT 1""", (chat,)).fetchone()

    def restore_chat(self, chat):
        with self.connect(write=True) as db:
            if not db.execute("UPDATE chat SET trash_state='restoring' WHERE id=? AND trash_state='trashed'", (id_value(chat),)).rowcount:
                raise Problem("chat_missing", "Chat not found in Trash.", 404)
        self.reconcile_trash(chat)

    def reconcile_trash(self, chat=None):
        with self.connect(write=True) as db:
            for row in db.execute("SELECT * FROM chat WHERE trash_state IN ('trashing','restoring') AND id=COALESCE(?,id)", (chat,)):
                live, trash = self.chat_directory(row), child(self.workspace_root, "trash", row["id"])
                restoring = row["trash_state"] == "restoring"
                source, dest = (trash, live) if restoring else (live, trash)
                if source.exists() == dest.exists():
                    raise Problem("trash_conflict", "RAH could not locate a unique chat folder. Check the workspace and Trash before retrying.", 409)
                if source.exists():
                    source.rename(dest)
                db.execute("UPDATE chat SET trash_state=?,trashed_at=? WHERE id=?",
                           (None if restoring else "trashed", None if restoring else row["trashed_at"], row["id"]))

    def change(self, db, project):
        db.execute("UPDATE project SET revision=revision+1, updated_at=? WHERE id=?", (now(), project))

    def create(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise Problem("project_name", "Enter a project name of 1 to 160 characters.")
        project = str(uuid4())
        directory = private_directory(self.directory(project))
        private_directory(child(directory, "files"))
        date = now()
        with self.connect(write=True) as db:
            db.execute("INSERT INTO scope VALUES (?)", (project,))
            db.execute("INSERT INTO project (id,name,revision,created_at,updated_at) VALUES (?, ?, 0, ?, ?)", (project, name.strip(), date, date))
        return project

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM project WHERE legacy_hidden=0 ORDER BY updated_at DESC")]

    def get(self, project, chat=None):
        with self.connect() as db:
            info = self.require_scope(db, project)
            owner = self.require_chat(db, project, chat) if chat else None
            info["paper"] = [dict(row) for row in db.execute("SELECT * FROM paper WHERE project_id=? ORDER BY rowid", (project,))]
            legacy = owner["legacy_project_id"] if owner else (db.execute("SELECT legacy_project_id FROM chat WHERE id=?", (project,)).fetchone() or [None])[0]
            info["artifact"] = [dict(row) for row in db.execute("""SELECT a.* FROM artifact a LEFT JOIN chat c ON c.id=a.chat_id
                WHERE c.trash_state IS NULL AND ((a.project_id=? AND (? IS NULL OR a.chat_id=?))
                    OR (a.project_id=? AND (a.chat_id=? OR (? IS NULL AND a.chat_id IS NULL)))) ORDER BY a.rowid""",
                    (project, chat, chat, legacy, chat or project, chat))]
            return info

    def directory(self, project):
        return child(self.workspace_root, "projects", id_value(project))

    def artifact(self, project, artifact):
        with self.connect() as db:
            self.require_scope(db, project)
            row = db.execute("""SELECT a.* FROM artifact a LEFT JOIN chat c ON c.id=a.chat_id
                 WHERE a.id=? AND c.trash_state IS NULL AND (a.project_id=? OR (c.id=? AND c.legacy_project_id=a.project_id)
                 OR (a.chat_id IS NULL AND a.project_id=(SELECT legacy_project_id FROM chat WHERE id=?)))""",
                             (id_value(artifact), project, project, project)).fetchone()
        if not row:
            raise Problem("artifact_missing", "Artifact not found.", 404)
        return dict(row)

    def artifact_path(self, row):
        return child(self.files_directory(row["project_id"], row.get("chat_id")), row["storage_name"])

    def files_directory(self, project, chat=None):
        if chat:
            with self.connect() as db:
                return child(self.chat_directory(self.require_chat(db, project, chat)), "files")
        return child(self.folder(project), "files")

    def register(self, project, stream, filename, kind, revision=None, app=None, version=None, run_id=None, bib=None, *, entry=None,
                 attachment_id=None, chat=None, role=None, origin="private_or_unknown", output_key=None):
        if not isinstance(filename, str) or not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
            raise Problem("file_name", "The file name is invalid.")
        suffix = PurePath(filename).suffix.lower()
        if suffix not in KINDS.get(kind, {}) or kind == "questionnaire":
            raise Problem("file_type", "Select a PDF, Markdown, plain-text, JSON or RIS file as appropriate.")
        if (role is not None and role not in ROLES) or origin not in ORIGINS:
            raise Problem("artifact_role", "Unknown artifact role or origin.")
        output_key = output_key or ("primary" if run_id else None)
        with self.connect() as db:
            scope = self.require_scope(db, project)
            if revision is not None and revision != scope["revision"]:
                raise Problem("stale_project", "Project changed. Reload and try again.", 409)
            request = self.workspace_entry(db, project, entry) if entry else None
            chat = request["chat_id"] if request else chat or (project if scope.get("scope_id") else None)
            if chat:
                self.require_chat(db, project, chat)
        directory = self.files_directory(project, chat)
        if not directory.is_dir():
            private_directory(directory)
        artifact = id_value(attachment_id) if attachment_id else str(uuid4())
        stored_name = artifact + suffix
        destination = child(directory, stored_name)
        temp_path = None
        sidecar = child(directory, artifact + ".bib")
        published = False
        placed = False
        size = 0
        try:
            with NamedTemporaryFile(dir=directory, delete=False) as handle:
                temp_path = child(directory, PurePath(handle.name).name)
                if kind == "pdf":
                    first = stream.read(8)
                    if not first.startswith(b"%PDF-"):
                        raise Problem("file_format", "The submitted file has no PDF header.")
                    handle.write(first)
                    size += len(first)
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    if size > (PDF_LIMIT if kind == "pdf" else REPORT_LIMIT):
                        raise Problem("file_size", "The file exceeds the upload limit.", 413)
                    handle.write(block)
            if not size:
                raise Problem("empty_file", "The file is empty.")
            if kind != "pdf":
                try:
                    temp_path.read_text(encoding="utf-8")
                except UnicodeError as error:
                    raise Problem("file_format", "The report must be UTF-8 text.") from error
            with self.connect(write=True) as db:
                scope = self.require_scope(db, project)
                if revision is not None and revision != scope["revision"]:
                    raise Problem("stale_project", "Project changed. Reload and try again.", 409)
                if chat:
                    self.require_chat(db, project, chat)
                checksum = file_digest(temp_path)
                if run_id:
                    previous = db.execute("SELECT * FROM artifact WHERE project_id=? AND run_id=? AND output_key=?",
                                          (project, run_id, output_key)).fetchone()
                    if previous:
                        if previous["chat_id"] != chat:
                            raise Problem("run_owner", "The saved result belongs to a different chat.", 409)
                        if (previous["kind"] != kind or previous["sha256"] != checksum or previous["step_id"] is not None
                                or PurePath(previous["original_name"] or "").suffix.lower() != suffix):
                            raise Problem("run_replay", "This run already saved a different result.", 409)
                        return previous["id"]
                if attachment_id:
                    previous = db.execute("SELECT * FROM artifact WHERE project_id=? AND id=?", (project, artifact)).fetchone()
                    if previous:
                        if (previous["chat_id"] != chat or previous["sha256"] != checksum
                                or previous["original_name"] != filename or previous["kind"] != kind):
                            raise Problem("attachment_replay", "This attachment ID belongs to another file.", 409)
                        current = self.workspace_entry(db, project, entry)
                        if artifact not in current["input_ids"] + current["result_ids"]:
                            raise Problem("attachment_replay", "This attachment ID belongs to another request.", 409)
                        return artifact
                if request:
                    request = self.workspace_entry(db, project, entry)
                    if db.execute("SELECT 1 FROM pipeline_step WHERE entry_id=?", (entry,)).fetchone():
                        raise Problem("guided_entry", "Use the guided stage actions to add files to this step.", 409)
                    if run_id:
                        if request["run_id"] != run_id or request["state"] not in ("Running", "Interrupted"):
                            raise Problem("run_owner", "This run is no longer associated with the request.", 409)
                    elif request["action"] == "native":
                        if request["state"] != "Opened" or kind != "report":
                            raise Problem("attachment_state", "Open the native app before attaching its result.", 409)
                    elif request["app"] != "pqa" or request["state"] != "Preparing" or kind != "pdf":
                        raise Problem("attachment_state", "Only a preparing PaperQA2 request accepts PDFs.", 409)
                    if kind == "pdf" and not run_id:
                        existing_ids = request["input_ids"]
                        if len(existing_ids) >= 40:
                            raise Problem("pdf_selection", "Select one to 40 PDFs.")
                        if existing_ids:
                            previous_size = sum(row["size_bytes"] for row in self.select_pdf_rows(db, project, existing_ids))
                            if previous_size + size > 512 * 1024 * 1024:
                                raise Problem("pdf_selection", "The selected corpus is over 512 MiB.", 413)
                temp_path.replace(destination)
                placed = True
                if bib:
                    atomic(sidecar, bib.encode())
                db.execute(
                    """INSERT INTO artifact
                    (project_id,id,kind,app_id,app_version,original_name,storage_name,sha256,size_bytes,run_id,created_at,chat_id,
                     role,media_type,output_key,origin_class)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (project, artifact, kind, app, version, filename, stored_name, checksum, size, run_id, now(), chat,
                     role, MEDIA.get(suffix), output_key, origin),
                )
                if request:
                    column = "result_ids" if run_id or request["action"] == "native" else "input_ids"
                    date = now()
                    db.execute(f"UPDATE chat_entry SET {column}=?, state=?, reason=NULL, updated_at=? WHERE id=?",
                               (json.dumps(request[column] + [artifact]), "Complete" if run_id else request["state"], date, entry))
                    db.execute("UPDATE chat SET updated_at=? WHERE id=?", (date, chat))
                db.execute("UPDATE project SET updated_at=? WHERE id=?", (now(), project))
            published = True
            return artifact
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            if placed and not published:
                destination.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)

    def save_papers(self, project, revision, rows):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 250:
            raise Problem("papers_input", "Select one to 250 citation rows.")
        with self.connect(write=True) as db:
            self.require(db, project, revision)
            for row in rows:
                if not isinstance(row, dict):
                    raise Problem("papers_input", "Expected a citation row.")
                fields = []
                for name in FIELDS:
                    value = row.get(name)
                    if value is not None and (not isinstance(value, str) or len(value) > 10_000):
                        raise Problem("papers_input", "Citation fields are too large or invalid.")
                    fields.append(value.strip() or None if value is not None else None)
                if not any(fields) or (fields[3] and not valid_url(fields[3])):
                    raise Problem("papers_input", "Supply citation text and a valid URL, if one is present.")
                state = row.get("state", "needed")
                if state not in ("needed", "supplied", "skipped"):
                    raise Problem("papers_input", "Invalid paper state.")
                pdf = row.get("pdf_artifact_id") if state == "supplied" else None
                source = row.get("source_artifact_id")
                if state == "supplied":
                    if not pdf or not db.execute("""SELECT 1 FROM artifact a LEFT JOIN chat c ON c.id=a.chat_id
                            WHERE a.project_id=? AND a.id=? AND a.kind='pdf' AND c.trash_state IS NULL""", (project, id_value(pdf))).fetchone():
                        raise Problem("pdf_association", "Pick a PDF from this project.")
                if source and not db.execute("SELECT 1 FROM artifact WHERE project_id=? AND id=?", (project, id_value(source))).fetchone():
                    raise Problem("source_association", "Pick an original source from this project.")
                paper = id_value(row["id"]) if row.get("id") else str(uuid4())
                db.execute(
                    """INSERT INTO paper (project_id,id,title,doi,pmid,citation_url,note,source_artifact_id,pdf_artifact_id,
                    abstract_only,state,access_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (project_id,id) DO UPDATE SET title=excluded.title,doi=excluded.doi,pmid=excluded.pmid,
                    citation_url=excluded.citation_url,note=excluded.note,source_artifact_id=excluded.source_artifact_id,
                    pdf_artifact_id=excluded.pdf_artifact_id,abstract_only=excluded.abstract_only,state=excluded.state,
                    access_status=CASE WHEN excluded.pdf_artifact_id IS NOT NULL THEN 'local_available'
                        WHEN paper.access_status='local_available' THEN 'unresolved' ELSE paper.access_status END""",
                    (project, paper, *fields, source, pdf, int(row.get("abstract_only") is True), state,
                     "local_available" if pdf else "unresolved"),
                )
            self.change(db, project)

    def selected_pdfs(self, project, ids):
        with self.connect() as db:
            self.require_scope(db, project)
            return self.select_pdf_rows(db, project, ids)

    @staticmethod
    def select_pdf_rows(db, project, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 40:
            raise Problem("pdf_selection", "Select one to 40 PDFs.")
        ids = [id_value(value) for value in ids]
        if len(ids) != len(set(ids)):
            raise Problem("pdf_selection", "Select each PDF once.")
        rows = []
        for artifact in ids:
            row = db.execute("""SELECT a.* FROM artifact a LEFT JOIN chat c ON c.id=a.chat_id
                 WHERE a.id=? AND a.kind='pdf' AND c.trash_state IS NULL
                 AND (a.project_id=? OR (c.id=? AND c.legacy_project_id=a.project_id)
                 OR (a.chat_id IS NULL AND a.project_id=(SELECT legacy_project_id FROM chat WHERE id=?)))""",
                             (artifact, project, project, project)).fetchone()
            if row is None:
                raise Problem("pdf_selection", "A selected PDF is unavailable in this project.", 409)
            rows.append(dict(row))
        if sum(row["size_bytes"] for row in rows) > 512 * 1024 * 1024:
            raise Problem("pdf_selection", "The selected corpus is over 512 MiB.", 413)
        return rows

    # ---- Guided multi-output registry -------------------------------------------------

    def stage(self, project, chat, source, filename, kind, *, limit=None):
        """Copy verified bytes into a private temporary file beside their destination."""
        suffix = PurePath(filename).suffix.lower()
        if suffix not in KINDS.get(kind, {}):
            raise Problem("file_type", "The output has an unexpected file type.")
        directory = self.files_directory(project, chat)
        if not directory.is_dir():
            private_directory(directory)
        limit = limit or (PDF_LIMIT if kind == "pdf" else REPORT_LIMIT)
        size = 0
        hasher = hashlib.sha256()
        handle = NamedTemporaryFile(dir=directory, prefix=".rah-stage-", delete=False)
        path = Path(handle.name)
        try:
            with handle:
                stream = source.open("rb") if isinstance(source, Path) else source
                try:
                    first = True
                    while block := stream.read(1024 * 1024):
                        if first and kind == "pdf" and not block.startswith(b"%PDF-"):
                            raise Problem("file_format", "The output has no PDF header.")
                        first = False
                        size += len(block)
                        if size > limit:
                            raise Problem("file_size", "The output exceeds its size limit.", 413)
                        hasher.update(block)
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
                finally:
                    if isinstance(source, Path):
                        stream.close()
            if not size:
                raise Problem("empty_file", "The output is empty.")
            if kind != "pdf":
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeError as error:
                    raise Problem("file_format", "The output must be UTF-8 text.") from error
                if kind == "questionnaire":
                    json.loads(text)
            return {"temp": path, "sha256": hasher.hexdigest(), "size": size, "suffix": suffix,
                    "filename": filename, "kind": kind}
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def publish(self, project, chat, outputs, *, step=None, run_id=None, entry=None, check=None, finalize=None, marker=None):
        """Place staged outputs and register them, their parents and the entry results atomically.

        Exact replays return the original IDs. Differences in owner, step, run, key, kind, name or bytes are rejected.
        """
        placed = []
        ids = []
        try:
            with self.writer:
                for item in outputs:
                    key = item["key"]
                    if not OUTPUT_KEY.fullmatch(key) or item.get("role") not in ROLES | {None} or item.get("origin", "private_or_unknown") not in ORIGINS:
                        raise Problem("artifact_output", "Invalid output identity.", 409)
                    item["id"] = output_id(step, key) if step else item.get("id") or str(uuid4())
                    item["storage"] = item["id"] + item["stage"]["suffix"]
                if marker is not None:
                    write_json(marker, {"outputs": [{"id": item["id"], "storage": item["storage"], "sha256": item["stage"]["sha256"]}
                                                    for item in outputs], "committed": False})
                with self.connect(write=True) as db:
                    self.require_scope(db, project)
                    self.require_chat(db, project, chat)
                    if check:
                        check(db)
                    directory = self.files_directory(project, chat)
                    for item in outputs:
                        staged = item["stage"]
                        previous = db.execute("SELECT * FROM artifact WHERE project_id=? AND id=?", (project, item["id"])).fetchone()
                        if previous:
                            if (previous["chat_id"] != chat or previous["step_id"] != step or previous["run_id"] != run_id
                                    or previous["output_key"] != item["key"] or previous["kind"] != staged["kind"]
                                    or previous["original_name"] != staged["filename"] or previous["sha256"] != staged["sha256"]):
                                raise Problem("output_replay", "This output identity belongs to different bytes or owner.", 409)
                            ids.append(item["id"])
                            continue
                        if step and db.execute("SELECT 1 FROM artifact WHERE project_id=? AND step_id=? AND output_key=?",
                                               (project, step, item["key"])).fetchone():
                            raise Problem("output_replay", "This step already saved a different output for this key.", 409)
                        destination = child(directory, item["storage"])
                        if destination.exists():
                            if file_digest(destination) != staged["sha256"]:
                                raise Problem("output_conflict", "An unregistered file with different bytes occupies the destination.", 409)
                            staged["temp"].unlink(missing_ok=True)
                        else:
                            staged["temp"].replace(destination)
                        placed.append(destination)
                        db.execute("""INSERT INTO artifact (project_id,id,kind,app_id,app_version,original_name,storage_name,sha256,
                            size_bytes,run_id,created_at,chat_id,step_id,role,media_type,output_key,title,origin_class)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (project, item["id"], staged["kind"], item.get("app"), item.get("version"), staged["filename"],
                             item["storage"], staged["sha256"], staged["size"], run_id, now(), chat, step, item.get("role"),
                             MEDIA.get(staged["suffix"]), item["key"], (item.get("title") or None) and item["title"][:300],
                             item.get("origin", "private_or_unknown")))
                        for parent in item.get("parents", ()):
                            self.add_parent(db, project, parent, item["id"])
                        ids.append(item["id"])
                    if entry:
                        row = db.execute("SELECT result_ids FROM chat_entry WHERE id=? AND chat_id=?", (entry, chat)).fetchone()
                        if not row:
                            raise Problem("entry_missing", "Request not found in this chat.", 404)
                        results = json.loads(row["result_ids"])
                        results += [value for value in ids if value not in results]
                        db.execute("UPDATE chat_entry SET result_ids=?, updated_at=? WHERE id=?", (json.dumps(results), now(), entry))
                    if finalize:
                        finalize(db, ids)
                    db.execute("UPDATE chat SET updated_at=? WHERE id=?", (now(), chat))
                placed = []
                if marker is not None:
                    data = read_json(marker, {}) or {}
                    write_json(marker, data | {"committed": True})
                return ids
        finally:
            for item in outputs:
                staged = item.get("stage") or {}
                if staged.get("temp"):
                    staged["temp"].unlink(missing_ok=True)
            for path in placed:
                path.unlink(missing_ok=True)

    def recover_publication(self, project, chat, marker):
        """Remove only files that this attempt placed but never registered."""
        data = read_json(marker, None)
        if not data or data.get("committed"):
            return
        directory = self.files_directory(project, chat)
        with self.writer, self.connect() as db:
            for item in data.get("outputs", []):
                path = child(directory, item["storage"])
                registered = db.execute("SELECT 1 FROM artifact WHERE project_id=? AND id=?", (project, item["id"])).fetchone()
                if not registered and path.is_file() and file_digest(path) == item["sha256"]:
                    path.unlink()

    @staticmethod
    def add_parent(db, project, parent, child_id):
        parent = id_value(parent)
        if parent == child_id:
            raise Problem("artifact_lineage", "An artifact cannot be its own parent.", 409)
        if not db.execute("SELECT 1 FROM artifact WHERE project_id=? AND id=?", (project, parent)).fetchone():
            raise Problem("artifact_lineage", "A parent artifact belongs to another scope.", 409)
        cycle = db.execute("""WITH RECURSIVE up(id) AS (SELECT ? UNION SELECT p.parent_id FROM artifact_parent p
                              JOIN up ON p.child_id=up.id WHERE p.project_id=?) SELECT 1 FROM up WHERE id=?""",
                           (parent, project, child_id)).fetchone()
        if cycle:
            raise Problem("artifact_lineage", "Artifact lineage cannot contain a cycle.", 409)
        db.execute("INSERT OR IGNORE INTO artifact_parent VALUES (?,?,?)", (project, parent, child_id))

    def artifacts_for(self, project, chat=None, *, kinds=None, include_project=False):
        """Picker listing: this chat, or this named project when requested. Trashed chats are hidden."""
        with self.connect() as db:
            scope = self.require_scope(db, project)
            if chat:
                self.require_chat(db, project, chat)
            named = bool(db.execute("SELECT 1 FROM project WHERE id=?", (project,)).fetchone())
            rows = db.execute("""SELECT a.* FROM artifact a LEFT JOIN chat c ON c.id=a.chat_id WHERE a.project_id=?
                AND (a.chat_id IS NULL OR c.trash_state IS NULL) ORDER BY a.rowid""", (project,)).fetchall()
            result = []
            for row in rows:
                if kinds and row["kind"] not in kinds:
                    continue
                if not (include_project and named) and row["chat_id"] != (chat or scope.get("scope_id")):
                    continue
                result.append(dict(row) | {"producer": {"ldr": "Local Deep Research", "gptr": "GPT Researcher",
                                                        "pqa": "PaperQA2"}.get(row["app_id"], "RAH" if row["step_id"] else "Imported")})
            return result

