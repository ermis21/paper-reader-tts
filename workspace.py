#!/usr/bin/env python3
"""Workspace store: a folder tree over unified documents, backed by SQLite.

Why SQLite and not papers.json
------------------------------
`papers.json` is a flat list. A workspace needs a tree, stable ids, per-item
provenance and cheap moves -- all awkward in a hand-edited flat array. But the
four CLI scripts (`fetch.py`, `extract.py`, `synth.py`, `build.py`) read
`papers.json`, so the manifest is kept as a **generated view** of the database:
every mutation rewrites it atomically. The CLI is therefore untouched and keeps
working exactly as before. Hand-editing `papers.json` still works *additively* --
ids found there but missing from the DB are imported on the next start.

The tree is VIRTUAL
-------------------
Folder names never touch the filesystem. Artefacts stay exactly where they were:

    pdfs/<id>.pdf   text/<id>.txt   cache/<id>/NNNNN.wav   out/<id>.wav

and the database only records where each item *appears*. Two consequences:

  1. Migration is lossless and reversible -- not one byte is moved. 8.39 hours
     of rendered audio and 1,371 cached chunks survive untouched.
  2. A folder name can never become a path-traversal vector, because it is
     never used to build a path. Every file endpoint still resolves a flat
     `^[A-Za-z0-9._-]+$` id inside a fixed base directory.

One item, not two
-----------------
A row in `items` IS the document. The PDF, the extracted text, the per-chunk
cache and the rendered WAV are *properties* of that row, derived from its `id`.
There is no separate "wav entry" to get out of sync with a "pdf entry".
"""
import argparse
import contextlib
import json
import pathlib
import re
import sqlite3
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent
DB = ROOT / "workspace.sqlite"
MANIFEST = ROOT / "papers.json"
PDFS, DROPIN, TEXT, CACHE, OUT, FILES = (ROOT / "pdfs", ROOT / "dropin", ROOT / "text",
                                         ROOT / "cache", ROOT / "out", ROOT / "files")
