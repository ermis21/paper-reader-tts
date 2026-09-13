#!/usr/bin/env python3
"""Audio quality linter for the chunk cache.

Two tiers, deliberately.

TIER 1 -- deterministic, no ML, every chunk. Reads `text/<id>.txt`, re-derives
the chunk boundaries with `synth.chunks_for` (so chunk N here is exactly the
chunk N that was narrated), and measures the WAV beside it: duration against a
duration *predicted from the source characters*, chars-per-second, peak,
clipping, DC offset, RMS, leading/trailing/longest-internal silence, unreadable
or empty files, and gaps in the index sequence. It also measures the *source*:
maths-symbol density, digit density and the fraction of tokens a speech
synthesiser can actually pronounce. That last group matters more than it looks
-- see below.

TIER 2 -- ASR round-trip on what Tier 1 flags (`--full` sweeps everything).
faster-whisper `tiny.en`, int8, CPU. `asr_words / src_words` is the headline
number: a clean chunk sits at 1.00. A proper word-level alignment (difflib over
word sequences) splits that ratio into insertions, deletions and substitutions,
so "the audio says more than the text" (repetition) is distinguishable from
"the audio says less" (truncation), and a repeated-4-gram coverage figure
confirms repetition from the transcript alone.

WHAT THIS FOUND, and why there is a seventh class
-------------------------------------------------
The task described two failure modes, repetition (asr/src ~= 2.5) and
truncation (~= 0.61). Both are real and reproduced here. Their *cause*,
however, is not the synthesiser losing its place: it is that `extract.py`
leaks maths and table rows into the text, and eSpeak, having no pronunciation
for U+1D70B MATHEMATICAL ITALIC SMALL PI, reads the codepoint out loud. The
ASR transcript of deepseekmath-grpo/00081 literally begins

    "Letter 1D70, Letter 1D703. Letter 1D Action. Letter 1Dpor5, ..."

which is what 2.58x word inflation sounds like, and is what a listener hears
as "the second half repeats each word twice". So chunks whose source is
unspeakable are classified `unspeakable`, not `repetition`: the two need
different repairs and conflating them sends you to the wrong one.

Classes: ok | unspeakable | repetition | truncation | silence | level |
missing | duration (Tier-1 duration anomaly that Tier 2 has not adjudicated).

Relationship to `.suspect`
--------------------------
`synth.py` writes `cache/<id>/NNNNN.suspect` when chars-per-second falls
outside [8, 32]. That rule is reproduced here verbatim as one Tier-1 check, so
this linter's flag set is a strict superset of it, and `--report` shows the
agreement. Nothing here writes or deletes a `.suspect` marker except `--fix`,
which removes the marker of a chunk it repaired and re-verified -- the marker
asserts a duration anomaly that no longer holds. `/api/library` keeps counting
`.suspect` exactly as before.

CPU only, always: this sets CUDA_VISIBLE_DEVICES="" before importing anything,
because both GPUs on this host belong to a vLLM server.
"""
import argparse
import collections
import difflib
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import wave
from concurrent.futures import ProcessPoolExecutor

os.environ["CUDA_VISIBLE_DEVICES"] = ""      # before torch/ctranslate2 import

import numpy as np                                              # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from synth import CPS_HI, CPS_LO, SR, chunks_for, write_wav     # noqa: E402

TEXT, CACHE, OUT, LINT = ROOT / "text", ROOT / "cache", ROOT / "out", ROOT / "lint"
BAK_DIR = ".lintbak"          # cache/<id>/.lintbak/NNNNN.wav -- never *.wav at top level

# ---------------------------------------------------------------- thresholds
# Every number here is a decision. They are set from the corpus distribution
# printed by --calibrate, not guessed; see README-style notes in the report.
DUR_HI, DUR_LO = 1.30, 0.72   # measured / predicted-from-characters
SIL_FRAC_HI = 0.60            # fraction of the chunk below the silence floor
SIL_GAP_HI = 2.50             # longest internal silence, seconds
SIL_EDGE_HI = 2.00            # leading or trailing silence, seconds
RMS_DB_LO, RMS_DB_HI = -34.0, -8.0
CLIP_FRAC_HI = 5e-4
DC_HI = 0.02
MATH_FRAC_HI = 0.015          # share of characters that are maths/greek/astral
DIGIT_FRAC_HI = 0.12
SPEAKABLE_LO = 0.80           # share of whitespace tokens a TTS can pronounce
# Tier 2
RATIO_REP, RATIO_TRUNC = 1.35, 0.75
INS_RATE_REP, DEL_RATE_TRUNC = 0.30, 0.25
DUP_COV_REP = 0.20
CODEPOINT_HITS = 1            # "letter 1d70b" in a transcript proves codepoint speech.
                              # Measured over all 8478 chunks: 155 carry at least
                              # one, and exactly one of those has a source with no
                              # maths, no ligature and no unpronounceable token --
                              # and that one is a true positive too. So one hit is
                              # enough; there is no benign way to produce it.
# "the audio agrees with its text" window, from the ratio distribution over the
# 4235 chunks whose source is provably clean: p1 = 0.945, p99 = 1.208,
# p99.5 = 1.264. tiny.en's own error is what sets the width, not the corpus.
AGREE_LO, AGREE_HI = 0.85, 1.30
MAX_TOKEN_LEN = 40            # no English word is this long; a 250-char token is
                              # a table header that lost its spaces
# repair
FIX_MIN_WORDS = 3             # cleaned text below this is not worth synthesising

CLASSES = ["ok", "unspeakable", "repetition", "truncation", "silence", "level",
           "missing", "duration"]
SEVERITY_W = {"unspeakable": 3, "repetition": 3, "truncation": 3, "missing": 3,
              "silence": 2, "duration": 2, "level": 1, "ok": 0}


# ---------------------------------------------------------------- text tools
MATH_RANGES = [(0x0370, 0x03FF), (0x2100, 0x214F), (0x2190, 0x21FF),
               (0x2200, 0x22FF), (0x2300, 0x23FF), (0x25A0, 0x25FF),
               (0x27C0, 0x27EF), (0x2980, 0x29FF), (0x2A00, 0x2AFF),
               (0x1D400, 0x1D7FF)]
LIG_RANGE = (0xFB00, 0xFB4F)


def _in(cp, ranges):
    return any(lo <= cp <= hi for lo, hi in ranges)


