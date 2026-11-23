import json
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import PurePath
import re
import sqlite3
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

from .common import Problem, atomic, child, file_digest, id_value, private_directory


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


def structured_citations(path):
    """Read citation fields emitted by the pinned LDR RIS exporter."""
    rows = []
    row = None
    fields = {"TI": "title", "DO": "doi", "UR": "citation_url", "N1": "note", "AB": "note"}
    try:
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
                    if not row or len(rows) >= 250:
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
                    rows.append(row)
                    row = None
                elif tag in fields and value:
                    field = fields[tag]
                    row[field] = row.get(field, "") + (" " if field in row else "") + value
        if row is not None or not rows:
            raise ValueError
        return rows
    except (OSError, ValueError) as error:
        raise Problem("unrecognized_export", "Use an LDR RIS export with up to 250 citations, or paste citations instead.") from error


UNFILED = "00000000-0000-4000-8000-000000000001"


class Projects:
    def __init__(self, root):
        self.root = root
        self.database = child(root, "hub", "state.sqlite3")
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

    def list_chats(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM chat ORDER BY updated_at DESC, rowid DESC")]

    def require_chat(self, db, project, chat):
        row = db.execute("SELECT * FROM chat WHERE id=? AND project_id=?", (id_value(chat), project)).fetchone()
        if row is None:
            raise Problem("chat_missing", "Chat not found in this project.", 404)
        return dict(row)

    def workspace_entry(self, db, project, entry):
        row = db.execute("""SELECT e.*, c.project_id FROM chat_entry e JOIN chat c ON c.id=e.chat_id
                            WHERE e.id=? AND c.project_id=?""", (id_value(entry), project)).fetchone()
        if not row:
            raise Problem("entry_missing", "Request not found in this project.", 404)
        return self.entry_data(row)

    @staticmethod
    def entry_data(row):
        result = dict(row)
        result.pop("intent")
        for key in ("input_ids", "result_ids"):
            result[key] = json.loads(result[key])
        return result

    def entry(self, project, entry):
        with self.connect() as db:
            return self.workspace_entry(db, project, entry)

    def get_chat(self, project, chat):
        with self.connect() as db:
            data = self.require_chat(db, project, chat)
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
        project = id_value(project) if project is not None else UNFILED
        chat = id_value(chat) if chat is not None else None
        entry = id_value(entry)
        intent = json.dumps([project, chat, app, action, mode, question, quote, pdfs], ensure_ascii=False)
        with self.connect(write=True) as db:
            if previous := self.replay(db, entry, intent):
                return previous | {"project_id": project, "replay": True}
            if chat:
                self.require_chat(db, project, chat)
                if action == "managed" and db.execute(
                    """SELECT 1 FROM chat_entry WHERE chat_id=? AND action='managed'
                       AND state IN ('Preparing','Awaiting approval','Running')""", (chat,)
                ).fetchone():
                    raise Problem("chat_busy", "Finish or discard the active request in this chat.", 409)
            ready()
            date = now()
            if project == UNFILED:
                db.execute("INSERT OR IGNORE INTO project VALUES (?, 'Unfiled', 0, ?, ?)", (project, date, date))
                private_directory(child(self.directory(project), "files"))
            self.require(db, project)
            if pdfs:
                self.select_pdf_rows(db, project, pdfs)
            if not chat:
                chat = str(uuid4())
                db.execute("INSERT INTO chat VALUES (?, ?, ?, ?, ?, ?)", (chat, project, title[:160], app, date, date))
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
                        updated_at=? WHERE state='Running' OR (state='Preparing' AND action='native')""", (now(),))

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

    def change(self, db, project):
        db.execute("UPDATE project SET revision=revision+1, updated_at=? WHERE id=?", (now(), project))

    def create(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise Problem("project_name", "Enter a project name of 1 to 160 characters.")
        project = str(uuid4())
        directory = private_directory(child(self.root, "projects", project))
        private_directory(child(directory, "files"))
        date = now()
        with self.connect(write=True) as db:
            db.execute("INSERT INTO project VALUES (?, ?, 0, ?, ?)", (project, name.strip(), date, date))
        return project

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM project ORDER BY updated_at DESC")]

    def get(self, project):
        with self.connect() as db:
            info = self.require(db, project)
            for table in ("paper", "artifact"):
                info[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE project_id=? ORDER BY rowid", (project,))]
            return info

    def directory(self, project):
        return child(self.root, "projects", id_value(project))

    def artifact(self, project, artifact):
        with self.connect() as db:
            self.require(db, project)
            row = db.execute("SELECT * FROM artifact WHERE project_id=? AND id=?", (project, id_value(artifact))).fetchone()
        if not row:
            raise Problem("artifact_missing", "Artifact not found.", 404)
        return dict(row)

    def artifact_path(self, row):
        return child(self.files_directory(row["project_id"], row.get("chat_id")), row["storage_name"])

    def files_directory(self, project, chat=None):
        directory = self.directory(project)
        return child(directory, "chats", id_value(chat), "files") if chat else child(directory, "files")

    def register(self, project, stream, filename, kind, revision=None, app=None, version=None, run_id=None, bib=None, *, entry=None, attachment_id=None):
        if not isinstance(filename, str) or not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
            raise Problem("file_name", "The file name is invalid.")
        suffix = PurePath(filename).suffix.lower()
        valid = {"pdf": {".pdf"}, "report": {".md", ".txt", ".json", ".ris"}, "answer": {".md"}}
        if suffix not in valid.get(kind, {}):
            raise Problem("file_type", "Select a PDF, Markdown, plain-text, JSON or RIS file as appropriate.")
        with self.connect() as db:
            self.require(db, project, revision)
            request = self.workspace_entry(db, project, entry) if entry else None
        chat = request["chat_id"] if request else None
        directory = private_directory(self.files_directory(project, chat))
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
                self.require(db, project, revision)
                checksum = file_digest(temp_path)
                if run_id:
                    previous = db.execute("SELECT id, chat_id FROM artifact WHERE project_id=? AND run_id=?", (project, run_id)).fetchone()
                    if previous:
                        if previous["chat_id"] != chat:
                            raise Problem("run_owner", "The saved result belongs to a different chat.", 409)
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
                    (project_id,id,kind,app_id,app_version,original_name,storage_name,sha256,size_bytes,run_id,created_at,chat_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (project, artifact, kind, app, version, filename, stored_name, checksum, size, run_id, now(), chat),
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
                    if not pdf or not db.execute("SELECT 1 FROM artifact WHERE project_id=? AND id=? AND kind='pdf'", (project, id_value(pdf))).fetchone():
                        raise Problem("pdf_association", "Pick a PDF from this project.")
                if source and not db.execute("SELECT 1 FROM artifact WHERE project_id=? AND id=?", (project, id_value(source))).fetchone():
                    raise Problem("source_association", "Pick an original source from this project.")
                paper = id_value(row["id"]) if row.get("id") else str(uuid4())
                db.execute(
                    """INSERT INTO paper VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (project_id,id) DO UPDATE SET title=excluded.title,doi=excluded.doi,pmid=excluded.pmid,
                    citation_url=excluded.citation_url,note=excluded.note,source_artifact_id=excluded.source_artifact_id,
                    pdf_artifact_id=excluded.pdf_artifact_id,abstract_only=excluded.abstract_only,state=excluded.state""",
                    (project, paper, *fields, source, pdf, int(row.get("abstract_only") is True), state),
                )
            self.change(db, project)

    def selected_pdfs(self, project, ids):
        with self.connect() as db:
            self.require(db, project)
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
            row = db.execute("SELECT * FROM artifact WHERE project_id=? AND id=? AND kind='pdf'",
                             (project, artifact)).fetchone()
            if row is None:
                raise Problem("pdf_selection", "A selected PDF is unavailable in this project.", 409)
            rows.append(dict(row))
        if sum(row["size_bytes"] for row in rows) > 512 * 1024 * 1024:
            raise Problem("pdf_selection", "The selected corpus is over 512 MiB.", 413)
        return rows
