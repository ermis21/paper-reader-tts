#!/usr/bin/env python3
"""Paper Audiobook: a workspace of documents you can listen to.

Runs on the homelab because the work is local -- the pipeline, the PDFs and the
audio all live on this disk. Tailnet-reachable and unauthenticated by design;
do not additionally port-forward it from the router.

Model
-----
ONE item per document. A row in `workspace.sqlite` *is* the paper; its PDF, its
extracted text, its per-chunk cache and its rendered WAV are properties of that
row, all derived from its id. There is no separate "wav entry" that can drift
out of sync with a "pdf entry", and the UI shows one card per document under the
PDF's own name.

Folders are virtual (see workspace.py): names live only in the database, so no
folder name ever becomes part of a path. Every file endpoint still resolves a
flat `^[A-Za-z0-9._-]+$` id inside one fixed base directory and refuses anything
that escapes it.
"""
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import shutil
import socket
import subprocess
import threading
import time
import unicodedata
import uuid
import wave
from queue import Queue
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

import workspace as ws

ROOT = pathlib.Path(__file__).resolve().parent
PDFS, DROPIN, TEXT, CACHE, OUT, FILES = ws.PDFS, ws.DROPIN, ws.TEXT, ws.CACHE, ws.OUT, ws.FILES
STAGE = ROOT / "tmp"
LINT = ROOT / "lint"          # written by lint_audio.py; absent == feature off
M4B_NAME = "audiobook.m4b"  # must match build.py

# AGPL-3.0 section 13: users interacting with this program over a network must
# be able to get its source. Point PA_SOURCE_URL at your copy -- including any
# modifications you made -- and the UI shows it as a link. Leaving it unset is
# fine for a purely local, single-user instance, where there are no such
# remote users.
SOURCE_URL = os.environ.get("PA_SOURCE_URL", "")
PY = str(ROOT / "venv" / "bin" / "python")

MAX_PDF_MB = 80             # unchanged from the single-file uploader
MAX_ATTACH_MB = 25          # non-renderable companions (notes, data, images)
MAX_FILES = 400             # per request; the UI batches larger folders
MAX_TOTAL_MB = 1024         # per request
MAX_DEPTH = 12              # nested folders recreated from an upload

app = FastAPI(title="Paper Audiobook")
JOBS: dict[str, dict] = {}
Q: "Queue[str]" = Queue()

ws.init()
STAGE.mkdir(exist_ok=True)
for _stale in STAGE.glob("*.part"):          # nothing in tmp/ survives a restart
    _stale.unlink(missing_ok=True)


# ---------------- cheap disk stats (memoised on mtime) ----------------
_memo: dict = {}


def _stat_key(p: pathlib.Path):
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def wav_seconds(p: pathlib.Path) -> float:
    k = _stat_key(p)
    if k is None:
        return 0.0
    hit = _memo.get(("wav", str(p)))
    if hit and hit[0] == k:
        return hit[1]
    try:
        with wave.open(str(p), "rb") as w:
            secs = w.getnframes() / w.getframerate()
    except Exception:
        secs = 0.0
    _memo[("wav", str(p))] = (k, secs)
    return secs


def chunk_counts(pid: str):
    """(chunks, suspect) for cache/<pid>/, recounted only when the directory changes."""
    d = CACHE / pid
    k = _stat_key(d)
    if k is None:
        return 0, 0
    hit = _memo.get(("chunks", pid))
    if hit and hit[0] == k:
        return hit[1]
    n = s = 0
    with os.scandir(d) as it:
        for e in it:
            if e.name.endswith(".wav"):
                n += 1
            elif e.name.endswith(".suspect"):
                s += 1
    _memo[("chunks", pid)] = (k, (n, s))
    return n, s