def fold(text):
    """NFKD-fold, dropping combining marks.

    This is a pure improvement on its own: it turns the ligatures the PDF
    carries (U+FB01 'fi', as in "Ef<fi>cientNet") into plain letters that
    eSpeak can say, and collapses MATHEMATICAL ITALIC SMALL PI to a plain
    greek pi rather than leaving a codepoint eSpeak spells out.
    """
    out = unicodedata.normalize("NFKD", text)
    return "".join(c for c in out if not unicodedata.combining(c))


def _exotic(ch):
    """A character eSpeak may have no pronunciation for.

    Not simply "non-ASCII". The corpus has 2711 U+FB01 LATIN SMALL LIGATURE FI
    and they are almost all spoken correctly, so a ligature -- anything that is
    a *letter* and folds to plain ASCII -- is not exotic. A maths-alphanumeric
    is, even though it folds to a plain letter, because it is a variable name
    and not a word. Everything else non-ASCII that is not a foldable letter
    (greek, operators, primes, modifier letters, currency) is exotic.
    """
    o = ord(ch)
    if o < 128:
        return False
    if _in(o, MATHVAR_RANGES):
        return True
    f = fold(ch)
    return not (f and all(ord(c) < 128 for c in f)
                and unicodedata.category(ch).startswith("L"))


def _token_speakable(tok):
    """Can a synthesiser make a word of this token?"""
    t = tok.strip(".,;:!?()'\"")
    if not t:
        return True                                   # bare punctuation
    if any(_in(ord(c), MATH_RANGES) or _in(ord(c), [LIG_RANGE]) for c in t):
        return False
    if any(ord(c) > 0x2000 for c in t):
        return False
    alpha = sum(c.isalpha() for c in t)
    if alpha == 0:
        return bool(re.fullmatch(r"[-+]?[\d.,%/]+", t))   # a number is speakable
    if alpha == 1 and len(t) == 1 and t.lower() not in "ai":
        return False                                  # a lone letter is spelled out
    if alpha / len(t) < 0.5 and len(t) > 3:
        return False                                  # "y1:t-1", "p(yt|x)"
    return True


def text_metrics(chunk):
    n = max(1, len(chunk))
    math_n = sum(_in(ord(c), MATH_RANGES) for c in chunk)
    lig_n = sum(_in(ord(c), [LIG_RANGE]) for c in chunk)
    dig_n = sum(c.isdigit() for c in chunk)
    toks = chunk.split()
    good = sum(_token_speakable(t) for t in toks)
    return {"chars": len(chunk), "words": len(toks),
            "math_frac": round(math_n / n, 4), "lig_frac": round(lig_n / n, 4),
            "digit_frac": round(dig_n / n, 4),
            "max_tok": max((len(t) for t in toks), default=0),
            "exotic_n": sum(_exotic(c) for c in chunk),
            "speakable_frac": round(good / max(1, len(toks)), 4)}


REPAIR_OPS = set("<>|=^_\\{}[]~:")
ALLOWED_PUNCT = set("-.%,'/")     # inside a token these are speakable: "GPT-3.5"
# Split for the repair path only. A MATHEMATICAL ITALIC CAPITAL K folds to a
# perfectly ordinary "K", so it has to be caught on the RAW token; a ligature
# folds to "fi" and must NOT be caught at all, or the repair deletes English
# words -- this corpus has 172 occurrences of "find" spelt with U+FB01.
MATHVAR_RANGES = [(0x1D400, 0x1D7FF)]
SYMBOL_RANGES = [(0x0370, 0x03FF), (0x2100, 0x214F), (0x2190, 0x21FF),
                 (0x2200, 0x22FF), (0x2300, 0x23FF), (0x25A0, 0x25FF),
                 (0x27C0, 0x27EF), (0x2980, 0x29FF), (0x2A00, 0x2AFF)]


def _token_repairable(tok):
    """Stricter than `_token_speakable`, and used ONLY by the repair rewrite.

    The two have different costs and so different tolerances. In detection a
    false drop shifts a ratio; in repair a false keep is seconds of spelled-out
    nonsense in the listener's ear and a false drop is one lost token.
    """
    raw = tok.strip(".,;:!?()'\"")
    if not raw:
        return False
    if any(_in(ord(c), MATHVAR_RANGES) for c in raw):
        return False                                  # a maths-italic variable
    t = fold(raw)                                     # ligatures become letters here
    if not t or any(_in(ord(c), SYMBOL_RANGES) or ord(c) > 0x2000 for c in t):
        return False
    if any(c in REPAIR_OPS for c in t):
        return False                                  # "o<t)", "t=1", "p(yt|y<t"
    alpha = sum(c.isalpha() for c in t)
    if alpha == 0:
        return bool(re.fullmatch(r"[-+]?[\d.,%]+", t))
    if alpha == len(t) and len(t) > 1 and re.fullmatch(r"(?i)(.)\1+", t):
        return False                                  # "AA", "rr", "KKKK" -- doubled glyphs
    junk = sum(1 for c in t if not c.isalnum() and c not in ALLOWED_PUNCT)
    if junk / len(t) > 0.25:
        return False
    # Digits are speakable, so the test is symbol density and not letter
    # density: "16B" and "GPT-3.5" survive, "p(yt|y<t" does not. A lone ASCII
    # letter also survives -- eSpeak says a variable's letter name correctly.
    return True


def clean_for_speech(chunk):
    """The repair transform: drop tokens no synthesiser can say, then fold.

    The order matters. Folding first would turn MATHEMATICAL ITALIC CAPITAL K
    into a plain "K", hiding from the filter exactly the thing the filter is
    for -- and "KKKK" is spelled out just as badly as the astral original. So
    the filter runs on the raw token and only the survivors are NFKD-folded,
    which is what repairs the PDF's fi/fl ligatures.

    Formula fragments and table rows are not listenable in *any* rendering --
    the choice is not "read the maths well" but "read the maths as a stream of
    codepoint names, or not at all". This drops them and keeps the prose that
    was packed into the same chunk.
    """
    kept = [fold(tok) for tok in chunk.split() if _token_repairable(tok)]
    s = " ".join(kept)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"([,.;:])\1+", r"\1", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"^[\s,.;:]+", "", s)
    if s and s[-1] not in ".!?":
        s += "."
    return s


