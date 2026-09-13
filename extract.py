#!/usr/bin/env python3
"""PDF -> speech-ready text for academic papers.

The hard part of a paper audiobook is not the TTS, it is that a two-column PDF
extracted naively yields interleaved columns, repeated running heads, inline
citations that are unlistenable, and a references section that is 30-40% of the
file and pure noise. This does the cleanup and reports statistics so quality is
checkable without reading the whole thing.
"""
import argparse
import collections
import json
import pathlib
import re
import sys

import pymupdf

ROOT = pathlib.Path(__file__).resolve().parent
PDFS, TEXT = ROOT/"pdfs", ROOT/"text"

# ---------- section boundaries ----------
REF_HEAD = re.compile(r"^\s*(references|bibliography|works\s+cited|literature\s+cited)\s*:?\s*$", re.I)
STOP_HEAD = re.compile(r"^\s*(acknowledg(e)?ments?|appendix|about\s+the\s+authors?|author\s+biograph)", re.I)
HEAD_NUM = re.compile(r"^\s*(\d+(\.\d+)*)\.?\s+([A-Z][^.]{2,80})\s*$")
HEAD_WORD = re.compile(r"^\s*(abstract|introduction|conclusions?|discussion|related\s+work|background|"
                       r"method(s|ology)?|results?|experiments?|evaluation|limitations?|future\s+work)\s*$", re.I)

# ---------- noise ----------
CITE_PAREN = re.compile(r"\((?:(?:see|e\.g\.|cf\.|also)\s+)?(?:[A-Z][A-Za-z'’\-]+"
                        r"(?:\s+(?:and|&|et\s+al\.?)\s*[A-Za-z'’\-]*)*,?\s*(?:19|20)\d\d[a-z]?"
                        r"(?:\s*[;,]\s*[^()]{0,60}?(?:19|20)\d\d[a-z]?)*)\)")
CITE_BRACK  = re.compile(r"\[\s*\d+(?:\s*[-,–]\s*\d+)*\s*\]")
CITE_NARR   = re.compile(r"\s*\((?:19|20)\d\d[a-z]?\)")      # "Simon (1969) argued" -> "Simon argued"
URL         = re.compile(r"(https?://\S+|www\.\S+|doi:\s*\S+|10\.\d{4,}/\S+)", re.I)
EMAIL       = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
CAPTION     = re.compile(r"^\s*(figure|fig\.?|table|algorithm|listing|eq\.?|equation)\s*\d+[.:）)]?\s", re.I)
PAGENUM     = re.compile(r"^\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*$")
BULLET      = re.compile(r"^\s*[•·▪◦‣–—*]\s*")
MATHY       = set("∑∏∫√≈≠≤≥±∞∈∉⊆⊂∪∩∀∃¬∧∨→←↔⇒⇐⇔αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ∂∇⊥∥⟨⟩⌈⌉⌊⌋")

# ---------- speech normalisation ----------
SPEECH = [
    (re.compile(r"\bet\s+al\.?"), "and colleagues"),
    (re.compile(r"\be\.\s?g\.,?"), "for example,"),
    (re.compile(r"\bi\.\s?e\.,?"), "that is,"),
    (re.compile(r"\bcf\.\s?"), "compare "),
    (re.compile(r"\bvs\.?\s"), "versus "),
    (re.compile(r"\betc\.(?=\s|$)"), "and so on."),
    (re.compile(r"\bw\.r\.t\.?"), "with respect to"),
    (re.compile(r"\bs\.t\.?"), "such that"),
    (re.compile(r"\bFigs?\.\s*"), "Figure "),
    (re.compile(r"\bTab\.\s*"), "Table "),
    (re.compile(r"\bSecs?\.\s*|§\s*"), "Section "),
    (re.compile(r"\bEqs?\.\s*"), "Equation "),
    (re.compile(r"\bApp\.\s*"), "Appendix "),
    (re.compile(r"\bapprox\.\s*|~(?=\d)"), "approximately "),
    (re.compile(r"\bal\.\s*"), "al. "),
    (re.compile(r"(?<=\d)\s*%"), " percent"),
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"(?<=\d)\s*[×x]\s*(?=\d)"), " by "),
    (re.compile(r"[“”„]"), '"'), (re.compile(r"[‘’‚]"), "'"),
    (re.compile(r"[–—]"), ", "), (re.compile(r"…"), "."),
    (re.compile(r"\bpp?\.\s*\d+[-–]?\d*"), ""),
]