ROOT_ID = 0                 # virtual root; no row exists for it
SCHEMA_VERSION = 1
ALLOC = threading.Lock()    # serialises id allocation across the worker + request threads

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS folders(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id INTEGER NOT NULL DEFAULT 0,   -- 0 = workspace root
  name      TEXT    NOT NULL,
  created   REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS folders_uniq ON folders(parent_id, name);

CREATE TABLE IF NOT EXISTS items(
  id        TEXT    PRIMARY KEY,          -- slug; keys pdfs/, text/, cache/, out/
  folder_id INTEGER NOT NULL DEFAULT 0,
  kind      TEXT    NOT NULL DEFAULT 'paper',   -- 'paper' | 'attachment'
  name      TEXT    NOT NULL,             -- the PDF's own filename, e.g. "Klees18.pdf"
  title     TEXT    NOT NULL,             -- display name (PDF name for uploads)
  authors   TEXT    NOT NULL DEFAULT '',
  year      TEXT    NOT NULL DEFAULT '',
  venue     TEXT    NOT NULL DEFAULT '',
  url       TEXT,
  access    TEXT    NOT NULL DEFAULT 'dropin',
  notes     TEXT    NOT NULL DEFAULT '',
  ord       INTEGER NOT NULL DEFAULT 0,   -- manifest order == M4B chapter order
  stored    TEXT,                         -- attachments: basename under files/
  bytes     INTEGER NOT NULL DEFAULT 0,   -- attachments: size on disk
  created   REAL    NOT NULL,
  deleted   INTEGER NOT NULL DEFAULT 0    -- archive flag; files are NEVER unlinked
);
CREATE INDEX IF NOT EXISTS items_folder ON items(folder_id);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind, deleted);
"""


# ---------------------------------------------------------------- connection
@contextlib.contextmanager
def conn(write=False):
    c = sqlite3.connect(DB, timeout=20.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        if write:
            c.execute("BEGIN IMMEDIATE")
        yield c
        if write:
            c.execute("COMMIT")
    except Exception:
        if write:
            with contextlib.suppress(Exception):
                c.execute("ROLLBACK")
        raise
    finally:
        c.close()


# ---------------------------------------------------------------- naming
def slug(name: str) -> str:
    """Untrusted filename -> a flat, shell-safe, filesystem-safe id.

    Matches the pre-workspace behaviour for .pdf so migrated ids are unchanged.
    """
    s = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", name or "")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()[:60]
    return s or "paper"


def clean_name(s: str, default: str = "untitled") -> str:
    """A folder or display name. Never used to build a path -- but kept tame anyway."""
    s = re.sub(r"[\x00-\x1f\x7f]", "", s or "")
    s = s.replace("/", " ").replace("\\", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(".").strip()          # kills "." and ".." outright
    return s[:80] or default


def safe_ext(name: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{1,8})$", name or "")
    return "." + m.group(1).lower() if m else ""


def id_taken(c, pid: str) -> bool:
    """An id is taken if the DB knows it OR any artefact on disk already uses it."""
    if c.execute("SELECT 1 FROM items WHERE id=?", (pid,)).fetchone():
        return True
    if (PDFS / f"{pid}.pdf").exists() or (OUT / f"{pid}.wav").exists():
        return True
    if (CACHE / pid).exists() or (TEXT / f"{pid}.txt").exists() or (DROPIN / f"{pid}.pdf").exists():
        return True
    return bool(list(FILES.glob(f"{pid}.*"))) if FILES.exists() else False


def unique_id(c, base: str) -> str:
    pid, n = base, 2
    while id_taken(c, pid):
        pid = f"{base}-{n}"
        n += 1
    return pid


# ---------------------------------------------------------------- init/migrate
def init():
    for d in (PDFS, DROPIN, TEXT, CACHE, OUT, FILES):
        d.mkdir(exist_ok=True)
    fresh = not DB.exists()
    with conn() as c:                       # executescript() commits, so no outer txn here
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")
    with conn(write=True) as c:
        c.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('schema',?)", (str(SCHEMA_VERSION),))
        empty = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"] == 0
        if empty and MANIFEST.exists():
            _import_manifest(c, backup=True)
        elif MANIFEST.exists():
            _import_manifest(c, backup=False)
    export_manifest()
    return fresh


def _import_manifest(c, backup: bool):
    """Bring ids present in papers.json but absent from the DB into the workspace."""
    try:
        doc = json.loads(MANIFEST.read_text())
    except Exception:
        return 0
    c.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('book_title',?)",
              (doc.get("title", "Library"),))
    c.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('book_author',?)",
              (doc.get("author", "Various"),))
    known = {r["id"] for r in c.execute("SELECT id FROM items")}
    added, now = 0, time.time()
    for p in doc.get("papers", []):
        pid = p.get("id")
        if not pid or pid in known:
            continue
        c.execute(
            "INSERT INTO items(id,folder_id,kind,name,title,authors,year,venue,url,access,notes,"
            "ord,created) VALUES(?,?,'paper',?,?,?,?,?,?,?,?,?,?)",
            (pid, ROOT_ID, f"{pid}.pdf", p.get("title") or pid, p.get("authors", "") or "",
             str(p.get("year", "") or ""), p.get("venue", "") or "", p.get("url"),
             p.get("access", "dropin") or "dropin", p.get("notes", "") or "",
             int(p.get("order", 0) or 0), now))
        added += 1
    if added and backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (ROOT / f"papers.json.bak-pre-workspace-{stamp}").write_bytes(MANIFEST.read_bytes())
    return added


# ---------------------------------------------------------------- manifest view
def _year(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else v


def export_manifest():
    """Rewrite papers.json from the DB. This is what keeps the CLI working."""
    with conn() as c:
        rows = c.execute("SELECT * FROM items WHERE kind='paper' AND deleted=0 "
                         "ORDER BY ord, created, id").fetchall()
        meta = {r["k"]: r["v"] for r in c.execute("SELECT k,v FROM meta")}
    papers = []
    for i, r in enumerate(rows, 1):
        p = {"id": r["id"], "order": r["ord"] or i, "title": r["title"],
             "authors": r["authors"], "year": _year(r["year"]), "venue": r["venue"],
             "url": r["url"], "access": r["access"]}
        if r["notes"]:
            p["notes"] = r["notes"]
        papers.append(p)
    doc = {"title": meta.get("book_title", "Library"),
           "author": meta.get("book_author", "Various"), "papers": papers}
    tmp = ROOT / "papers.json.part"
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(MANIFEST)
    return len(papers)


# ---------------------------------------------------------------- folders
def _free_name(c, parent_id, name, exclude=None):
    """Resolve a UNIQUE(parent,name) clash by suffixing, rather than failing."""
    base, n, out = name, 2, name
    while True:
        row = c.execute("SELECT id FROM folders WHERE parent_id=? AND name=?",
                        (parent_id, out)).fetchone()
        if not row or row["id"] == exclude:
            return out
        out = f"{base} ({n})"
        n += 1


def folder_exists(c, fid) -> bool:
    return fid == ROOT_ID or bool(c.execute("SELECT 1 FROM folders WHERE id=?", (fid,)).fetchone())


def ancestors(c, fid):
    out, seen = [], set()
    while fid and fid != ROOT_ID and fid not in seen:
        seen.add(fid)
        r = c.execute("SELECT id,parent_id,name FROM folders WHERE id=?", (fid,)).fetchone()
        if not r:
            break
        out.append({"id": r["id"], "name": r["name"]})
        fid = r["parent_id"]
    return list(reversed(out))


def create_folder(name, parent_id=ROOT_ID):
    name = clean_name(name, "New folder")
    with ALLOC, conn(write=True) as c:
        if not folder_exists(c, parent_id):
            raise KeyError("no such parent folder")
        name = _free_name(c, parent_id, name)
        cur = c.execute("INSERT INTO folders(parent_id,name,created) VALUES(?,?,?)",
                        (parent_id, name, time.time()))
        fid = cur.lastrowid
    return {"id": fid, "parent_id": parent_id, "name": name}


def update_folder(fid, name=None, parent_id=None):
    if fid == ROOT_ID:
        raise ValueError("the root folder cannot be renamed or moved")
    with ALLOC, conn(write=True) as c:
        row = c.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
        if not row:
            raise KeyError("no such folder")
        new_parent = row["parent_id"] if parent_id is None else parent_id
        if new_parent != row["parent_id"]:
            if not folder_exists(c, new_parent):
                raise KeyError("no such parent folder")
            # A folder may not become its own descendant.
            if new_parent == fid or fid in {a["id"] for a in ancestors(c, new_parent)}:
                raise ValueError("cannot move a folder inside itself")
        new_name = row["name"] if name is None else clean_name(name, row["name"])
        new_name = _free_name(c, new_parent, new_name, exclude=fid)
        c.execute("UPDATE folders SET name=?, parent_id=? WHERE id=?", (new_name, new_parent, fid))
    return {"id": fid, "parent_id": new_parent, "name": new_name}


def delete_folder(fid):
    """Remove a folder; its children move up to its parent.

    Nothing on disk is touched and no item is lost -- deleting a folder is an
    organisational act, never a destructive one.
    """
    if fid == ROOT_ID:
        raise ValueError("the root folder cannot be deleted")
    with ALLOC, conn(write=True) as c:
        row = c.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
        if not row:
            raise KeyError("no such folder")
        up = row["parent_id"]
        for ch in c.execute("SELECT id,name FROM folders WHERE parent_id=?", (fid,)).fetchall():
            c.execute("UPDATE folders SET parent_id=?, name=? WHERE id=?",
                      (up, _free_name(c, up, ch["name"]), ch["id"]))
        moved = c.execute("UPDATE items SET folder_id=? WHERE folder_id=?", (up, fid)).rowcount
        c.execute("DELETE FROM folders WHERE id=?", (fid,))
    return {"deleted": fid, "reparented_to": up, "items_moved": moved}


def ensure_path(parts, parent_id=ROOT_ID):
    """Get-or-create a chain of folders. Used to rebuild an uploaded directory tree."""
    fid, created = parent_id, 0
    with ALLOC, conn(write=True) as c:
        if not folder_exists(c, fid):
            fid = ROOT_ID
        for raw in parts:
            nm = clean_name(raw, "")
            if not nm:
                continue
            row = c.execute("SELECT id FROM folders WHERE parent_id=? AND name=?",
                            (fid, nm)).fetchone()
            if row:
                fid = row["id"]
            else:
                cur = c.execute("INSERT INTO folders(parent_id,name,created) VALUES(?,?,?)",
                                (fid, nm, time.time()))
                fid = cur.lastrowid
                created += 1
    return fid, created


def list_folders():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id,parent_id,name,created FROM folders ORDER BY name COLLATE NOCASE")]


# ---------------------------------------------------------------- items
def next_ord(c):
    r = c.execute("SELECT MAX(ord) m FROM items").fetchone()
    return int(r["m"] or 0) + 1


def add_paper(filename, folder_id=ROOT_ID, title=None, authors="uploaded",
              venue="uploaded", year="", url=None, access="dropin", notes=""):
    """Reserve an id + DB row for a new PDF. The caller writes the bytes."""
    with ALLOC, conn(write=True) as c:
        if not folder_exists(c, folder_id):
            folder_id = ROOT_ID
        pid = unique_id(c, slug(filename))
        name = clean_name(filename, f"{pid}.pdf")
        c.execute(
            "INSERT INTO items(id,folder_id,kind,name,title,authors,year,venue,url,access,notes,"
            "ord,created) VALUES(?,?,'paper',?,?,?,?,?,?,?,?,?,?)",
            (pid, folder_id, name, title or _pretty(name), authors, str(year), venue, url,
             access, notes, next_ord(c), time.time()))
    export_manifest()          # the CLI reads papers.json, so keep the view current
    return pid


def add_attachment(filename, folder_id=ROOT_ID, stored=None, nbytes=0):
    with ALLOC, conn(write=True) as c:
        if not folder_exists(c, folder_id):
            folder_id = ROOT_ID
        pid = unique_id(c, slug(filename))
        name = clean_name(filename, pid)
        c.execute(
            "INSERT INTO items(id,folder_id,kind,name,title,ord,created,stored,bytes) "
            "VALUES(?,?,'attachment',?,?,?,?,?,?)",
            (pid, folder_id, name, name, 0, time.time(), stored, nbytes))
    return pid


def set_stored(pid, stored, nbytes):
    """Record where an attachment's bytes landed under files/."""
    with conn(write=True) as c:
        c.execute("UPDATE items SET stored=?, bytes=? WHERE id=?", (stored, int(nbytes), pid))