_ONES = "zero one two three four five six seven eight nine ten eleven twelve " \
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _int_words(n):
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + _int_words(n % 100))
    for div, name in ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= div:
            return _int_words(n // div) + " " + name + ("" if n % div == 0 else " " + _int_words(n % div))
    return str(n)


def _num_words(tok):
    """Spoken form of a number, well enough to count its words."""
    m = re.fullmatch(r"([-+]?)(\d[\d,]*)(?:\.(\d+))?", tok)
    if not m:
        return tok
    whole = int(m.group(2).replace(",", ""))
    frac = m.group(3)
    if frac is None and 1100 <= whole <= 2099 and whole % 100 != 0:
        head, tail = divmod(whole, 100)          # "1998" -> "nineteen ninety eight"
        return f"{_int_words(head)} {_int_words(tail) if tail >= 10 else 'oh ' + _int_words(tail)}"
    out = _int_words(whole) if whole < 10 ** 12 else " ".join(_ONES[int(d)] for d in str(whole))
    if frac:
        out += " point " + " ".join(_ONES[int(d)] for d in frac)
    return out


def words_for_compare(text, expand_numbers=True):
    t = fold(text).lower()
    t = t.replace("-", " ").replace("/", " ")
    t = re.sub(r"[^a-z0-9'.\s]", " ", t)
    out = []
    for tok in t.split():
        tok = tok.strip(".'")
        if not tok:
            continue
        if expand_numbers and re.fullmatch(r"\d[\d,]*(\.\d+)?", tok):
            out += _num_words(tok).split()
        else:
            out.append(tok)
    return out


# ---------------------------------------------------------------- audio tools
def wav_metrics(path):
    """Everything Tier 1 measures about one rendered chunk."""
    try:
        size = path.stat().st_size
    except OSError:
        return {"error": "absent"}
    if size < 45:
        return {"error": "empty", "bytes": size}
    try:
        with wave.open(str(path), "rb") as w:
            nch, sw, sr, nframes = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(nframes)
    except Exception as e:
        return {"error": f"unreadable: {type(e).__name__}", "bytes": size}
    declared = nframes * nch * sw
    if len(raw) < declared:
        return {"error": "truncated", "bytes": size, "have": len(raw), "want": declared}
    if sw != 2:
        return {"error": f"sampwidth {sw}", "bytes": size}
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    n = len(a)
    if n == 0:
        return {"error": "zero frames", "bytes": size, "secs": 0.0}

    secs = n / sr
    peak = float(np.abs(a).max())
    rms = float(np.sqrt(np.mean(a * a)))
    dc = float(a.mean())
    clip = float(np.mean(np.abs(a) >= 32700 / 32767.0))

    hop = max(1, sr // 50)                        # 20 ms frames
    m = (n // hop) * hop
    fr = a[:m].reshape(-1, hop)
    frms = np.sqrt((fr * fr).mean(axis=1)) if len(fr) else np.zeros(1, np.float32)
    floor = max(3e-4, 0.03 * float(np.percentile(frms, 95)) if len(frms) else 3e-4)
    quiet = frms < floor
    lead = int(np.argmax(~quiet)) if (~quiet).any() else len(quiet)
    trail = int(np.argmax(~quiet[::-1])) if (~quiet).any() else len(quiet)
    gap = run = 0
    for q in quiet:
        run = run + 1 if q else 0
        gap = max(gap, run)
    return {"bytes": size, "sr": sr, "frames": n, "secs": round(secs, 3),
            "peak": round(peak, 4), "rms": round(rms, 5),
            "rms_db": round(20 * math.log10(rms) if rms > 0 else -120.0, 1),
            "dc": round(dc, 5), "clip_frac": round(clip, 6),
            "silence_frac": round(float(quiet.mean()) if len(quiet) else 1.0, 4),
            "lead_sil": round(lead * hop / sr, 2), "trail_sil": round(trail * hop / sr, 2),
            "max_gap": round(gap * hop / sr, 2)}


# ---------------------------------------------------------------- tier 1
def paper_ids(only=None):
    man = json.loads((ROOT / "papers.json").read_text())["papers"]
    ids = [p["id"] for p in man]
    if only:
        ids = [i for i in ids if i == only] or [i for i in ids if only in i]
    return [i for i in ids if (TEXT / f"{i}.txt").exists()]


def repaired_texts(pid):
    """{index: cleaned text} for chunks a previous --fix rewrote.

    A repaired chunk deliberately no longer says what `text/<id>.txt` says --
    the unspeakable part was removed on purpose. Measuring it against the
    original would re-flag it as truncation forever, so the repair record
    carries the text it actually speaks and that is what it is judged against.
    """
    f = LINT / f"{pid}.json"
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text())
    except Exception:
        return {}
    return {r["i"]: r["repaired"] for r in d.get("chunks", [])
            if isinstance(r.get("repaired"), dict) and r["repaired"].get("clean_text")}


def tier1_paper(pid):
    """All Tier-1 measurements for one paper. Runs in a worker process."""
    planned = chunks_for((TEXT / f"{pid}.txt").read_text())
    fixed = repaired_texts(pid)
    cd = CACHE / pid
    present = {}
    if cd.is_dir():
        for f in cd.glob("*.wav"):
            if f.stem.isdigit():
                present[int(f.stem)] = f
    rows = []
    for i in range(max(len(planned), (max(present) + 1) if present else 0)):
        src = planned[i] if i < len(planned) else None
        r = {"i": i, "src_known": src is not None}
        if i in fixed:
            src = fixed[i]["clean_text"]
            r["repaired"] = fixed[i]
            r["src_known"] = True
        r.update(text_metrics(src) if src is not None else
                 {"chars": 0, "words": 0, "math_frac": 0.0, "lig_frac": 0.0,
                  "digit_frac": 0.0, "max_tok": 0, "exotic_n": 0,
                  "speakable_frac": 1.0})
        f = present.get(i)
        r["audio"] = wav_metrics(f) if f is not None else {"error": "absent"}
        rows.append(r)
    return {"id": pid, "planned": len(planned), "present": len(present),
            "suspect_markers": sorted(int(s.stem) for s in cd.glob("*.suspect")
                                      if s.stem.isdigit()) if cd.is_dir() else [],
            "rows": rows}


def fit_duration_model(papers):
    """secs ~= a*chars + b, fitted on clean prose only, then outlier-trimmed.

    A per-corpus fit rather than a fixed chars-per-second constant, because the
    per-chunk constant term (the pauses at each end) is a real few tenths of a
    second and matters at short chunk lengths.
    """
    xs, ys = [], []
    for p in papers:
        for r in p["rows"]:
            au = r["audio"]
            if "error" in au or not r["src_known"] or r["chars"] < 40:
                continue
            if r["speakable_frac"] < 0.95 or r["math_frac"] > 0.002 or r["digit_frac"] > 0.05:
                continue
            xs.append(r["chars"]); ys.append(au["secs"])
    if len(xs) < 50:
        return 1.0 / 15.5, 0.0, 0.0, len(xs)
    x = np.asarray(xs, float); y = np.asarray(ys, float)
    for _ in range(3):
        a, b = np.polyfit(x, y, 1)
        res = y - (a * x + b)
        mad = float(np.median(np.abs(res - np.median(res)))) or 1e-6
        keep = np.abs(res - np.median(res)) < 4 * mad
        if keep.all():
            break
        x, y = x[keep], y[keep]
    a, b = np.polyfit(x, y, 1)
    res = y - (a * x + b)
    mad = float(np.median(np.abs(res - np.median(res))))
    return float(a), float(b), mad, len(x)


def classify_tier1(r, a, b):
    """Provisional class + the flags that produced it. Never uses ML."""
    au, flags = r["audio"], []
    if "error" in au:
        cls = "silence" if au["error"] in ("empty", "zero frames") else "missing"
        flags.append(au["error"])
        r.update(cls=cls, flags=flags, dur_ratio=None, cps=None)
        return
    secs = au["secs"]
    pred = max(0.35, a * r["chars"] + b)
    dur_ratio = secs / pred
    cps = r["chars"] / max(secs, 1e-6)
    r["dur_ratio"] = round(dur_ratio, 3)
    r["cps"] = round(cps, 2)
    r["pred_secs"] = round(pred, 2)
    r["suspect_rule"] = not (CPS_LO <= cps <= CPS_HI)      # synth.py's own rule

    if au["rms_db"] < -55 or au["silence_frac"] > SIL_FRAC_HI:
        flags.append("silence")
    if au["max_gap"] > SIL_GAP_HI:
        flags.append("gap%.1fs" % au["max_gap"])
    if au["lead_sil"] > SIL_EDGE_HI or au["trail_sil"] > SIL_EDGE_HI:
        flags.append("edge-silence")
    if au["clip_frac"] > CLIP_FRAC_HI:
        flags.append("clipping")
    if abs(au["dc"]) > DC_HI:
        flags.append("dc-offset")
    if not (RMS_DB_LO <= au["rms_db"] <= RMS_DB_HI):
        flags.append("level%.0fdB" % au["rms_db"])
    if r["src_known"] and r["chars"] > 0:
        if dur_ratio > DUR_HI:
            flags.append("long%.2fx" % dur_ratio)
        elif dur_ratio < DUR_LO:
            flags.append("short%.2fx" % dur_ratio)
        if r["suspect_rule"]:
            flags.append("cps%.1f" % cps)
        if (r["math_frac"] > MATH_FRAC_HI or r["digit_frac"] > DIGIT_FRAC_HI
                or r["speakable_frac"] < SPEAKABLE_LO or r["lig_frac"] > 0.002
                or r.get("max_tok", 0) > MAX_TOKEN_LEN):
            flags.append("unspeakable-source")
        if r.get("max_tok", 0) > MAX_TOKEN_LEN:
            flags.append("token%d" % r["max_tok"])
        if r.get("exotic_n", 0):
            # A *risk*, not a verdict: one exotic character is not a defect, but
            # every one of the 155 chunks eSpeak was caught spelling out has at
            # least one. It buys Tier 2 its recall and costs nothing here.
            flags.append("exotic%d" % r["exotic_n"])
    else:
        flags.append("no-source")

    text_bad = "unspeakable-source" in flags
    dur_bad = any(f.startswith(("long", "short", "cps")) for f in flags)
    if "silence" in flags:
        cls = "silence"
    elif any(f.startswith("token") for f in flags):
        cls = "unspeakable"          # duration cannot see this one; see MAX_TOKEN_LEN
    elif text_bad and dur_bad:
        cls = "unspeakable"
    elif dur_bad:
        cls = "duration"
    elif any(f.startswith(("clip", "dc-", "level")) for f in flags):
        cls = "level"
    elif any(f.startswith("gap") or f == "edge-silence" for f in flags):
        cls = "silence"
    else:
        cls = "ok"
    r.update(cls=cls, flags=flags)


# ---------------------------------------------------------------- tier 2
_MODEL = None


def _model(size="tiny.en"):
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(size, device="cpu", compute_type="int8",
                              cpu_threads=int(os.environ.get("LINT_ASR_THREADS", "2")))
    return _MODEL


def src_fingerprint(text):
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:12]


CODEPOINT_RX = re.compile(r"\bletter\s+(?=\S*\d)\S{2,8}")
ALPHA_RX = re.compile(r"[A-Za-z][A-Za-z'\-]*")     # the owner's word counter


def dup_coverage(words, n=4):
    if len(words) < 2 * n:
        return 0.0
    pos = collections.defaultdict(list)
    for i in range(len(words) - n + 1):
        pos[tuple(words[i:i + n])].append(i)
    cov = set()
    for ps in pos.values():
        if len(ps) > 1:
            for p in ps:
                cov.update(range(p, p + n))
    return round(len(cov) / len(words), 3)


def align(src, asr):
    sm = difflib.SequenceMatcher(None, src, asr, autojunk=False)
    eq = ins = dele = sub_s = sub_a = 0
    del_pos = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            eq += i2 - i1
        elif tag == "insert":
            ins += j2 - j1
        elif tag == "delete":
            dele += i2 - i1; del_pos += list(range(i1, i2))
        elif tag == "replace":
            sub_s += i2 - i1; sub_a += j2 - j1; del_pos += list(range(i1, i2))
    ns = max(1, len(src))
    tail = sum(1 for p in del_pos if p >= 0.75 * len(src)) / max(1, len(del_pos))
    return {"eq": eq, "ins": ins, "del": dele, "sub_src": sub_s, "sub_asr": sub_a,
            "ins_rate": round(ins / ns, 3), "del_rate": round((dele + sub_s) / ns, 3),
            "sub_rate": round(sub_s / ns, 3), "match_rate": round(eq / ns, 3),
            "tail_del_frac": round(tail, 3)}


def transcribe(path, size="tiny.en"):
    segs, _ = _model(size).transcribe(str(path), beam_size=1, language="en",
                                      vad_filter=False, condition_on_previous_text=False)
    return " ".join(s.text for s in segs).strip()


def tier2_one(args):
    """(pid, i, wav_path, src_text) -> ASR metrics. Runs in a worker process."""
    pid, i, wav, src, size = args
    t0 = time.time()
    try:
        asr_text = transcribe(pathlib.Path(wav), size)
    except Exception as e:
        return pid, i, {"error": f"{type(e).__name__}: {e}"[:180]}
    sw_raw = words_for_compare(src, expand_numbers=False)
    sw_num = words_for_compare(src, expand_numbers=True)
    aw = words_for_compare(asr_text, expand_numbers=False)
    al = align(sw_num, aw)
    out = {"asr_words": len(aw), "src_words": len(sw_raw), "src_words_spoken": len(sw_num),
           # Every ratio below is relative to THIS text. A stored record is only
           # reusable while the source it was measured against is unchanged --
           # which a repair deliberately changes.
           "src_hash": src_fingerprint(src),
           # the owner's original measurement: alphabetic source words vs a plain
           # whitespace split of the transcript. Kept so the two are comparable.
           "src_words_alpha": len(ALPHA_RX.findall(src)),
           "ratio_alpha": round(len(asr_text.split()) / max(1, len(ALPHA_RX.findall(src))), 3),
           "ratio": round(len(aw) / max(1, len(sw_raw)), 3),
           "ratio_norm": round(len(aw) / max(1, len(sw_num)), 3),
           "dup_cov": dup_coverage(aw),
           "codepoint_hits": len(CODEPOINT_RX.findall(asr_text.lower())),
           "asr_secs": round(time.time() - t0, 2),
           "asr_head": asr_text[:220]}
    out.update(al)
    return pid, i, out


def classify_tier2(r):
    """Tier 2 adjudicates; its verdict replaces the Tier-1 provisional one."""
    t = r.get("asr")
    if not t or "error" in t:
        return
    ratio, ins, dele = t["ratio"], t["ins_rate"], t["del_rate"]
    unspeakable_src = (r["math_frac"] > MATH_FRAC_HI or r["speakable_frac"] < SPEAKABLE_LO
                       or r["lig_frac"] > 0.002 or r["digit_frac"] > DIGIT_FRAC_HI
                       or r.get("max_tok", 0) > MAX_TOKEN_LEN)
    if t["codepoint_hits"] >= CODEPOINT_HITS or (unspeakable_src and ratio > RATIO_REP):
        r["cls"] = "unspeakable"
    elif unspeakable_src and not (AGREE_LO <= ratio <= AGREE_HI):
        # A defective-looking source is not on its own a defect: a chunk with
        # one ligature in it is "unspeakable" by the character test and still
        # reads perfectly. The transcript has to disagree with the text too.
        r["cls"] = "unspeakable"
    elif ratio >= RATIO_REP and ins >= INS_RATE_REP and t["dup_cov"] >= DUP_COV_REP:
        r["cls"] = "repetition"
    elif ratio <= RATIO_TRUNC and dele >= DEL_RATE_TRUNC:
        r["cls"] = "truncation"
    elif r.get("cls") in ("duration", "unspeakable"):
        if not (AGREE_LO <= ratio <= AGREE_HI):
            r["cls"] = "duration"
        elif r.get("suspect_rule"):
            # The audio faithfully renders its text, but the speech rate is
            # outside synth.py's own 8-32 chars/s band -- so the text is not
            # prose. Clearing it here would break the promise that this tool's
            # flag set is a superset of the .suspect markers.
            r["cls"] = "unspeakable" if unspeakable_src else "duration"
        else:
            r["cls"] = "ok"
    r["flags"] = [f for f in r.get("flags", []) if not f.startswith("asr")] + ["asr%.2f" % ratio]


# ---------------------------------------------------------------- reporting
def summarise(p):
    cnt = collections.Counter(r["cls"] for r in p["rows"])
    bad_secs = sum(r["audio"].get("secs", 0.0) for r in p["rows"] if r["cls"] != "ok")
    tot_secs = sum(r["audio"].get("secs", 0.0) for r in p["rows"])
    bad = sum(v for k, v in cnt.items() if k != "ok")
    sev = sum(SEVERITY_W.get(k, 1) * v for k, v in cnt.items())
    return {"chunks": len(p["rows"]), "bad": bad, "classes": dict(cnt),
            "bad_secs": round(bad_secs, 1), "secs": round(tot_secs, 1),
            "bad_frac": round(bad / max(1, len(p["rows"])), 4),
            "severity": sev, "tier2": any("asr" in r for r in p["rows"]),
            "planned": p["planned"], "present": p["present"],
            "plan_mismatch": p["planned"] != p["present"],
            "suspect_markers": len(p["suspect_markers"])}


def print_table(papers, verbose=False):
    rows = [(p["id"], summarise(p)) for p in papers]
    rows.sort(key=lambda r: (-r[1]["severity"], -r[1]["bad_secs"]))
    print(f"\n{'paper':<44}{'chunks':>7}{'bad':>5}{'bad%':>7}{'bad min':>9}"
          f"{'unspk':>7}{'rep':>5}{'trunc':>6}{'dur':>5}{'sil':>5}{'lvl':>5}{'miss':>6}{'susp':>6}")
    print("-" * 121)
    tot = collections.Counter()
    for pid, s in rows:
        c = s["classes"]
        tot.update(c)
        if s["bad"] == 0 and not verbose:
            continue
        print(f"{pid[:43]:<44}{s['chunks']:>7}{s['bad']:>5}{100*s['bad_frac']:>6.1f}%"
              f"{s['bad_secs']/60:>9.1f}{c.get('unspeakable',0):>7}{c.get('repetition',0):>5}"
              f"{c.get('truncation',0):>6}{c.get('duration',0):>5}{c.get('silence',0):>5}"
              f"{c.get('level',0):>5}{c.get('missing',0):>6}{s['suspect_markers']:>6}")
    clean = sum(1 for _, s in rows if s["bad"] == 0)
    print("-" * 121)
    print(f"{len(rows)} papers: {clean} clean, {len(rows)-clean} affected. "
          f"chunks by class: " + ", ".join(f"{k}={tot[k]}" for k in CLASSES if tot[k]))
    bad_min = sum(s["bad_secs"] for _, s in rows) / 60
    all_min = sum(s["secs"] for _, s in rows) / 60
    print(f"suspect audio: {bad_min:.1f} min of {all_min:.1f} min "
          f"({100*bad_min/max(1e-9, all_min):.2f}%)")
    return rows


def calibrate(papers):
    def col(f):
        v = [f(r) for p in papers for r in p["rows"] if "error" not in r["audio"] and f(r) is not None]
        return np.asarray(v, float)
    print("\ncorpus calibration (percentiles 1 / 5 / 50 / 95 / 99):")
    for name, f in [("dur_ratio", lambda r: r.get("dur_ratio")),
                    ("cps", lambda r: r.get("cps")),
                    ("rms_db", lambda r: r["audio"]["rms_db"]),
                    ("peak", lambda r: r["audio"]["peak"]),
                    ("dc", lambda r: abs(r["audio"]["dc"])),
                    ("silence_frac", lambda r: r["audio"]["silence_frac"]),
                    ("max_gap", lambda r: r["audio"]["max_gap"]),
                    ("clip_frac", lambda r: r["audio"]["clip_frac"])]:
        v = col(f)
        if not len(v):
            continue
        q = np.percentile(v, [1, 5, 50, 95, 99])
        print(f"  {name:<14}" + "  ".join(f"{x:>10.4f}" for x in q))


def load_lint():
    """Rebuild the in-memory paper structures from lint/*.json. Every Tier-1
    and Tier-2 *measurement* is stored, so a class can be re-decided offline."""
    out = []
    for f in sorted(LINT.glob("*.json")):
        if f.name == "index.json":
            continue
        d = json.loads(f.read_text())
        cd = CACHE / d["id"]
        out.append({"id": d["id"], "rows": d["chunks"],
                    "planned": d["summary"]["planned"], "present": d["summary"]["present"],
                    # always from disk: synth.py owns these files, not this tool
                    "suspect_markers": sorted(int(x.stem) for x in cd.glob("*.suspect")
                                              if x.stem.isdigit()) if cd.is_dir() else []})
    return out


def write_json(papers):
    LINT.mkdir(exist_ok=True)
    idx = {}
    for p in papers:
        s = summarise(p)
        doc = {"id": p["id"], "generated": time.time(), "summary": s,
               "suspect_markers": p["suspect_markers"],
               "chunks": [{k: v for k, v in r.items() if k != "src"} for r in p["rows"]]}
        tmp = LINT / f"{p['id']}.json.part"
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.replace(LINT / f"{p['id']}.json")
        idx[p["id"]] = s
    tmp = LINT / "index.json.part"
    tmp.write_text(json.dumps({"generated": time.time(), "papers": idx}, indent=1))
    tmp.replace(LINT / "index.json")


# ---------------------------------------------------------------- repair
def repair_candidates(p):
    return [r for r in p["rows"]
            if r["cls"] in ("unspeakable", "repetition", "truncation")
            and r["src_known"] and not isinstance(r.get("repaired"), dict)]


def do_fix(papers, args, srcs):
    """Additive repair. Original audio is copied to cache/<id>/.lintbak/ first,
    the replacement is verified by a fresh ASR round-trip, and the cache is only
    written when the repaired chunk verifies better than the original."""
    from kokoro import KPipeline
    pipe = KPipeline(lang_code="a", device="cpu")

    def synth(t):
        segs = [au.detach().cpu().numpy() if hasattr(au, "detach") else np.asarray(au)
                for _, _, au in pipe(t, voice=args.voice, speed=args.speed)]
        return np.concatenate(segs) if segs else np.zeros(1, np.float32)

    touched, log = set(), []
    for p in papers:
        cands = repair_candidates(p)
        if args.max_fix:
            cands = cands[:args.max_fix]
        for r in cands:
            pid, i = p["id"], r["i"]
            src = srcs[pid][i]
            cleaned = clean_for_speech(src)
            nwords = sum(c.isalpha() for w in cleaned.split() for c in w[:1])
            if nwords < FIX_MIN_WORDS:
                log.append({"id": pid, "i": i, "action": "skipped",
                            "why": f"nothing speakable left ({nwords} words)"})
                continue
            if cleaned.strip() == src.strip():
                # Nothing to change, and nothing else to try: Kokoro's output
                # length is exactly reproducible for a given (text, voice,
                # speed), and splitting a chunk reproduces the same total
                # duration to 20 ms -- so neither a retry nor a re-split can
                # repair a chunk whose text is already speakable. See --help.
                log.append({"id": pid, "i": i, "action": "skipped",
                            "why": "source is already speakable; retry/split proven not to change the output"})
                continue
            wav = CACHE / pid / f"{i:05d}.wav"
            bak = CACHE / pid / BAK_DIR
            bak.mkdir(exist_ok=True)
            before = r.get("asr", {}).get("ratio")
            if before is None:                       # always compare like with like
                _, _, t0m = tier2_one((pid, i, str(wav), src, args.model))
                r["asr"] = t0m
                before = t0m.get("ratio")
            audio = synth(cleaned)
            # candidate lives in the backup subdirectory: build.py globs
            # cache/<id>/*.wav non-recursively, so it can never be assembled.
            tmp = bak / f"{i:05d}.cand.wav"
            write_wav(tmp, audio)
            try:
                _, _, t2 = tier2_one((pid, i, str(tmp), cleaned, args.model))
                after = t2.get("ratio")
                new_secs = len(audio) / SR
                # Three independent conditions, all of which the ORIGINAL must
                # fail and the REPAIR must pass. The ASR ratio alone is not
                # enough: on codepoint-spelling audio it is noisy and can land
                # near 1.0 by coincidence (deepseekmath-grpo/00081 measures
                # 1.13 while being 123 s of "letter 1D70B").
                def verdict(secs, chars, asr):
                    cps = chars / max(secs, 1e-6)
                    return {"rate": CPS_LO <= cps <= CPS_HI,
                            "faithful": (asr.get("ratio") is not None
                                         and RATIO_TRUNC <= asr["ratio"] <= RATIO_REP),
                            "clean": asr.get("codepoint_hits", 0) < CODEPOINT_HITS}
                v_old = verdict(r["audio"].get("secs", 0.0), len(src), r.get("asr", {}))
                v_new = verdict(new_secs, len(cleaned), t2)
                if all(v_old.values()):
                    log.append({"id": pid, "i": i, "action": "skipped",
                                "why": "original already passes every check -- never overwrite a good chunk"})
                    tmp.unlink(missing_ok=True)
                    continue
                if not all(v_new.values()):
                    log.append({"id": pid, "i": i, "action": "rejected",
                                "before": before, "after": after,
                                "checks_before": v_old, "checks_after": v_new,
                                "why": "repaired chunk did not pass every check"})
                    tmp.unlink(missing_ok=True)
                    continue
                dest = bak / f"{i:05d}.wav"
                if not dest.exists():                       # never clobber the first original
                    shutil.copy2(wav, dest)
                old_secs = r["audio"].get("secs", 0.0)
                tmp.replace(wav)
                sm = CACHE / pid / f"{i:05d}.suspect"
                if sm.exists() and CPS_LO <= len(cleaned) / max(new_secs, 1e-6) <= CPS_HI:
                    sm.unlink()
                touched.add(pid)
                r.pop("asr", None)          # describes audio that no longer exists
                r["repaired"] = {"when": time.time(), "before_ratio": before,
                                 "after_ratio": after, "clean_chars": len(cleaned),
                                 "src_chars": len(src), "clean_text": cleaned}
                log.append({"id": pid, "i": i, "action": "repaired",
                            "before_ratio": before, "after_ratio": after,
                            "checks_before": v_old, "checks_after": v_new,
                            "before_secs": round(old_secs, 2), "after_secs": round(new_secs, 2),
                            "src_chars": len(src), "clean_chars": len(cleaned),
                            "backup": str(dest.relative_to(ROOT)),
                            "clean_head": cleaned[:120],
                            # the whole text, so lint/<id>.json can always be
                            # rebuilt from this append-only ledger alone
                            "clean_text": cleaned})
            finally:
                tmp.unlink(missing_ok=True)
    for line in log:
        print("  " + json.dumps(line))
    for pid in sorted(touched):
        print(f"\nrebuilding out/{pid}.wav ...")
        outwav = OUT / f"{pid}.wav"
        was = wav_secs(outwav)
        proc = subprocess.run([sys.executable, str(ROOT / "build.py"), "--only", pid],
                              cwd=ROOT, capture_output=True, text=True)
        print(proc.stdout.strip()[-400:] or proc.stderr.strip()[-400:])
        print(f"  out/{pid}.wav {was/60:.2f} min -> {wav_secs(outwav)/60:.2f} min")
    if not touched:
        print("\nnothing repaired; out/*.wav untouched")
    LINT.mkdir(exist_ok=True)
    with (LINT / "fixes.jsonl").open("a") as fh:      # append-only repair ledger
        fh.write("".join(json.dumps(x) + "\n" for x in log))
    return log


def wav_secs(p):
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="one paper id (exact wins, else substring)")
    ap.add_argument("--tier1", action="store_true", help="Tier 1 only, no ASR")
    ap.add_argument("--full", action="store_true", help="Tier 2 over EVERY chunk")
    ap.add_argument("--sample", type=int, default=0,
                    help="also ASR this many random Tier-1-clean chunks, as a control")
    ap.add_argument("--chunks", help="explicit id:index[,id:index...] for Tier 2")
    ap.add_argument("--fix", action="store_true", help="repair flagged chunks (additive, verified)")
    ap.add_argument("--max-fix", type=int, default=0, help="cap repairs per paper")
    ap.add_argument("--estimate", action="store_true", help="cost of a --full sweep, then exit")
    ap.add_argument("--report", action="store_true", help="re-print from lint/*.json, measure nothing")
    ap.add_argument("--rescan", action="store_true",
                    help="re-measure Tier 1 from disk, keep the stored transcripts, "
                         "re-classify (use after a repair, or after adding a signal)")
    ap.add_argument("--reclassify", action="store_true",
                    help="re-decide every class from the stored measurements "
                         "(re-tune a threshold without re-transcribing 53 h)")
    ap.add_argument("--calibrate", action="store_true", help="print corpus percentiles")
    ap.add_argument("--verbose", action="store_true", help="list clean papers too")
    ap.add_argument("--jobs", type=int, default=max(1, min(6, (os.cpu_count() or 2) // 2)))
    ap.add_argument("--model", default="tiny.en")
    ap.add_argument("--voice", default="af_heart"); ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--yes", action="store_true", help="run --full without the cost prompt")
    ap.add_argument("--refresh-asr", action="store_true",
                    help="ignore stored transcripts and transcribe again")
    a = ap.parse_args()

    if a.rescan:
        stored = {(p["id"], r["i"]): r.get("asr") for p in load_lint() for r in p["rows"]}
        ids = paper_ids(a.only)
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            papers = list(ex.map(tier1_paper, ids))
        for p in papers:
            for r in p["rows"]:
                got = stored.get((p["id"], r["i"]))
                if got:
                    r["asr"] = got
        aa, bb, mad, nfit = fit_duration_model(papers)
        for p in papers:
            for r in p["rows"]:
                classify_tier1(r, aa, bb)
                classify_tier2(r)
        kept = sum(1 for p in papers for r in p["rows"] if "asr" in r)
        print(f"re-measured {sum(len(p['rows']) for p in papers)} chunks, "
              f"kept {kept} stored transcripts; model secs = {aa:.5f}*chars + {bb:.3f}")
        print_table(papers, a.verbose)
        write_json(papers)
        return 0

    if a.reclassify:
        papers = load_lint()
        if not papers:
            print("no lint/*.json yet"); return 1
        aa, bb, mad, nfit = fit_duration_model(papers)
        print(f"duration model refitted on {nfit} clean chunks: "
              f"secs = {aa:.5f}*chars + {bb:.3f} (MAD {mad:.2f}s)")
        for p in papers:
            for r in p["rows"]:
                classify_tier1(r, aa, bb)
                classify_tier2(r)
        print_table(papers, a.verbose)
        write_json(papers)
        print(f"\nrewrote lint/*.json for {len(papers)} papers")
        return 0

    if a.report:
        papers = load_lint()
        if not papers:
            print("no lint/*.json yet; run without --report first"); return 1
        print_table(papers, a.verbose)
        return 0

    ids = paper_ids(a.only)
    if not ids:
        print("no papers with extracted text matched"); return 1

    # ---- Tier 1
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        papers = list(ex.map(tier1_paper, ids))
    t1_read = time.time() - t0
    aa, bb, mad, nfit = fit_duration_model(papers)
    for p in papers:
        for r in p["rows"]:
            classify_tier1(r, aa, bb)
    nchunks = sum(len(p["rows"]) for p in papers)
    tot_secs = sum(r["audio"].get("secs", 0.0) for p in papers for r in p["rows"])
    print(f"TIER 1: {len(papers)} papers, {nchunks} chunks, {tot_secs/3600:.2f} h of audio "
          f"in {t1_read:.1f}s ({a.jobs} workers)")
    print(f"  duration model: secs = {aa:.5f} * chars + {bb:.3f}   "
          f"(= {1/aa:.1f} chars/s asymptotic; fitted on {nfit} clean chunks, "
          f"residual MAD {mad:.2f}s)")
    if a.calibrate:
        calibrate(papers)

    # ---- who gets ASR
    srcs = {p["id"]: chunks_for((TEXT / f"{p['id']}.txt").read_text()) for p in papers}

    def effective_src(pid, r):
        """What this chunk is supposed to say now -- the repaired text if it was
        repaired, otherwise the chunk the narrator was given."""
        rep = r.get("repaired")
        if isinstance(rep, dict) and rep.get("clean_text"):
            return rep["clean_text"]
        return srcs[pid][r["i"]] if r["i"] < len(srcs[pid]) else ""

    # Transcripts are expensive and the audio they describe has not changed, so
    # carry forward anything lint/*.json already holds -- but only where it was
    # measured against the same text. Without the carry-forward a
    # `--tier1 --only X` silently discards X's share of a 39-minute sweep;
    # without the fingerprint check a repaired chunk keeps a ratio computed
    # against the text it no longer speaks.
    stored = {(q["id"], r["i"]): r.get("asr") for q in load_lint() for r in q["rows"]}
    reused = stale = 0
    for p in papers:
        for r in p["rows"]:
            got = None if a.refresh_asr else stored.get((p["id"], r["i"]))
            if not got:
                continue
            want = src_fingerprint(effective_src(p["id"], r))
            # records written before fingerprinting have no hash; trust them
            # unless this chunk has been repaired, which changes the source
            if got.get("src_hash", want if "repaired" not in r else None) != want:
                stale += 1
                continue
            r["asr"] = got
            reused += 1
    if reused or stale:
        print(f"  reused {reused} stored transcripts"
              + (f", dropped {stale} measured against different text" if stale else ""))
    # Adjudicate on what we already know, so that every exit path below -- and
    # `--tier1` in particular -- reports the Tier-2 verdict where one exists
    # instead of silently reverting to the provisional class.
    for p in papers:
        for r in p["rows"]:
            if "asr" in r:
                classify_tier2(r)

    def wavpath(pid, i): return CACHE / pid / f"{i:05d}.wav"

    if a.chunks:
        want = set()
        for tok in a.chunks.split(","):
            pid, _, ix = tok.partition(":")
            want.add((pid, int(ix)))
        todo = [(p["id"], r) for p in papers for r in p["rows"] if (p["id"], r["i"]) in want]
    elif a.full:
        todo = [(p["id"], r) for p in papers for r in p["rows"]
                if "error" not in r["audio"] and "asr" not in r]
    else:
        todo = [(p["id"], r) for p in papers for r in p["rows"]
                if (r["cls"] in ("duration", "unspeakable", "silence")
                    or r.get("exotic_n", 0))
                and "error" not in r["audio"] and "asr" not in r]
        if a.sample:
            import random
            rng = random.Random(20260910)
            clean = [(p["id"], r) for p in papers for r in p["rows"]
                     if r["cls"] == "ok" and "error" not in r["audio"]]
            todo += rng.sample(clean, min(a.sample, len(clean)))

    asr_secs = sum(r["audio"].get("secs", 0.0) for _, r in todo)
    # Measured on this host over the whole 52.62 h corpus: 81x realtime
    # aggregate at 5 workers, i.e. 16x per worker. (A single clean chunk runs
    # at 53x; the corpus average is far lower because a defective chunk is both
    # longer and much slower to decode.) Override with LINT_ASR_RTF.
    rtf = float(os.environ.get("LINT_ASR_RTF", "16"))
    est = asr_secs / (rtf * a.jobs)
    full_secs = sum(r["audio"].get("secs", 0.0) for p in papers for r in p["rows"])
    print(f"  a FULL Tier-2 sweep would transcribe {full_secs/3600:.2f} h "
          f"=> ~{full_secs/(rtf*a.jobs)/60:.0f} min wall clock at {a.jobs} workers")
    if a.estimate:
        return 0
    if a.tier1:
        print_table(papers, a.verbose); write_json(papers); return 0
    if a.full and not a.yes:
        print("  (--full without --yes: printing the estimate only. Re-run with --yes to sweep.)")
        print_table(papers, a.verbose); write_json(papers); return 0

    # ---- Tier 2
    if todo:
        print(f"\nTIER 2: {len(todo)} chunks, {asr_secs/60:.1f} min of audio, "
              f"~{est/60:.1f} min predicted ...", flush=True)
        t0 = time.time()
        work = [(pid, r["i"], str(wavpath(pid, r["i"])), effective_src(pid, r), a.model)
                for pid, r in todo]
        byid = {(p["id"], r["i"]): r for p in papers for r in p["rows"]}
        done = 0
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            for pid, i, res in ex.map(tier2_one, work, chunksize=1):
                byid[(pid, i)]["asr"] = res
                done += 1
                if done % 50 == 0:
                    print(f"   {done}/{len(work)}  {(time.time()-t0)/60:.1f} min", flush=True)
        for p in papers:
            for r in p["rows"]:
                if "asr" in r:
                    classify_tier2(r)
        print(f"TIER 2 done in {(time.time()-t0)/60:.1f} min "
              f"({asr_secs/max(1e-9, time.time()-t0):.0f}x realtime aggregate)")

    rows = print_table(papers, a.verbose)
    write_json(papers)
    print(f"\nwrote lint/<id>.json for {len(papers)} papers + lint/index.json")

    if a.fix:
        print("\nREPAIR")
        do_fix(papers, a, srcs)
        write_json(papers)      # repaired chunks lose their stale transcript here
        print("run `lint_audio.py --only <id>` to re-measure the repaired chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