def dehyphenate(t):
    return re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)

def running_heads(pages, thresh=0.5):
    """Lines that repeat across most pages are headers/footers, not content."""
    cnt = collections.Counter()
    for p in pages:
        ls = [ln.strip() for ln in p.splitlines() if ln.strip()]
        for ln in set(ls[:2] + ls[-2:]):
            if 3 < len(ln) < 120: cnt[ln] += 1
    n = max(1, len(pages))
    return {ln for ln, c in cnt.items() if c / n >= thresh and n > 2}

def mathy(line):
    if not line: return False
    sym = sum(ch in MATHY for ch in line)
    nonalpha = sum((not ch.isalnum()) and (not ch.isspace()) for ch in line)
    alpha = sum(ch.isalpha() for ch in line)
    return sym >= 2 or (alpha and nonalpha / max(1, len(line)) > 0.35) or (alpha < 3 and len(line) > 6)

def extract(pdf_path, stats):
    doc = pymupdf.open(pdf_path)
    stats["pages"] = doc.page_count
    pages = []
    for pg in doc:
        # KNOWN DEFECT -- see README "Extraction quality". `sort=True` orders
        # blocks vertically then horizontally across the WHOLE page, which on
        # some two-column layouts interleaves the columns line by line rather
        # than reading one column then the other. Measured over this corpus by
        # hyphen-rejoin rate (a line-final hyphen should be completed by the
        # next token): one dense two-column ACM paper scores 5.8% with sort=True
        # against 72.4% with sort=False, while the other six papers differ by
        # under 5 points either way. It is NOT simply "sort=False is better" --
        # it is layout-dependent, and changing it re-renders everything.
        # Left as-is deliberately; the fix is a decision, not a cleanup.
        pages.append(pg.get_text("text", sort=True))
    raw = "\n".join(pages)
    stats["raw_chars"] = len(raw)

    heads = running_heads(pages)
    stats["running_heads_removed"] = len(heads)

    body, cut_at, seen = [], None, 0
    for pi, page in enumerate(pages):
        page = dehyphenate(page)
        for line in page.splitlines():
            s = line.strip()
            if not s: body.append(""); continue
            if s in heads: continue
            if PAGENUM.match(s): continue
            # Absolute floor, not a page fraction: a 118-page paper puts References
            # on p9, so any percentage rule swallows the whole appendix.
            if (REF_HEAD.match(s) or STOP_HEAD.match(s)) and pi >= 2 and seen > 4000:
                cut_at = pi; break
            body.append(s); seen += len(s)
        if cut_at is not None: break
    stats["refs_cut_at_page"] = cut_at

    text = "\n".join(body)
    stats["after_refs_chars"] = len(text)

    kept, dropped_caption, dropped_math = [], 0, 0
    for line in text.splitlines():
        s = line.strip()
        if not s: kept.append(""); continue
        if CAPTION.match(s): dropped_caption += 1; continue
        if mathy(s): dropped_math += 1; continue
        kept.append(BULLET.sub("", s))
    stats["caption_lines_dropped"] = dropped_caption
    stats["math_lines_dropped"] = dropped_math
    return "\n".join(kept)


def table_like(p):
    """Table rows, code and trajectory dumps reflow into one huge pseudo-sentence."""
    toks = p.split()
    if len(toks) < 6: return False
    digits = sum(c.isdigit() for c in p) / max(1, len(p))
    alpha_toks = sum(t.isalpha() for t in toks) / len(toks)
    mean_len = sum(len(t) for t in toks) / len(toks)
    no_stop = p.count(".") + p.count("?") + p.count("!")
    return (digits > 0.14 or alpha_toks < 0.55 or mean_len < 3.0
            or (len(toks) > 120 and no_stop <= 1))

