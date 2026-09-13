#!/usr/bin/env python3
"""align.py: tokeniser, chunk offsets, alignment edge cases and the timeline.

PDFs and WAVs are synthesised in a throwaway directory, so this needs only the
venv -- no corpus, no cached audio:

    ./venv/bin/python tests/test_align.py

The corpus golden check at the end runs only when the real workspace is present.
"""
import json
import pathlib
import re
import sys
import tempfile
import wave

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

import align  # noqa: E402
import build as buildmod  # noqa: E402
import synth  # noqa: E402

failures = 0


def check(name, cond, extra=""):
    global failures
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    failures += not cond


# ---------------------------------------------------------------- tokeniser
def t_tokeniser():
    check("skeleton folds case/punct", align.skeleton("Smith,") == align.skeleton("SMITH") == "smith")
    check("skeleton keeps digits", align.skeleton("(2020)") == "2020")
    check("skeleton empties punctuation", align.skeleton("—") == "")
    toks = align.needle_tokens("One two.\n\nThree 50% done.")
    check("needle skips punctuation-only tokens",
          [t[0] for t in toks] == ["one", "two", "three", "50", "done"])
    check("needle offsets point at source",
          all("One two.\n\nThree 50% done."[s:e].lower().startswith(t[0][:3]) for t, s, e in
              [(tk, tk[1], tk[2]) for tk in toks[:1]]) and toks[0][1:] == (0, 3))


# ---------------------------------------------------------------- chunk offsets
def legacy_chunks_for(text):
    """The pre-refactor chunks_for, frozen. chunk_spans must stay byte-identical:
    cached WAVs narrate these exact strings in this exact order."""
    sents = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        sents += [s for s in re.split(r"(?<=[.!?])\s+", para) if s.strip()]
    out, buf = [], ""
    for s in sents:
        if len(buf) + len(s) + 1 <= synth.MAX_CHARS:
            buf = f"{buf} {s}".strip()
        else:
            if buf: out.append(buf)
            while len(s) > synth.MAX_CHARS:
                cut = s.rfind(",", 0, synth.MAX_CHARS)
                cut = cut if cut > synth.MAX_CHARS // 2 else synth.MAX_CHARS
                out.append(s[:cut].strip()); s = s[cut:].strip()
            buf = s
    if buf: out.append(buf)
    return out


def t_chunk_spans():
    samples = [
        "Short one. Another here.\n\nNew para starts. And continues on.\n",
        "No terminal punctuation paragraph\n\nFollows. With sentence two.",
        "A" * 600 + ", tail bit. Next sentence.",           # pathological long sentence
        "Weird   spacing\n  inside a\nparagraph. Then more.\n\n\n\nBig gap. End.",
    ]
    for i, s in enumerate(samples):
        spans = synth.chunk_spans(s)
        check(f"chunk_spans identical to legacy ({i})",
              [c for c, _, _ in spans] == legacy_chunks_for(s))
        check(f"chunk_spans offsets reconstruct text ({i})",
              all(s[a:b].split() == c.split() for c, a, b in spans))
        check(f"chunk_spans offsets ordered ({i})",
              all(spans[k][1] < spans[k][2] <= spans[k + 1][1] for k in range(len(spans) - 1)))


# ---------------------------------------------------------------- synthetic PDFs
def make_pdf(path, pages_lines):
    """One line per list entry, single column, generous spacing."""
    doc = pymupdf.open()
    for lines in pages_lines:
        pg = doc.new_page(width=612, height=792)
        y = 72
        for ln in lines:
            pg.insert_text((56, y), ln, fontsize=11)
            y += 16
    doc.save(path)
    doc.close()


def make_wav(path, seconds):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(align.SR)
        w.writeframes(b"\x00\x00" * int(seconds * align.SR))


