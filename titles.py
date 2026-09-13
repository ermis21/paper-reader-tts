#!/usr/bin/env python3
"""Find a paper's real title, so an upload called `2602.03249v2.pdf` is filed as
"Accordion-Thinking: Self-Regulated Step Summaries for Efficient and Readable LLM
Reasoning" rather than by whatever its file happened to be called.

Local and deterministic -- no network, no model; the same PDF always gives the
same answer. Two signals, both read off the PDF itself:

  1. Typesetting. The title is the biggest horizontal text in the upper part of
     page 1. Lines within 8% of that size, stacked less than a line apart, are
     one wrapped title. The rotated arXiv side stamp, bare section labels
     ("Abstract"), drop caps and superscript footnote marks are ignored.
  2. The document-info Title, trusted ONLY when its words are printed at that
     title size. Metadata is routinely junk -- this corpus alone has
     "final1.dvi", "PLME0208_696-701.indd" and "PII: 0364-0213(94)90007-8" --
     but when it checks out it spells the title better than a text layer does
     (OCR slips, ligatures, line-break hyphens), so it wins when it agrees.

ALL-CAPS titles (ICLR's small caps) are recased from how the paper's own first
two pages spell each word: "LORA: LOW-RANK ADAPTATION OF LARGE LAN- / GUAGE
MODELS" becomes "LoRA: Low-rank adaptation of large language models".

When neither signal is confident the answer is None and the upload keeps its
filename -- a wrong title is worse than an ugly one. Accuracy on this project's
corpus is in README, "Titles".

Runs as a SUBPROCESS of the web app, never inside it: parsing an uploaded PDF is
an attack on MuPDF (SECURITY.md), and a crash there must not take the server down.

    ./titles.py some.pdf other.pdf       # print what would be detected
    ./titles.py --retitle                # dry run over the workspace
    ./titles.py --retitle --apply        # write it
"""
import argparse
import collections
import json
import pathlib
import re
import sys
import unicodedata

TOP = 0.70          # title lives in the upper part of page 1 (a slide-deck cover puts it mid-page)
SAME_SIZE = 0.92    # one wrapped title can mix 17.0 and 17.2 pt lines
LINE_GAP = 0.9      # ... stacked less than this many title-heights apart
MAX_LINES = 4       # more lines than this at title size is body text, not a title
MIN_WORDS, MAX_CHARS = 2, 250
AGREE = 0.8         # share of a metadata title's words that must be printed at title size

JUNK_META = re.compile(
    r"\.(dvi|pdf|docx?|tex|indd|ps|eps|rtf|qxd|odt|pptx?)$"     # a file name: "final1.dvi"
    r"|^(microsoft\s+\w+\s+-|untitled|pii:|doi:|arxiv:)"        # producer and identifier noise
    r"|(\.\.\.|…)$",                                            # truncated by the producer
    re.I)
SITE_SUFFIX = re.compile(r"\s+[»|]\s+.*$")                       # "... » American Scientist"
LABEL = re.compile(r"^(abstract|article|open|preprint|research\s+article|letter|review|contents|"
                   r"original\s+(article|research)|technical\s+report)$", re.I)
BY_LINE = re.compile(r"^by\b", re.I)
TRAILING_MARKS = re.compile(r"[\s*∗†‡§¶]+$")


# ---------------------------------------------------------------- text helpers
def fold(s):
    return unicodedata.normalize("NFKC", s or "").casefold()


def words(s):
    return re.findall(r"[^\W_]+", fold(s))


def tidy(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,:;?!)])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    return TRAILING_MARKS.sub("", s).strip()


def _forms(text):
    """How the paper itself spells each word: folded word -> Counter of spellings."""
    seen = collections.defaultdict(collections.Counter)
    for w in re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text)):
        seen[fold(w)][w] += 1
    return seen


SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on",
         "onto", "or", "per", "the", "to", "via", "vs", "with"}


def _own(text):
    return collections.Counter(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text)))


def _is_caps(s):
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) >= 0.9 * len(letters)


def _spell(line, forms, own):
    """Respell capitals the way the paper does OUTSIDE its title.

    `own` counts the title's own words and is subtracted: a title set in small
    caps is no evidence that LAN or GUAGE are acronyms. In an ALL-CAPS line every
    word becomes Title Case, except the paper's inner-capital spellings (LoRA,
    DeepSeekMoE) and acronyms it never writes any other way (BLEU). In a
    mixed-case line only the inner-capital respelling applies: QLORA -> QLoRA."""
    caps = _is_caps(line)

    def one(m):
        tok = m.group(0)
        if any(c.isdigit() for c in tok) or not tok.isupper():
            return tok
        seen = {f: n - own[f] for f, n in (forms.get(fold(tok)) or {}).items() if n > own[f]}
        inner = [f for f in seen if not f.isupper() and any(c.isupper() for c in f[1:])]
        if inner and len(tok) > 2:
            return max(inner, key=seen.get)
        if not caps or (len(tok) <= 5 and seen and all(f.isupper() for f in seen)):
            return tok
        return tok.lower() if tok.lower() in SMALL else tok[:1] + tok[1:].lower()

    return re.sub(r"[^\W_]+", one, line)


