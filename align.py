#!/usr/bin/env python3
"""Align narration chunks to PDF page rects and audio time.

The reader page highlights the passage currently being narrated, but the audio
speaks text/<id>.txt (speech-normalised: citations stripped, "et al." -> "and
colleagues", running heads and references gone) while the user reads the raw
PDF. This bridges the two.

Method: both sides are reduced to skeleton tokens (lowercase alnum), then
difflib.SequenceMatcher finds matching blocks. Narration tokens are the needle
(every one should match); PDF words are the haystack (arbitrarily skippable --
that is where stripped citations, captions, running heads and references live).
Matching blocks are monotonic in both sequences, so a matched token can never
jump backwards through the document. Speech substitutions that change token
COUNT ("et al." 2 tokens -> "and colleagues" 2 tokens; "50%" 1 -> "50 percent"
2) simply cost a few unmatched needle tokens around a stable anchor, which is
why per-chunk confidence exists: below CONF_PAGE_ONLY the reader highlights
the whole page rather than risk a wrong rect.

Timings come from the cached chunk WAVs (frames / sample rate, exact) plus
build.py's inter-chunk GAP, imported -- never copy-pasted -- so a retune of
build.py cannot desync the reader.

Output: cache/<id>/align.json. It lives in the chunk cache on purpose:
extract.py's retire_stale_cache() moves that directory aside when the text
changes, so a stale alignment can never silently survive a re-extraction.

    ./venv/bin/python align.py [--only ID] [--report]
"""
import argparse
import difflib
import hashlib
import json
import pathlib
import re
import sys
import time
import unicodedata
import wave

import pymupdf

import build as buildmod
import synth

ROOT = pathlib.Path(__file__).resolve().parent
PDFS, TEXT, CACHE = ROOT/"pdfs", ROOT/"text", ROOT/"cache"
SR = 24000
VERSION = 1
CONF_PAGE_ONLY = 0.6     # below this the front end falls back to page-level highlight


def skeleton(tok):
    """lowercase alnum core of a token; "Smith," / "Smith)" / "SMITH" all equal."""
    t = unicodedata.normalize("NFKD", tok).lower()
    return re.sub(r"[^a-z0-9]+", "", t)


def pdf_words(pdf_path):
    """Every word in the PDF: (skeleton, page, x0, y0, x1, y1, block, line).

    sort=True matches extract.py's reading order -- including its documented
    two-column interleaving defect, which then cancels out because BOTH sides
    inherit it.
    """
    words = []
    doc = pymupdf.open(pdf_path)
    for pno, pg in enumerate(doc):
        for x0, y0, x1, y1, w, bno, lno, _ in pg.get_text("words", sort=True):
            s = skeleton(w)
            if s:
                words.append((s, pno, x0, y0, x1, y1, bno, lno))
    return words, doc.page_count


def needle_tokens(text):
    """(skeleton, char_start, char_end) per narration token."""
    return [(skeleton(m.group()), m.start(), m.end())
            for m in re.finditer(r"\S+", text) if skeleton(m.group())]


def align(hay, needle):
    """needle index -> hay index, from SequenceMatcher's monotonic equal blocks."""
    sm = difflib.SequenceMatcher(a=[h[0] for h in hay], b=[n[0] for n in needle],
                                 autojunk=True)
    n2h = {}
    for bl in sm.get_matching_blocks():
        for k in range(bl.size):
            n2h[bl.b + k] = bl.a + k
    return n2h


def chunk_rects(chunk, tokens, n2h, hay):
    """Per-line highlight rects for one chunk, merged within (page, block).

    Line-level unions read as clean highlighter strokes; merging adjacent
    matched lines of the same block keeps column rects separate on two-column
    pages (a whole-page union would paint both columns).
    """
    lo, hi = chunk[1], chunk[2]
    lines = {}                       # (page, block, line) -> [x0,y0,x1,y1]
    matched = total = 0
    for i, (sk, ts, te) in enumerate(tokens):
        if not (lo <= ts < hi):
            continue
        total += 1
        h = n2h.get(i)
        if h is None:
            continue
        matched += 1
        _, pno, x0, y0, x1, y1, bno, lno = hay[h]
        key = (pno, bno, lno)
        r = lines.get(key)
        if r is None:
            lines[key] = [x0, y0, x1, y1]
        else:
            r[0] = min(r[0], x0); r[1] = min(r[1], y0)
            r[2] = max(r[2], x1); r[3] = max(r[3], y1)
    # merge consecutive lines of the same block into one rect
    merged = {}                      # (page, block) -> [x0,y0,x1,y1]
    for (pno, bno, _), r in sorted(lines.items()):
        m = merged.get((pno, bno))
        if m is None:
            merged[(pno, bno)] = list(r)
        else:
            m[0] = min(m[0], r[0]); m[1] = min(m[1], r[1])
            m[2] = max(m[2], r[2]); m[3] = max(m[3], r[3])
    rects = [{"page": pno,
              "x0": round(r[0], 1), "y0": round(r[1], 1),
              "x1": round(r[2], 1), "y1": round(r[3], 1)}
             for (pno, _), r in sorted(merged.items())]
    conf = matched / max(1, total)
    return rects, conf