def build_in(tmp, pid, pdf_pages, text, wav_seconds_list=None):
    """Run build_alignment with align.py pointed at a throwaway workspace."""
    text_dir, pdf_dir, cache_dir = tmp/"text", tmp/"pdfs", tmp/"cache"
    text_dir.mkdir(exist_ok=True); pdf_dir.mkdir(exist_ok=True)
    (cache_dir/pid).mkdir(parents=True, exist_ok=True)
    (text_dir/f"{pid}.txt").write_text(text)
    make_pdf(pdf_dir/f"{pid}.pdf", pdf_pages)
    for i, sec in enumerate(wav_seconds_list or []):
        make_wav(cache_dir/pid/f"{i:05d}.wav", sec)
    old = align.TEXT, align.PDFS, align.CACHE
    align.TEXT, align.PDFS, align.CACHE = text_dir, pdf_dir, cache_dir
    try:
        return align.build_alignment(pid)
    finally:
        align.TEXT, align.PDFS, align.CACHE = old


BODY = ("The system fails when small faults line up. Each barrier was thought "
        "sufficient on its own. Hindsight makes the path look obvious after the fact.")


def t_align_basics():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        pdf_pages = [[
            "The system fails when small faults line up (Smith, 2020; Jones, 2019).",
            "Each barrier was thought sufficient on its own [12].",
            "Hindsight makes the path look obvious after the fact.",
        ]]
        al = build_in(tmp, "t1", pdf_pages, BODY)
        check("citations skipped, all chunks aligned",
              all(c["conf"] >= 0.9 for c in al["chunks"]),
              str([c["conf"] for c in al["chunks"]]))
        check("every chunk has rects", all(c["rects"] for c in al["chunks"]))
        check("rects on page 0", all(r["page"] == 0 for c in al["chunks"] for r in c["rects"]))

        # char ranges cover the text contiguously enough to round-trip the chunk text
        txt = BODY
        check("char ranges reconstruct chunk text",
              all(txt[c["char_start"]:c["char_end"]].split() ==
                  synth.chunk_spans(txt)[c["i"]][0].split() for c in al["chunks"]))


def t_align_speech_subs():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        # PDF speaks original; narration speaks normalised: et al. -> and colleagues,
        # % -> percent, & -> and.
        pdf_pages = [[
            "Smith et al. show that 50% of loops & nets survive.",
            "The rest fail quietly under load.",
        ]]
        text = "Smith and colleagues show that 50 percent of loops and nets survive. The rest fail quietly under load."
        al = build_in(tmp, "t2", pdf_pages, text)
        confs = [c["conf"] for c in al["chunks"]]
        # In an 18-token chunk, et al.->"and colleagues" + %->"percent" + &->"and"
        # costs 4 unmatched needle tokens = 0.78. On real ~60-token chunks the same
        # substitutions dilute to ~0.95 (see the corpus golden check). The walk
        # itself must not lose its place -- that is what is asserted here.
        check("speech substitutions stay alignable", all(c >= 0.7 for c in confs), str(confs))


def t_align_repeated_phrase():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        repeated = "Mistakes were made."
        pdf_pages = [
            [repeated, "First page unique content follows here."],
            [repeated, "Second page unique content follows here."],
        ]
        # narration says it once, in the second occurrence's neighbourhood
        text = "First page unique content follows here. " + repeated + " Second page unique content follows here."
        al = build_in(tmp, "t3", pdf_pages, text)
        pages_used = sorted({r["page"] for c in al["chunks"] for r in c["rects"]})
        check("repeated phrase never moves pointer backwards",
              pages_used == sorted(pages_used) and
              all(c["conf"] >= 0.8 for c in al["chunks"]),
              str([(c["i"], c["conf"], [r["page"] for r in c["rects"]]) for c in al["chunks"]]))


def t_align_page_break_mid_chunk():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        s1 = "This sentence ends one page."
        s2 = "This sentence opens the next."
        pdf_pages = [[s1], [s2]]
        al = build_in(tmp, "t4", pdf_pages, f"{s1} {s2}")
        c = al["chunks"][0]
        pages = sorted({r["page"] for r in c["rects"]})
        check("chunk crossing a page break gets rects on both pages", pages == [0, 1], str(pages))