def lint_index() -> dict:
    """`lint/index.json` -> {id: summary}, memoised on mtime like everything else.

    Read-only and optional: if lint_audio.py has never run, this is `{}` and no
    payload gains a `lint` key. One `stat()` per call, no parsing per item.
    """
    p = LINT / "index.json"
    k = _stat_key(p)
    if k is None:
        return {}
    hit = _memo.get(("lint", ""))
    if hit and hit[0] == k:
        return hit[1]
    try:
        d = json.loads(p.read_text()).get("papers", {})
    except Exception:
        d = {}
    _memo[("lint", "")] = (k, d)
    return d


def size_of(p: pathlib.Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


# ---------------- render worker ----------------
def planned_chunks(pid):
    try:
        out = subprocess.run([PY, str(ROOT / "synth.py"), "--plan", "--only", pid],
                             cwd=ROOT, capture_output=True, text=True, timeout=180).stdout
        for line in out.splitlines():
            p = line.split()
            if len(p) == 3 and p[0] == pid and p[1].isdigit():
                return int(p[1])
    except Exception:
        pass
    return 0


def done_chunks(pid):
    return chunk_counts(pid)[0]


def run_step(pid, stage, args):
    JOBS[pid]["stage"] = stage
    r = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        JOBS[pid].update(state="error", stage=f"{stage} failed")
        JOBS[pid]["error"] = (r.stderr or r.stdout)[-600:]
        return False
    return True


TITLE_LOCK = threading.Lock()


def detect_titles(pids):
    """Read the real title off each upload still `pending`, via titles.py.

    A subprocess, never in-process: parsing an uploaded PDF is an attack on MuPDF
    (SECURITY.md), and a crash or hang there must not take the server down. The
    lock serialises an upload's batch, the startup sweep and the render worker --
    which calls this first, so extract.py speaks the real title rather than the
    filename. titles.py writes compare-and-set: a rename that lands first wins."""
    with TITLE_LOCK:
        todo = [p for p in pids if (ws.get_item(p) or {}).get("title_source") == "pending"]
        if not todo:
            return
        try:
            r = subprocess.run([PY, str(ROOT / "titles.py"), "--retitle", "--apply", "--only", *todo],
                               cwd=ROOT, capture_output=True, text=True, timeout=60 + 10 * len(todo))
            if r.returncode:
                print(f"titles.py exited {r.returncode}: {(r.stderr or r.stdout)[-400:]}", flush=True)
        except subprocess.TimeoutExpired:
            print(f"titles.py timed out on {len(todo)} PDF(s); they keep their filenames", flush=True)
        for p in todo:
            it = ws.get_item(p)
            if it and p in JOBS:
                JOBS[p]["title"] = it["title"]


def worker():
    while True:
        pid = Q.get()
        j = JOBS.setdefault(pid, {"title": pid})
        j.update(state="running", stage="extracting", pct=0)
        try:
            detect_titles([pid])            # before extract.py, which reads the title aloud
            if not run_step(pid, "extracting", [str(ROOT / "extract.py"), "--only", pid]):
                continue
            total = planned_chunks(pid)
            j["total"] = total
            j.update(stage="narrating", pct=0)
            stop = threading.Event()

            def tick(j=j, pid=pid, total=total, stop=stop):
                # Bound at definition: `worker` reuses these names on the next
                # job, and a ticker outliving its job would then report progress
                # against the wrong one.
                while not stop.wait(3):
                    if total:
                        j["pct"] = min(99, int(100 * done_chunks(pid) / total))
            threading.Thread(target=tick, daemon=True).start()
            ok = run_step(pid, "narrating", [str(ROOT / "synth.py"), "--only", pid])
            stop.set()
            if not ok:
                continue
            j.update(stage="assembling", pct=99)
            run_step(pid, "assembling", [str(ROOT / "build.py"), "--only", pid])
            j.update(state="done", stage="done", pct=100, finished=time.time())
        except Exception as e:
            j.update(state="error", stage="error", error=str(e)[:400])
        finally:
            Q.task_done()


threading.Thread(target=worker, daemon=True).start()
# Uploads still named after their file -- rows from before titles were read off the
# PDF, or an upload whose detection never got to run -- are titled in the background.
threading.Thread(target=lambda: detect_titles(
    [r["id"] for r in ws.list_items() if r.get("title_source") == "pending"]), daemon=True).start()


def enqueue(pid, title):
    JOBS[pid] = {"state": "queued", "stage": "queued", "pct": 0, "title": title}
    Q.put(pid)


# ---------------- page ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "static" / "index.html").read_text()


# ---------------- library ----------------
def item_payload(r: dict) -> dict:
    """One document, with every artefact hung off it. Never two rows for one paper."""
    pid = r["id"]
    out = {"id": pid, "kind": r["kind"], "folder_id": r["folder_id"],
           "title": r["title"], "name": r["name"], "order": r["ord"],
           "authors": r["authors"], "venue": r["venue"], "year": r["year"],
           "created": r["created"],
           # the name it was uploaded as, so search still finds "Chinchilla" after
           # the card says "Training Compute-Optimal Large Language Models"
           "orig_name": r.get("orig_name") or r["name"], "title_source": r.get("title_source", "")}
    if r["kind"] == "attachment":
        stored = r["stored"] or ""
        here = bool(stored) and (FILES / stored).exists()
        out["state"] = "file"
        out["file"] = {"present": here,
                       "bytes": (r["bytes"] or size_of(FILES / stored)) if here else 0,
                       "url": f"/file/{stored}" if here else None}
        return out

    pdf, txt, wav = PDFS / f"{pid}.pdf", TEXT / f"{pid}.txt", OUT / f"{pid}.wav"
    has_pdf, has_wav = pdf.exists(), wav.exists()
    chunks, suspect = chunk_counts(pid)
    job = JOBS.get(pid, {})
    out["pdf"] = {"present": has_pdf, "bytes": size_of(pdf),
                  "url": f"/pdf/{pid}.pdf" if has_pdf else None, "name": r["name"]}
    out["text"] = {"present": txt.exists(), "bytes": size_of(txt),
                   "url": f"/text/{pid}.txt" if txt.exists() else None}
    out["audio"] = {"present": has_wav, "bytes": size_of(wav),
                    "seconds": round(wav_seconds(wav)) if has_wav else 0,
                    "url": f"/audio/{pid}.wav" if has_wav else None,
                    "name": ws.filename_for(r["title"], ".wav") if has_wav else None}
    out["chunks"], out["suspect"] = chunks, suspect
    li = lint_index().get(pid)
    if li:
        out["lint"] = {"chunks": li.get("chunks", 0), "bad": li.get("bad", 0),
                       "bad_secs": li.get("bad_secs", 0), "tier2": li.get("tier2", False),
                       "classes": li.get("classes", {})}
    if job.get("state") in ("queued", "running"):
        out["state"] = "rendering" if job["state"] == "running" else "queued"
    elif has_wav:
        out["state"] = "narrated"
    elif has_pdf:
        out["state"] = "not rendered"
    else:
        out["state"] = "needs PDF"
    return out


@app.get("/api/library")
def library():
    items = [item_payload(r) for r in ws.list_items()]
    folders = ws.list_folders()
    papers = [i for i in items if i["kind"] == "paper"]
    tot_s = sum(i["audio"]["seconds"] for i in papers)
    tot_c = sum(i["chunks"] for i in papers)
    susp = sum(i["suspect"] for i in papers)
    lint_bad = sum(i.get("lint", {}).get("bad", 0) for i in papers)
    lint_seen = sum(1 for i in papers if "lint" in i)
    ready = sum(1 for i in papers if i["audio"]["present"])
    jobs = [{"id": k, "title": v.get("title", k), "state": v.get("state", "?"),
             "stage": v.get("stage", ""), "pct": v.get("pct", 0),
             "error": v.get("error", ""),
             "recent": time.time() - v.get("finished", 0) < 25}
            for k, v in JOBS.items()]
    return {
        "folders": folders, "items": items, "jobs": jobs,
        "total_seconds": tot_s, "total_chunks": tot_c, "suspect": susp,
        "lint_bad": lint_bad, "lint_papers": lint_seen,
        "ready": ready, "documents": len(papers), "files": len(items) - len(papers),
        "m4b": (OUT / M4B_NAME).exists(), "host": socket.gethostname(),
        "source_url": SOURCE_URL,
        # deprecated flat view, kept so anything still polling the old shape works
        "papers": [{"id": i["id"], "order": i["order"], "title": i["title"],
                    "authors": i["authors"], "venue": i["venue"],
                    "pdf": i["pdf"]["present"], "wav": i["audio"]["present"],
                    "seconds": i["audio"]["seconds"], "chunks": i["chunks"],
                    "suspect": i["suspect"], "bytes": i["audio"]["bytes"]} for i in papers],
    }


# ---------------- folders ----------------
async def _body(request: Request) -> dict:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@app.post("/api/folders")
async def folder_create(request: Request):
    b = await _body(request)
    try:
        return ws.create_folder(b.get("name", "New folder"), _int(b.get("parent_id"), ws.ROOT_ID))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.patch("/api/folders/{fid}")
async def folder_update(fid: int, request: Request):
    b = await _body(request)
    try:
        return ws.update_folder(fid, b.get("name"),
                                _int(b["parent_id"]) if "parent_id" in b else None)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/folders/{fid}")
def folder_delete(fid: int):
    """Children move up to the parent. No file is ever removed from disk."""
    try:
        return ws.delete_folder(fid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---------------- items ----------------
@app.patch("/api/items/{pid}")
async def item_update(pid: str, request: Request):
    b = await _body(request)
    try:
        return ws.update_item(pid, _int(b["folder_id"]) if "folder_id" in b else None,
                              b.get("title"))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.delete("/api/items/{pid}")
def item_delete(pid: str):
    """Archive: the row is hidden, the audio and cache stay on disk. See README."""
    try:
        return ws.delete_item(pid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/items/{pid}/render")
def item_render(pid: str):
    it = ws.get_item(pid)
    if not it or it["kind"] != "paper":
        raise HTTPException(404, "no such document")
    if not (PDFS / f"{pid}.pdf").exists():
        raise HTTPException(409, f"no PDF yet - drop one in as dropin/{pid}.pdf")
    if JOBS.get(pid, {}).get("state") in ("queued", "running"):
        raise HTTPException(409, "already rendering")
    enqueue(pid, it["title"])
    return {"id": pid, "queued": True}


# ---------------- reader: alignment + margin notes ----------------
def _paper_or_404(pid: str) -> dict:
    it = ws.get_item(pid)
    if not it or it["kind"] != "paper":
        raise HTTPException(404, "no such document")
    return it


def _alignment(pid: str) -> dict:
    """cache/<pid>/align.json, rebuilt on demand when missing or stale.

    Stale means: the text changed (sha1), align.py's format moved on (version),
    or audio has since completed (timings were null). Building takes seconds
    (no TTS), so doing it synchronously here beats a cache-state machine. The
    file lives in the chunk cache, so extract.py's retire_stale_cache() sweeps
    it aside on a text change -- the sha1 check is belt and braces."""
    import align as alignmod                    # lazy: pulls pymupdf + synth + build
    txt = TEXT / f"{pid}.txt"
    if not txt.exists():
        raise HTTPException(409, "no extracted text yet - render the document first")
    dest = CACHE / pid / "align.json"
    al = None
    if dest.exists():
        try:
            al = json.loads(dest.read_text())
        except (json.JSONDecodeError, OSError):
            al = None
    sha = hashlib.sha1(txt.read_bytes()).hexdigest()
    stale = (al is None
             or al.get("version") != alignmod.VERSION
             or al.get("text_sha1") != sha
             or (not al.get("audio_complete")
                 and any((CACHE / pid).glob("*.wav"))))
    if stale:
        al = alignmod.build_alignment(pid)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(al))
    return al


@app.get("/api/papers/{pid}/alignment")
def paper_alignment(pid: str):
    _paper_or_404(pid)
    if not (PDFS / f"{pid}.pdf").exists():
        raise HTTPException(409, "no PDF yet - the reader needs the original pages")
    return _alignment(pid)


def _chunk_quote(pid: str, chunk_idx: int) -> str:
    """The first ws.QUOTE_LEN chars of a chunk's text -- the note's re-anchoring
    lifeline. Computed server-side so the client can never quote one passage
    while anchoring another."""
    import synth                              # lazy: keeps numpy out of the hot path
    txt = TEXT / f"{pid}.txt"
    if not txt.exists():
        raise HTTPException(409, "no extracted text yet")
    spans = synth.chunk_spans(txt.read_text())
    if not 0 <= chunk_idx < len(spans):
        raise HTTPException(400, f"chunk_idx out of range (0..{len(spans)-1})")
    c, s, e = spans[chunk_idx]
    return c[:ws.QUOTE_LEN]


@app.get("/api/papers/{pid}/notes")
def paper_notes(pid: str):
    _paper_or_404(pid)
    return {"notes": ws.list_notes(pid)}


@app.post("/api/papers/{pid}/notes")
async def note_create(pid: str, request: Request):
    _paper_or_404(pid)
    b = await _body(request)
    idx = _int(b.get("chunk_idx"))
    if idx is None or idx < 0:
        raise HTTPException(400, "chunk_idx required")
    try:
        return ws.add_note(pid, idx, b.get("body", ""),
                           _chunk_quote(pid, idx), _int(b.get("char_off"), 0))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.patch("/api/notes/{nid}")
async def note_update(nid: int, request: Request):
    b = await _body(request)
    try:
        return ws.update_note(nid, b.get("body", ""))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/notes/{nid}")
def note_delete(nid: int):
    try:
        return ws.delete_note(nid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


# ---------------- upload ----------------
def _split_path(rel: str):
    """Untrusted relative path -> (folder components, basename). Never touches disk."""
    parts = [p for p in re.split(r"[\\/]+", rel or "") if p not in ("", ".", "..")]
    if not parts:
        return [], "file"
    return parts[:-1][:MAX_DEPTH], parts[-1]


async def _spill(up, tmp: pathlib.Path, cap: int):
    """Stream an upload to disk with a hard byte cap. Returns (bytes, head) or (None, head)."""
    n, head, over = 0, b"", False
    with open(tmp, "wb") as fh:
        while True:
            b = await up.read(262144)
            if not b:
                break
            n += len(b)
            if n > cap:
                over = True
                break
            if len(head) < 8:
                head += b[:8]
            fh.write(b)
    if over:
        tmp.unlink(missing_ok=True)
        return None, head
    return n, head


def _commit_pdf(base, folder_id, tmp: pathlib.Path):
    pid = ws.add_paper(base, folder_id)
    tmp.replace(DROPIN / f"{pid}.pdf")               # keep the pristine original
    shutil.copy2(DROPIN / f"{pid}.pdf", PDFS / f"{pid}.pdf")
    return pid, ws.get_item(pid)


def _commit_attachment(base, folder_id, tmp: pathlib.Path, nbytes):
    pid = ws.add_attachment(base, folder_id, stored=None, nbytes=nbytes)
    stored = f"{pid}{ws.safe_ext(base)}"
    tmp.replace(FILES / stored)
    ws.set_stored(pid, stored, nbytes)
    return pid


@app.post("/api/upload")
async def upload(request: Request):
    """Accepts one file, many files, or a whole directory tree.

    The relative path of each file arrives either as the multipart *filename*
    (what the browser sends when the UI passes `webkitRelativePath` as the
    FormData filename) or as a `path` field immediately before its file part.
    Folders in that path are recreated in the workspace -- as database rows, not
    as directories, so a hostile path can never escape anywhere.
    """
    queued, attached, skipped, made_folders = [], [], [], 0
    try:
        async with request.form(max_files=MAX_FILES + 8, max_fields=MAX_FILES + 8) as form:
            pairs = list(form.multi_items())
            base_folder = ws.ROOT_ID
            for k, v in pairs:
                if k in ("folder_id", "folder") and not isinstance(v, StarletteUploadFile):
                    base_folder = _int(v, ws.ROOT_ID)

            files, pending = [], None
            for k, v in pairs:
                if isinstance(v, StarletteUploadFile):
                    files.append((pending or v.filename or "file", v))
                    pending = None
                elif k in ("path", "paths", "webkitRelativePath", "relpath"):
                    pending = str(v)
            if not files:
                raise HTTPException(400, "no files in request")
            if len(files) > MAX_FILES:
                raise HTTPException(413, f"more than {MAX_FILES} files in one request")

            cache, total = {}, 0
            for rel, up in files:
                dirs, base = _split_path(rel)
                key = (base_folder, tuple(dirs))
                if key not in cache:
                    fid, made = ws.ensure_path(dirs, base_folder)
                    cache[key] = fid
                    made_folders += made
                folder_id = cache[key]

                is_pdf = base.lower().endswith(".pdf")
                cap = (MAX_PDF_MB if is_pdf else MAX_ATTACH_MB) * 1024 * 1024
                tmp = STAGE / f"{uuid.uuid4().hex}.part"
                n, head = await _spill(up, tmp, cap)
                if n is None:
                    skipped.append({"path": rel, "reason":
                                    f"larger than {cap // (1024*1024)} MB"})
                    continue
                total += n
                if total > MAX_TOTAL_MB * 1024 * 1024:
                    tmp.unlink(missing_ok=True)
                    skipped.append({"path": rel, "reason": f"request over {MAX_TOTAL_MB} MB"})
                    break
                if is_pdf and not head.startswith(b"%PDF-"):
                    tmp.unlink(missing_ok=True)
                    skipped.append({"path": rel, "reason": "not a PDF (bad header)"})
                    continue

                # The disk work (an 80 MB copy) and the DB writes are blocking, so
                # they go to the threadpool: a 200-file folder upload must not
                # stall the event loop and stop the library from answering.
                if is_pdf:
                    pid, it = await run_in_threadpool(_commit_pdf, base, folder_id, tmp)
                    enqueue(pid, it["title"])
                    queued.append({"id": pid, "title": it["title"], "name": it["name"],
                                   "folder_id": folder_id})
                else:
                    pid = await run_in_threadpool(_commit_attachment, base, folder_id, tmp, n)
                    attached.append({"id": pid, "name": base, "folder_id": folder_id,
                                     "bytes": n})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"upload failed: {type(e).__name__}: {str(e)[:200]}") from e
    finally:
        for stale in STAGE.glob("*.part"):
            stale.unlink(missing_ok=True)
    if queued:      # titles appear within seconds, long before each paper's turn to render
        threading.Thread(target=detect_titles, args=([q["id"] for q in queued],), daemon=True).start()
    return {"queued": queued, "attachments": attached, "skipped": skipped,
            "folders_created": made_folders}


# ---------------- file serving ----------------
SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def _resolve(base: pathlib.Path, name: str) -> pathlib.Path:
    """The single traversal guard used by every file endpoint.

    `name` must be a flat id-shaped basename, and the resolved path must still
    live inside `base` after symlinks are followed.
    """
    if not SAFE.match(name or "") or ".." in name:
        raise HTTPException(400, "bad name")
    root = base.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or root not in path.parents or not path.is_file():
        raise HTTPException(404)
    return path


def _ranged(path: pathlib.Path, request: Request, ctype: str, disposition: str | None = None,
            filename: str | None = None, extra: dict | None = None):
    """Range-aware file response. Seeking a 300 MB WAV depends on this."""
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes"}
    if extra:
        headers.update(extra)
    if disposition:
        headers["Content-Disposition"] = disposition
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(path, media_type=ctype,
                            headers={**headers, "Content-Length": str(size)},
                            filename=filename if filename and not disposition else None)
    m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise HTTPException(416, "bad range")
    if m.group(1):
        s = int(m.group(1))
        e = int(m.group(2)) if m.group(2) else size - 1
    else:                                        # suffix range: bytes=-N
        s, e = max(0, size - int(m.group(2))), size - 1
    s, e = max(0, s), min(e, size - 1)
    if s > e or s >= size:
        raise HTTPException(416, "bad range")

    def stream():
        with open(path, "rb") as f:
            f.seek(s)
            left = e - s + 1
            while left > 0:
                b = f.read(min(262144, left))
                if not b:
                    break
                left -= len(b)
                yield b
    return StreamingResponse(stream(), status_code=206, media_type=ctype,
                             headers={**headers, "Content-Range": f"bytes {s}-{e}/{size}",
                                      "Content-Length": str(e - s + 1)})


def _inline_as(filename: str) -> str:
    """Content-Disposition that keeps the PDF viewer and <audio> working (`inline`)
    while a save or a download link gets the paper's title as its file name.
    RFC 6266: an ASCII fallback for old clients, then the exact UTF-8 name."""
    plain = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    plain = re.sub(r"[^\w .\-()\[\],]+", "_", plain).strip() or "download"
    return f"inline; filename=\"{plain}\"; filename*=UTF-8''{quote(filename, safe='')}"


@app.get("/audio/{name}")
def audio(name: str, request: Request):
    path = _resolve(OUT, name)
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    it = ws.get_item(path.stem)
    dl = ws.filename_for(it["title"], path.suffix) if it else path.name
    return _ranged(path, request, ctype, disposition=_inline_as(dl))


@app.get("/pdf/{name}")
def pdf(name: str, request: Request):
    path = _resolve(PDFS, name)
    it = ws.get_item(path.stem)
    dl = it["name"] if it else path.name
    if not dl.lower().endswith(".pdf"):
        dl += ".pdf"
    return _ranged(path, request, "application/pdf", disposition=_inline_as(dl),
                   extra={"X-Content-Type-Options": "nosniff"})


@app.get("/text/{name}")
def text(name: str, request: Request):
    path = _resolve(TEXT, name)
    return _ranged(path, request, "text/plain; charset=utf-8",
                   extra={"X-Content-Type-Options": "nosniff"})


@app.get("/file/{name}")
def attachment(name: str, request: Request):
    """Uploaded companions. Always a download, never a rendered document.

    Served as octet-stream + `nosniff` + `attachment` so an uploaded .html or
    .svg can never execute as same-origin script against this app.
    """
    path = _resolve(FILES, name)
    it = ws.item_by_stored(name) or {}
    dl = re.sub(r'[^\w .\-()\[\]]+', "_", it.get("name") or name)[:120] or name
    return _ranged(path, request, "application/octet-stream",
                   disposition=f'attachment; filename="{dl}"',
                   extra={"X-Content-Type-Options": "nosniff"})


@app.get("/m4b")
def m4b():
    p = OUT / M4B_NAME
    if not p.exists():
        raise HTTPException(404, "M4B not built (needs ffmpeg)")
    return FileResponse(p, media_type="audio/mp4", filename=M4B_NAME)


if __name__ == "__main__":
    import uvicorn
    # Loopback by default: this app is unauthenticated and an upload starts a
    # subprocess pipeline. Set PA_HOST=0.0.0.0 to expose it on a LAN/VPN you
    # trust -- deliberately, and never straight to the internet. See SECURITY.md.
    uvicorn.run(app, host=os.environ.get("PA_HOST", "127.0.0.1"),
                port=int(os.environ.get("PA_PORT", "3002")), log_level="info")
