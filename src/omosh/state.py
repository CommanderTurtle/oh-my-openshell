# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time


def private_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".omosh-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data.encode() if isinstance(data, str) else data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class State:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.db = self.root / "omosh.sqlite3"
        with self.connect() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id));
              CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, label TEXT, scope TEXT, created REAL);
            """)
        os.chmod(self.db, 0o600)
        key = self.root / "admin.key"
        if not key.exists():
            private_write(key, secrets.token_urlsafe(40))
        self.admin_key = key.read_text().strip()
        # Running processes cannot survive a dashboard restart as tracked workers.
        for job in self.list("jobs"):
            if job["status"] in ("queued", "running"):
                job.update(status="interrupted", ended=time.time(), error="Dashboard restarted; inspect the named tmux session before retrying.")
                self.put("jobs", job["id"], job)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, kind, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, kind, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)", (kind, key, json.dumps(value)))

    def delete(self, kind, key):
        with self.connect() as db:
            db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))

    def list(self, kind, limit=None):
        with self.connect() as db:
            rows = (db.execute("SELECT value FROM records WHERE kind=? ORDER BY rowid DESC LIMIT ?", (kind, limit)).fetchall()[::-1]
                    if limit else db.execute("SELECT value FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall())
        return [json.loads(row[0]) for row in rows]

    def mint_token(self, label: str, sessions: list[str]):
        token = secrets.token_urlsafe(40)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            db.execute("INSERT INTO tokens VALUES (?,?,?,?)", (digest, label, json.dumps(sessions), time.time()))
        return token

    def token_scope(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT scope FROM tokens WHERE hash=?", (digest,)).fetchone()
        return json.loads(row[0]) if row else None

    def tokens(self):
        with self.connect() as db:
            rows = db.execute("SELECT hash,label,scope,created FROM tokens").fetchall()
        return [dict(id=r[0], label=r[1], sessions=json.loads(r[2]), created=r[3]) for r in rows]

    def revoke(self, digest):
        with self.connect() as db:
            db.execute("DELETE FROM tokens WHERE hash=?", (digest,))