def to_speech(text, stats):
    n_paren = len(CITE_PAREN.findall(text)); n_brack = len(CITE_BRACK.findall(text))
    text = CITE_PAREN.sub("", text)
    text = CITE_BRACK.sub("", text)
    text = CITE_NARR.sub("", text)
    text = URL.sub("", text); text = EMAIL.sub("", text)
    stats["citations_stripped"] = n_paren + n_brack

    # reflow: join wrapped lines inside a paragraph, keep blank lines as breaks
    paras, buf = [], []
    for line in text.splitlines():
        if not line.strip():
            if buf: paras.append(" ".join(buf)); buf = []
        else:
            buf.append(line.strip())
    if buf: paras.append(" ".join(buf))

    out = []
    for p in paras:
        for rx, rep in SPEECH: p = rx.sub(rep, p)
        p = re.sub(r"\s+", " ", p).strip()
        p = re.sub(r"\s+([,.;:!?])", r"\1", p)
        p = re.sub(r"([,.;:])\1+", r"\1", p)
        p = re.sub(r"\(\s*\)", "", p)
        if len(p) < 25 and not p.endswith((".", "?", "!")):   # stray fragment
            continue
        if table_like(p):
            stats["table_paras_dropped"] = stats.get("table_paras_dropped", 0) + 1
            continue
        out.append(p)
    stats["paragraphs"] = len(out)
    return "\n\n".join(out)


def select_ids(papers, only):
    """Which papers --only refers to. An exact id wins outright; otherwise fall
    back to the old substring match, so `--only 01-cook` still works. Exactness
    matters now that a folder upload can create many ids sharing a prefix."""
    if not only:
        return {p["id"] for p in papers}
    exact = {p["id"] for p in papers if p["id"] == only}
    return exact or {p["id"] for p in papers if only in p["id"]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only"); ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    TEXT.mkdir(exist_ok=True)
    papers = json.loads((ROOT/"papers.json").read_text())["papers"]
    sel = select_ids(papers, a.only)
    rows = []
    for p in papers:
        if p["id"] not in sel: continue
        src = PDFS/f"{p['id']}.pdf"
        if not src.exists(): rows.append((p["id"], None)); continue
        st = {}
        body = to_speech(extract(src, st), st)
        header = (f"{p['title']}. By {p['authors']}. Published {p['year']} in {p['venue']}.\n\n")
        (TEXT/f"{p['id']}.txt").write_text(header + body)
        st["final_chars"] = len(body)
        st["words"] = len(body.split())
        st["est_minutes"] = round(st["words"] / 150, 1)
        sents = [s for s in re.split(r"(?<=[.!?])\s+", body) if s]
        st["sentences"] = len(sents)
        st["avg_sent_words"] = round(sum(len(s.split()) for s in sents) / max(1, len(sents)), 1)
        st["max_sent_words"] = max((len(s.split()) for s in sents), default=0)
        (TEXT/f"{p['id']}.meta.json").write_text(json.dumps(st, indent=1))
        rows.append((p["id"], st))
    print(f"{'paper':<36}{'pg':>4}{'raw kB':>8}{'kept kB':>8}{'keep%':>7}"
          f"{'cites':>7}{'words':>8}{'min':>7}{'avgS':>6}{'maxS':>6}")
    tot = 0
    for pid, st in rows:
        if st is None: print(f"{pid[:35]:<36}{'  -- no PDF (drop-in needed)':>50}"); continue
        keep = 100 * st["final_chars"] / max(1, st["raw_chars"])
        tot += st["est_minutes"]
        print(f"{pid[:35]:<36}{st['pages']:>4}{st['raw_chars']/1000:>8.1f}{st['final_chars']/1000:>8.1f}"
              f"{keep:>6.0f}%{st['citations_stripped']:>7}{st['words']:>8}{st['est_minutes']:>7.1f}"
              f"{st['avg_sent_words']:>6.1f}{st['max_sent_words']:>6}")
    print(f"\ntotal estimated audio: {tot/60:.1f} h")

if __name__ == "__main__": sys.exit(main())