def item_by_stored(stored):
    with conn() as c:
        r = c.execute("SELECT * FROM items WHERE stored=?", (stored,)).fetchone()
        return dict(r) if r else None


def _pretty(filename):
    """Display name for an upload: the PDF's own name, lightly tidied."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", filename or "").strip()
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem[:160] or "Untitled"


def get_item(pid):
    with conn() as c:
        r = c.execute("SELECT * FROM items WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def update_item(pid, folder_id=None, title=None):
    with ALLOC, conn(write=True) as c:
        row = c.execute("SELECT * FROM items WHERE id=?", (pid,)).fetchone()
        if not row:
            raise KeyError("no such item")
        fid = row["folder_id"] if folder_id is None else folder_id
        if not folder_exists(c, fid):
            raise KeyError("no such folder")
        ttl = row["title"] if title is None else (clean_name(title, row["title"]) or row["title"])
        c.execute("UPDATE items SET folder_id=?, title=? WHERE id=?", (fid, ttl, pid))
    export_manifest()
    return {"id": pid, "folder_id": fid, "title": ttl}


def delete_item(pid):
    """Archive an item. Files on disk are never unlinked -- see README."""
    with ALLOC, conn(write=True) as c:
        if not c.execute("SELECT 1 FROM items WHERE id=?", (pid,)).fetchone():
            raise KeyError("no such item")
        c.execute("UPDATE items SET deleted=1 WHERE id=?", (pid,))
    export_manifest()
    return {"id": pid, "archived": True}


def restore_item(pid):
    with ALLOC, conn(write=True) as c:
        c.execute("UPDATE items SET deleted=0 WHERE id=?", (pid,))
    export_manifest()
    return {"id": pid, "archived": False}


def list_items(include_deleted=False):
    q = "SELECT * FROM items" + ("" if include_deleted else " WHERE deleted=0") + \
        " ORDER BY ord, created, id"
    with conn() as c:
        return [dict(r) for r in c.execute(q)]


# ---------------------------------------------------------------- cli
def _status():
    items = list_items(include_deleted=True)
    folders = list_folders()
    papers = [i for i in items if i["kind"] == "paper" and not i["deleted"]]
    print(f"db        : {DB}")
    print(f"folders   : {len(folders)}")
    print(f"items     : {len(items)}  ({len(papers)} papers, "
          f"{sum(1 for i in items if i['kind']=='attachment')} attachments, "
          f"{sum(1 for i in items if i['deleted'])} archived)")
    by = {f["id"]: f["name"] for f in folders}
    for i in items:
        loc = by.get(i["folder_id"], "/")
        wav = (OUT / f"{i['id']}.wav")
        mark = f"{wav.stat().st_size/1e6:>8.1f} MB wav" if wav.exists() else " " * 15
        print(f"  [{i['kind'][:5]:<5}] {i['id'][:44]:<45} {mark}  in {loc}")


def main():
    ap = argparse.ArgumentParser(description="workspace store maintenance")
    ap.add_argument("--init", action="store_true", help="create/migrate the DB, rewrite papers.json")
    ap.add_argument("--export", action="store_true", help="rewrite papers.json from the DB")
    ap.add_argument("--status", action="store_true", help="show the workspace contents")
    a = ap.parse_args()
    if a.init:
        fresh = init()
        print(("created" if fresh else "opened") + f" {DB}")
    if a.export or a.init:
        print(f"papers.json: {export_manifest()} papers")
    if a.status or not (a.init or a.export):
        init()
        _status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