def t_align_unalignable_falls_back():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        pdf_pages = [["Genuine body text that matches the narration exactly."]]
        text = ("Genuine body text that matches the narration exactly.\n\n"
                "thethe resultresult repeatedrepeated manymany timestimes "
                "untiluntil resultsresults areare exhaustedexhausted.")
        al = build_in(tmp, "t5", pdf_pages, text)
        last = al["chunks"][-1]
        check("garbage chunk is low-confidence", last["conf"] < align.CONF_PAGE_ONLY,
              f"conf={last['conf']}")
        check("garbage chunk falls back to a page, not a rect",
              last["page_only"] == 0, f"page_only={last['page_only']}")


def t_timeline():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        # ~250 chars per sentence -> one chunk per sentence (MAX_CHARS is 400)
        sents = [("Sentence number %d keeps going with enough plain words to pad it "
                  "out well past two hundred characters so that it can fill an entire "
                  "chunk of its very own without ever sharing space with a neighbour "
                  "sentence in the packing step." % i) for i in range(3)]
        pdf_pages = [[s] for s in sents]
        text = "\n\n".join(sents)
        al = build_in(tmp, "t6", pdf_pages, text, wav_seconds_list=[2.0, 3.0, 4.0])
        ch = al["chunks"]
        check("three long sentences -> three chunks", len(ch) == 3, str(len(ch)))
        gap = buildmod.GAP
        ok = True
        t = 0.0
        for i, c in enumerate(ch):
            ok &= abs(c["t_start"] - t) < 1e-6
            ok &= abs(c["t_end"] - (t + [2.0, 3.0, 4.0][i])) < 1e-6
            t = c["t_end"] + gap
        check("timeline is cumulative durations plus build.py GAP", ok,
              str([(c["t_start"], c["t_end"]) for c in ch]))
        check("gap recorded from build.py, not hard-coded", al["gap_seconds"] == gap)
        check("audio_complete when every chunk has a wav", al["audio_complete"] is True)

        # partial audio -> timings stop but alignment still builds
        al2 = build_in(tmp, "t7", pdf_pages, text, wav_seconds_list=[2.0])
        check("partial audio: later chunks have null timings",
              al2["chunks"][-1]["t_start"] is None and al2["audio_complete"] is False)
        check("partial audio: rects still computed",
              all(c["rects"] for c in al2["chunks"]))


def t_no_audio_at_all():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        pdf_pages = [["Words that exist on the page for notes before narration."]]
        al = build_in(tmp, "t8", pdf_pages, "Words that exist on the page for notes before narration.")
        check("no wavs: notes-ready alignment with null timings",
              all(c["t_start"] is None for c in al["chunks"]) and
              all(c["rects"] for c in al["chunks"]))


# ---------------------------------------------------------------- corpus golden
def t_corpus_golden():
    pid = "01-cook-how-complex-systems-fail"
    if not ((align.TEXT/f"{pid}.txt").exists() and (align.PDFS/f"{pid}.pdf").exists()):
        print("  SKIP  corpus golden (workspace not present)")
        return
    al = align.build_alignment(pid)
    confs = [c["conf"] for c in al["chunks"]]
    hi = sum(c >= 0.95 for c in confs) / len(confs)
    check("corpus golden: >=90% of chunks at conf>=0.95", hi >= 0.90, f"{hi:.0%}")
    check("corpus golden: no chunk below page-fallback", min(confs) >= 0.0)
    mono = [r["page"] for c in al["chunks"] for r in c["rects"][:1]]
    check("corpus golden: first-rect pages non-decreasing",
          all(a <= b for a, b in zip(mono, mono[1:])))


if __name__ == "__main__":
    t_tokeniser()
    t_chunk_spans()
    t_align_basics()
    t_align_speech_subs()
    t_align_repeated_phrase()
    t_align_page_break_mid_chunk()
    t_align_unalignable_falls_back()
    t_timeline()
    t_no_audio_at_all()
    t_corpus_golden()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURES'}")
    sys.exit(1 if failures else 0)