def _capitalise(title):
    """After Title-Casing: the first word, and the first after a colon, even if small."""
    t = re.sub(r"^([^A-Za-z]*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), title)
    return re.sub(r"([:?!]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)


def _join(parts, vocab):
    """Rejoin a wrapped title. A line-end hyphen is dropped only when the paper
    itself uses the joined word ("Lan-" + "Guage"); "Self-" + "Supervised" keeps it."""
    out = parts[0].strip()
    for nxt in (p.strip() for p in parts[1:]):
        head, tail = re.search(r"([^\W\d_]+)-$", out), re.match(r"[^\W\d_]+", nxt)
        if head and tail and fold(head.group(1) + tail.group(0)) in vocab:
            lower = any(c.islower() for c in head.group(1))
            out = out[:-1] + (nxt[:1].lower() + nxt[1:] if lower else nxt)
        elif head and tail:
            out += nxt
        else:
            out = f"{out} {nxt}"
    return out


# ---------------------------------------------------------------- page layout
def _lines(page):
    """Horizontal text lines of a page: text, size and bbox, in points."""
    import pymupdf
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    out = []
    for block in page.get_text("dict", flags=flags)["blocks"]:
        for ln in block.get("lines", []):
            if abs(ln["dir"][1]) > 0.1:          # rotated: the arXiv side stamp, margin notes
                continue
            inked = [s for s in ln["spans"] if s["text"].strip()]
            if not inked:
                continue
            big = max(s["size"] for s in inked)
            # superscripts ride on the title line: footnote marks, affiliation digits
            text = "".join(s["text"] for s in ln["spans"] if s["size"] >= 0.7 * big or not s["text"].strip())
            x0, y0, x1, y1 = ln["bbox"]
            out.append({"text": text, "size": big, "x0": x0, "x1": x1, "y0": y0, "y1": y1})
    if out and max(ln["size"] for ln in out) < 3:  # TeX Type 3 fonts can report a 0.1 pt size
        for ln in out:
            ln["size"] = ln["y1"] - ln["y0"]
    return out


def _rows(lines):
    """Merge segments sharing a baseline: TeX kerning emits "Lifew" and "orld" apart."""
    rows = []
    for ln in sorted(lines, key=lambda d: (d["y0"], d["x0"])):
        for r in rows:
            if (abs(r["y1"] - ln["y1"]) < 0.3 * r["size"]
                    and min(r["size"], ln["size"]) >= SAME_SIZE * max(r["size"], ln["size"])):
                r["segs"].append(ln)
                break
        else:
            rows.append({"size": ln["size"], "y0": ln["y0"], "y1": ln["y1"], "segs": [ln]})
    for r in rows:
        segs, text = sorted(r["segs"], key=lambda d: d["x0"]), ""
        for i, s in enumerate(segs):
            gap = s["x0"] - segs[i - 1]["x1"] if i else 0
            text += (" " if i and gap > 0.15 * r["size"] else "") + s["text"]
        r["text"], r["size"] = text, max(s["size"] for s in segs)
    return rows


def _layout(page, vocab, forms):
    """(title, words printed at title size) from the typesetting of page 1."""
    rows = [r for r in _rows(_lines(page))
            if r["y0"] < TOP * page.rect.height and r["size"] >= 3
            and sum(c.isalpha() for c in r["text"]) >= 3          # drop caps, "1.", stray marks
            and not LABEL.match(r["text"].strip()) and "@" not in r["text"]]
    if not rows:
        return "", set()
    big = max(r["size"] for r in rows)
    tall = sorted((r for r in rows if r["size"] >= SAME_SIZE * big), key=lambda r: r["y0"])
    groups = []
    for r in tall:
        if (groups and r["y0"] - groups[-1][-1]["y1"] <= LINE_GAP * big
                and not BY_LINE.match(r["text"].strip())):
            groups[-1].append(r)
        else:
            groups.append([r])
    printed = set(words(" ".join(r["text"] for r in tall)))
    groups = [g for g in groups if len(g) <= MAX_LINES]
    if not groups:
        return "", printed
    # The most ink wins, the topmost on a tie: a journal banner set at title size
    # ("Future Generation Computer Systems") is shorter than the title under it.
    best = max(groups, key=lambda g: sum(c.isalpha() for r in g for c in r["text"]))
    if any(s["x1"] > page.rect.width + 1 for r in best for s in r["segs"]):
        # It runs off the page, so the text layer itself is cut ("...evolution of
        # ontologies t"). An incomplete title is a wrong title: decline, don't guess.
        return "", printed
    own = _own(" ".join(r["text"] for r in best))
    title = tidy(_join([_spell(r["text"], forms, own) for r in best], vocab))
    return (_capitalise(title) if any(_is_caps(r["text"]) for r in best) else title), printed


def _verdict(title, source):
    if not title or len(title) > MAX_CHARS or len(words(title)) < MIN_WORDS:
        return None
    ink = [c for c in title if not c.isspace()]
    if sum(c.isalpha() for c in ink) < 0.6 * len(ink):
        return None
    return {"title": title, "source": source}


def detect(path):
    """{"title": ..., "source": "metadata"|"layout"} for one PDF, or None."""
    import pymupdf
    with pymupdf.open(path) as doc:
        if doc.needs_pass or doc.page_count == 0:
            return None
        page = doc[0]
        first = page.get_text("text")
        both = first + "\n" + (doc[1].get_text("text") if doc.page_count > 1 else "")
        forms = _forms(both)
        lay, printed = _layout(page, set(words(both)), forms)
        meta = SITE_SUFFIX.sub("", tidy(str((doc.metadata or {}).get("title") or "")))
    if meta and not JUNK_META.search(meta):
        mw = [w for w in words(meta) if len(w) > 2] or words(meta)
        against = printed or set(words(first))
        if mw and sum(w in against for w in mw) >= AGREE * len(mw):
            caps = _is_caps(meta)
            meta = _spell(meta, forms, _own(meta))
            meta = _capitalise(meta) if caps else meta
            partial = lay and set(words(meta)) < set(words(lay))   # metadata held only part of it
            hit = None if partial else _verdict(meta, "metadata")
            if hit:
                return hit
    return _verdict(lay, "layout")


# ---------------------------------------------------------------- workspace
def retitle(only=(), apply=False, retry=False):
    """Title the workspace papers that are still named after their upload."""
    import workspace as ws
    ws.init()
    wanted = {"pending", "filename"} if retry else {"pending"}
    rows = [r for r in ws.list_items() if r["kind"] == "paper" and r.get("title_source") in wanted
            and (not only or r["id"] in only)]
    found = 0
    for r in rows:
        pdf = ws.PDFS / f"{r['id']}.pdf"
        if not pdf.exists():
            print(f"  {'-':<9} {r['id'][:44]:<45} no PDF yet, left pending")
            continue
        try:
            hit = detect(pdf)
        except Exception as e:           # a broken PDF keeps its filename; it does not stop the run
            print(f"  {'error':<9} {r['id'][:44]:<45} {type(e).__name__}: {str(e)[:80]}")
            hit = None
        found += bool(hit)
        print(f"  {hit['source'] if hit else 'none':<9} {r['id'][:44]:<45} "
              f"{r['title'][:40]!r} -> {hit['title'] if hit else '(keeps its filename)'!r}")
        if apply:
            ws.set_detected_title(r["id"], hit["title"] if hit else None, hit["source"] if hit else None)
    print(f"\n{found} of {len(rows)} titled" + ("" if apply else "  (dry run -- add --apply to write)"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="read a paper's title off the PDF itself")
    ap.add_argument("pdfs", nargs="*", type=pathlib.Path, help="print the title detected for each PDF")
    ap.add_argument("--json", action="store_true", help="one JSON object per PDF")
    ap.add_argument("--retitle", action="store_true",
                    help="title workspace papers still named after their upload filename (dry run)")
    ap.add_argument("--apply", action="store_true", help="with --retitle: write the titles")
    ap.add_argument("--only", nargs="+", metavar="ID", help="with --retitle: just these ids")
    ap.add_argument("--retry", action="store_true",
                    help="with --retitle: also retry papers where nothing was found last time")
    a = ap.parse_args()
    if a.retitle:
        return retitle(set(a.only or ()), a.apply, a.retry)
    if not a.pdfs:
        ap.error("give one or more PDFs, or --retitle")
    for p in a.pdfs:
        try:
            hit = detect(p)
        except Exception as e:
            print(f"{p}: {type(e).__name__}: {e}", file=sys.stderr)
            hit = None
        if a.json:
            print(json.dumps({"file": str(p), **(hit or {"title": None, "source": None})}))
        else:
            print(f"{(hit or {}).get('source') or 'none':<9} "
                  f"{(hit or {}).get('title') or '(nothing trustworthy: keeps its filename)'}   [{p.name}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
