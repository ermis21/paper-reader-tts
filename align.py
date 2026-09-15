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
VERSION = 2
CONF_PAGE_ONLY = 0.6     # below this the front end falls back to page-level highlight
COVER_MIN = 0.35         # a PDF line needs >=35% matched words to earn a rect at all


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


def line_word_counts(hay):
    """Words per (page, block, line) -- the coverage denominators."""
    cnt = {}
    for h in hay:
        key = (h[1], h[6], h[7])
        cnt[key] = cnt.get(key, 0) + 1
    return cnt


def span_rects(lo, hi, tokens, n2h, hay, line_words):
    """Per-line highlight rects for the narration tokens in text[lo:hi].

    v2: one rect per (page, block, line) that is mostly narration. A line where
    fewer than COVER_MIN of the words matched -- typically a line that is
    mostly a stripped citation -- gets NO rect, rather than a highlight painted
    over text that is never read. Within a kept line the rect is the union of
    the MATCHED words' boxes only, so a stripped parenthetical at the line's
    end stays unhighlighted. No merging across lines: highlight blocks and
    citation holes read exactly as they sound.
    """
    lines = {}                       # (page, block, line) -> [x0,y0,x1,y1,n]
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
            lines[key] = [x0, y0, x1, y1, 1]
        else:
            r[0] = min(r[0], x0); r[1] = min(r[1], y0)
            r[2] = max(r[2], x1); r[3] = max(r[3], y1)
            r[4] += 1
    rects = []
    for key, r in sorted(lines.items()):
        if r[4] / line_words[key] < COVER_MIN:
            continue
        pno = key[0]
        rects.append({"page": pno,
                      "x0": round(r[0], 1), "y0": round(r[1], 1),
                      "x1": round(r[2], 1), "y1": round(r[3], 1)})
    conf = matched / max(1, total)
    return rects, conf


def sentence_spans(text, lo, hi):
    """Absolute (start, end) char offsets of the sentences inside text[lo:hi].

    Same split rule synth.chunk_spans uses, applied within one chunk. A chunk
    cut mid-sentence by the pathological-length path simply yields a sentence
    piece -- still a contiguous, speakable segment."""
    body = text[lo:hi]
    spans, start = [], 0
    for m in synth.SENT_END.finditer(body):
        s = body[start:m.start()]
        if s.strip():
            lead = len(s) - len(s.lstrip())
            spans.append((lo + start + lead, lo + m.start()))
        start = m.end()
    s = body[start:]
    if s.strip():
        lead = len(s) - len(s.lstrip())
        trail = len(s.rstrip())
        spans.append((lo + start + lead, lo + start + trail))
    return spans


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
    line_words = line_word_counts(hay)
    spans = synth.chunk_spans(text)
    durs = chunk_durations(CACHE/pid)
    gap = buildmod.GAP
    chunks = []
    t = 0.0
    for i, sp in enumerate(spans):
        rects, conf = span_rects(sp[1], sp[2], tokens, n2h, hay, line_words)
        dur = durs.get(i)
        if dur is not None:
            t_start, t_end = t, t + dur
            t = t_end + gap
        else:
            t_start = t_end = None
        clen = max(1, sp[2] - sp[1])
        segs = []
        for cs, ce in sentence_spans(text, sp[1], sp[2]):
            sr, sc_ = span_rects(cs, ce, tokens, n2h, hay, line_words)
            segs.append({"cs": cs, "ce": ce,
                         "f0": round((cs - sp[1]) / clen, 4),
                         "f1": round((ce - sp[1]) / clen, 4),
                         "rects": sr, "conf": round(sc_, 3), "page_only": None})
        chunks.append({"i": i, "char_start": sp[1], "char_end": sp[2],
                       "t_start": t_start, "t_end": t_end,
                       "rects": rects, "conf": round(conf, 3),
                       "page_only": None, "segs": segs})
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
    # segments fall back the same way, nearest rect-bearing sibling first,
    # then the chunk's own page_only
    for c in chunks:
        last = None
        for s in c["segs"]:
            if s["rects"]:
                last = s["rects"][0]["page"]
                if s["conf"] < CONF_PAGE_ONLY:
                    s["page_only"] = last
            else:
                s["page_only"] = last
        nxt = c["page_only"]
        for s in reversed(c["segs"]):
            if s["rects"]:
                nxt = s["rects"][0]["page"]
            elif s["page_only"] is None:
                s["page_only"] = nxt
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