def chunk_durations(cd):
    """Seconds per cached chunk WAV, from frame counts (exact, no decoding)."""
    durs = {}
    if not cd.is_dir():
        return durs
    for w in cd.glob("*.wav"):
        try:
            with wave.open(str(w), "rb") as f:
                durs[int(w.stem)] = f.getnframes() / f.getframerate()
        except (wave.Error, ValueError, EOFError):
            continue
    return durs


def build_alignment(pid):
    txt_path = TEXT/f"{pid}.txt"
    pdf_path = PDFS/f"{pid}.pdf"
    text = txt_path.read_text()
    hay, npages = pdf_words(pdf_path)
    tokens = needle_tokens(text)
    n2h = align(hay, tokens)
    spans = synth.chunk_spans(text)
    durs = chunk_durations(CACHE/pid)
    gap = buildmod.GAP
    chunks = []
    t = 0.0
    for i, sp in enumerate(spans):
        rects, conf = chunk_rects(sp, tokens, n2h, hay)
        dur = durs.get(i)
        if dur is not None:
            t_start, t_end = t, t + dur
            t = t_end + gap
        else:
            t_start = t_end = None
        chunks.append({"i": i, "char_start": sp[1], "char_end": sp[2],
                       "t_start": t_start, "t_end": t_end,
                       "rects": rects, "conf": round(conf, 3),
                       "page_only": None})
    # page_only: where the reader falls back when a chunk can't be trusted at
    # rect level. Two cases: (a) conf below CONF_PAGE_ONLY -- the rects are too
    # noisy to paint, but their page is usually still right; (b) no rects at
    # all (table dumps, interleaved figure text, the spoken header) -- inherit
    # the nearest neighbouring chunk's page. A wrong rect is a lie; a coarse
    # page is honest.
    for c in chunks:
        if c["rects"] and c["conf"] < CONF_PAGE_ONLY:
            c["page_only"] = c["rects"][0]["page"]
    last = None
    for c in chunks:
        if c["rects"]:
            last = c["rects"][0]["page"]
        elif c["page_only"] is None:
            c["page_only"] = last
    nxt = None
    for c in reversed(chunks):
        if c["rects"]:
            nxt = c["rects"][0]["page"]
        elif c["page_only"] is None:
            c["page_only"] = nxt
    return {
        "version": VERSION,
        "text_sha1": hashlib.sha1(text.encode()).hexdigest(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "gap_seconds": gap,
        "pdf_pages": npages,
        "audio_complete": len(durs) == len(spans) and len(spans) > 0,
        "chunks": chunks,
    }


def select_ids(papers, only):
    """Exact id wins; otherwise substring, mirroring synth.py/extract.py."""
    if not only:
        return {p["id"] for p in papers}
    exact = {p["id"] for p in papers if p["id"] == only}
    return exact or {p["id"] for p in papers if only in p["id"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--report", action="store_true", help="report only, do not write")
    a = ap.parse_args()

    papers = json.loads((ROOT/"papers.json").read_text())["papers"]
    sel = select_ids(papers, a.only)
    print(f"{'paper':<42}{'chunks':>7}{'>=.95':>7}{'>=.6':>7}{'<.6':>6}{'audio':>7}{'time':>6}")
    worst = []
    for p in papers:
        pid = p["id"]
        if pid not in sel:
            continue
        if not (TEXT/f"{pid}.txt").exists() or not (PDFS/f"{pid}.pdf").exists():
            continue
        t0 = time.time()
        al = build_alignment(pid)
        el = time.time() - t0
        confs = [c["conf"] for c in al["chunks"]]
        hi = sum(c >= 0.95 for c in confs)
        mid = sum(0.6 <= c < 0.95 for c in confs)
        lo = sum(c < 0.6 for c in confs)
        aud = "yes" if al["audio_complete"] else "partial" if any(
            c["t_start"] is not None for c in al["chunks"]) else "no"
        print(f"{pid[:41]:<42}{len(confs):>7}{hi:>7}{mid:>7}{lo:>6}{aud:>7}{el:>5.1f}s")
        worst += [(c["conf"], pid, c["i"]) for c in al["chunks"] if c["conf"] < 0.6]
        if not a.report:
            dest = CACHE/pid
            dest.mkdir(parents=True, exist_ok=True)
            (dest/"align.json").write_text(json.dumps(al))
    if worst:
        print(f"\n{len(worst)} low-confidence chunks (page-level fallback); worst 10:")
        for conf, pid, i in sorted(worst)[:10]:
            print(f"  {conf:5.2f}  {pid[:50]:<52} chunk {i}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
