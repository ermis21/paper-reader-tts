#!/usr/bin/env python3
"""Title detection, title provenance and the stale-cache guard -- no corpus needed.

PDFs are synthesised with PyMuPDF and the workspace runs against a throwaway
directory, so this needs only the venv:

    ./venv/bin/python tests/test_titles.py
"""
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

import extract  # noqa: E402
import titles  # noqa: E402
import workspace as ws  # noqa: E402

failures = 0


def check(name, cond, extra=""):
    global failures
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    failures += not cond


TMP = pathlib.Path(tempfile.mkdtemp(prefix="pa-titles-"))


def pdf(name, lines, meta=None, stamp=False, page2=""):
    """lines: (text, fontsize, y) drawn at x=72 on a US-letter page."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for text, size, y in lines:
        page.insert_text((72, y), text, fontsize=size)
    if stamp:
        page.insert_text((30, 560), "arXiv:2106.09685v2 [cs.CL] 16 Oct 2021", fontsize=20, rotate=90)
    if page2:
        doc.new_page(width=612, height=792).insert_text((72, 72), page2, fontsize=10)
    if meta is not None:
        doc.set_metadata({"title": meta})
    path = TMP / name
    doc.save(path)
    return path


def title(path):
    hit = titles.detect(path)
    return (hit or {}).get("title"), (hit or {}).get("source")


BODY = [("Abstract", 12, 260), ("We study the problem at length and report results.", 10, 280)]

print("-- detection")
t, s = title(pdf("arxiv.pdf", [("Training Compute-Optimal Large", 17, 100), ("Language Models", 17, 121),
                               ("Jordan Hoffmann, Sebastian Borgeaud", 12, 150), *BODY], stamp=True))
check("wrapped title from the typesetting; rotated arXiv stamp ignored",
      t == "Training Compute-Optimal Large Language Models" and s == "layout", repr(t))

t, s = title(pdf("dvi.pdf", [("Provably Bounded-Optimal Agents", 17, 100), *BODY], meta="final1.dvi"))
check("junk metadata (a file name) is not trusted", t == "Provably Bounded-Optimal Agents" and s == "layout",
      repr((t, s)))

t, s = title(pdf("ocr.pdf", [("Missing Informatlon (Applicable and Inapplicable)", 14, 90),
                             ("in Relational Databases", 14, 108), *BODY],
                 meta="Missing Information (Applicable and Inapplicable) in Relational Databases"))
check("metadata printed at title size wins, and fixes the OCR slip",
      t == "Missing Information (Applicable and Inapplicable) in Relational Databases" and s == "metadata",
      repr((t, s)))

t, s = title(pdf("banner.pdf", [("Future Generation Computer Systems", 9, 60),
                                ("The Open Provenance Model Core Specification", 16, 110), *BODY],
                 meta="Future Generation Computer Systems"))
check("metadata that is printed, but NOT at title size, loses to the typesetting",
      t == "The Open Provenance Model Core Specification" and s == "layout", repr((t, s)))

t, s = title(pdf("site.pdf", [("The Statistical Crisis in Science", 14, 100), *BODY],
                 meta="The Statistical Crisis in Science » American Scientist"))
check("a site suffix on the metadata title is dropped", t == "The Statistical Crisis in Science", repr(t))

t, _ = title(pdf("caps.pdf", [("LORA: LOW-RANK ADAPTATION OF LARGE LAN-", 17, 100), ("GUAGE MODELS", 17, 121),
                              *BODY],
                 page2="We propose Low-Rank Adaptation, or LoRA, for large language models.\n"
                       "LoRA reduces the number of trainable parameters. BLEU improves."))
check("ALL CAPS recased from the paper's own spelling; line-break hyphen rejoined",
      t == "LoRA: Low-Rank Adaptation of Large Language Models", repr(t))

t, _ = title(pdf("hyphen.pdf", [("Barlow Twins: Self-", 17, 100), ("Supervised Learning via Redundancy", 17, 121),
                                *BODY]))
check("a real hyphen at a line break is kept", t == "Barlow Twins: Self-Supervised Learning via Redundancy", repr(t))

t, _ = title(pdf("label.pdf", [("Article", 22, 50), ("Why Most Published Research Findings", 16, 100),
                               ("Are False", 16, 119), *BODY]))
check("a bigger section label ('Article') is not the title", t == "Why Most Published Research Findings Are False",
      repr(t))

t, _ = title(pdf("clipped.pdf", [("The OBO Foundry: coordinated evolution of ontologies to support", 20, 100),
                                 *BODY]))
check("a title running off the page is declined, not truncated", t is None, repr(t))

t, _ = title(pdf("blank.pdf", []))
check("no text layer -> None (the upload keeps its filename)", t is None, repr(t))

print("-- naming")
check("filename_for: colon and slash made portable, ? dropped",
      ws.filename_for("LoRA: Low-Rank / Adaptation?", ".pdf") == "LoRA - Low-Rank Adaptation.pdf",
      ws.filename_for("LoRA: Low-Rank / Adaptation?", ".pdf"))
long = ws.filename_for("word " * 60, ".wav")
check("filename_for: long titles cut at a word, extension kept",
      long.endswith("word.wav") and len(long) <= ws.FILENAME_MAX + 4, repr(long[-20:]))
check("clean_filename keeps the extension past the length cap",
      ws.clean_filename("A" * 100 + ".pdf", "x").endswith("A.pdf"))

print("-- workspace provenance")
for attr, sub in (("PDFS", "pdfs"), ("DROPIN", "dropin"), ("TEXT", "text"), ("CACHE", "cache"),
                  ("OUT", "out"), ("FILES", "files")):
    setattr(ws, attr, TMP / sub)
ws.DB, ws.MANIFEST = TMP / "workspace.sqlite", TMP / "papers.json"
with sqlite3.connect(ws.DB) as db:                 # a schema-1 database, as it existed before
    db.executescript("""
      CREATE TABLE items(id TEXT PRIMARY KEY, folder_id INTEGER NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT 'paper', name TEXT NOT NULL, title TEXT NOT NULL,
        authors TEXT NOT NULL DEFAULT '', year TEXT NOT NULL DEFAULT '', venue TEXT NOT NULL DEFAULT '',
        url TEXT, access TEXT NOT NULL DEFAULT 'dropin', notes TEXT NOT NULL DEFAULT '',
        ord INTEGER NOT NULL DEFAULT 0, stored TEXT, bytes INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
      INSERT INTO items(id,name,title,authors,venue,created) VALUES
        ('chinchilla','Chinchilla.pdf','Chinchilla','uploaded','uploaded',1),
        ('lora','LoRA.pdf','My LoRA notes','uploaded','uploaded',2),
        ('01-cook','01-cook.pdf','How Complex Systems Fail','Richard I. Cook','CtL',3);
    """)
ws.init()
src = {r["id"]: r["title_source"] for r in ws.list_items()}
check("migration: an upload never renamed becomes pending", src["chinchilla"] == "pending", src["chinchilla"])
check("migration: an upload renamed by hand becomes manual", src["lora"] == "manual", src["lora"])
check("migration: a manifest entry becomes manifest", src["01-cook"] == "manifest", src["01-cook"])
check("migration: orig_name backfilled", ws.get_item("chinchilla")["orig_name"] == "Chinchilla.pdf")

pdf("pdfs/chinchilla.pdf", [("Training Compute-Optimal Large", 17, 100), ("Language Models", 17, 121), *BODY])
titles.retitle(apply=True)
it = ws.get_item("chinchilla")
check("retitle --apply titles a pending upload",
      it["title"] == "Training Compute-Optimal Large Language Models" and it["title_source"] == "layout",
      repr(it["title"]))
check("... its file name follows the title", it["name"] == "Training Compute-Optimal Large Language Models.pdf",
      it["name"])
check("... and papers.json (what the CLI reads) says so",
      "Training Compute-Optimal" in ws.MANIFEST.read_text())
check("a manual title is never overwritten", not ws.set_detected_title("lora", "Something Else", "layout")
      and ws.get_item("lora")["title"] == "My LoRA notes")

ws.update_item("chinchilla", title="Chinchilla: compute-optimal scaling")
it = ws.get_item("chinchilla")
check("rename: title_source manual, file name follows",
      it["title_source"] == "manual" and it["name"] == "Chinchilla - compute-optimal scaling.pdf", repr(it["name"]))
check("rename then detection: the person wins",
      not ws.set_detected_title("chinchilla", "Training Compute-Optimal", "layout"))

pid = ws.add_paper("2602.03249v2.pdf")
it = ws.get_item(pid)
check("a new upload starts pending, under its own name",
      it["title_source"] == "pending" and it["name"] == "2602.03249v2.pdf" and it["title"] == "2602.03249v2",
      repr((it["title_source"], it["name"], it["title"])))
ws.set_detected_title(pid, None, None)
it = ws.get_item(pid)
check("nothing found: keeps its filename, marked `filename`",
      it["title_source"] == "filename" and it["title"] == "2602.03249v2")

print("-- spoken header and stale cache")
COOK = {"title": "How Complex Systems Fail", "authors": "Richard I. Cook", "year": 1998, "venue": "CtL"}
check("a fully described manifest entry is spoken exactly as before",
      extract.spoken_header(COOK) == "How Complex Systems Fail. By Richard I. Cook. Published 1998 in CtL.\n\n")
check("an upload's placeholders are not read aloud",
      extract.spoken_header({"title": "Are X a Mirage?", "authors": "uploaded", "venue": "uploaded",
                             "year": ""}) == "Are X a Mirage?\n\n")
extract.CACHE = TMP / "cache"
(extract.CACHE / "p").mkdir(parents=True)
check("no cached audio -> nothing to retire", extract.retire_stale_cache("p") is None)
(extract.CACHE / "p" / "00000.wav").write_bytes(b"RIFF")
moved = extract.retire_stale_cache("p")
check("changed text moves the cache aside, never deletes it",
      moved is not None and (moved / "00000.wav").exists() and not (extract.CACHE / "p").exists()
      and ".stale-" in moved.name, str(moved))

print(f"\n{failures} FAILURE(S)" if failures else "\nALL TITLE CHECKS PASSED")
sys.exit(1 if failures else 0)
