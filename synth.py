#!/usr/bin/env python3
"""Kokoro synthesis: resumable, chunk-cached, with a duration sanity check.

Runs on CPU so vLLM keeps both GPUs. Every chunk is cached as its own wav, so
an interrupted run resumes by re-running, and progress is readable from disk.
"""
import argparse
import json
import pathlib
import re
import sys
import time
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
TEXT, CACHE, OUT = ROOT/"text", ROOT/"cache", ROOT/"out"
SR = 24000
MAX_CHARS = 400          # Kokoro is stable well past this; keeps cache granular
CPS_LO, CPS_HI = 8.0, 32.0   # plausible chars-per-second of speech; outside => suspect

PARA_SPAN = re.compile(r"[^\n]+(?:\n(?!\n)[^\n]+)*")
SENT_END = re.compile(r"(?<=[.!?])\s+")

def chunk_spans(text):
    """chunks_for plus char offsets: a list of (chunk_text, start, end).

    Offsets point into the ORIGINAL text, so align.py can map a chunk back to
    its place in text/<id>.txt. The chunk strings are byte-identical to what
    chunks_for has always produced -- the cached audio is keyed by chunk
    position and narrates those exact strings, so any drift would silently
    desync the reader's offsets from the cached WAVs. tests/test_align.py
    asserts this identity over every extracted paper.
    """
    # Pack sentences up to MAX_CHARS *across* paragraph boundaries. Packing per
    # paragraph fragments formal papers badly (Russell: 971 chunks of ~15 words
    # vs Agre's 355 of ~44) which is choppy to listen to and far slower.
    sents = []                                    # (sentence, char_start)
    for pm in PARA_SPAN.finditer(text):
        para = pm.group().strip()
        if not para: continue
        base = pm.start() + (len(pm.group()) - len(pm.group().lstrip()))
        start = 0
        for m in SENT_END.finditer(para):
            s = para[start:m.start()]
            if s.strip(): sents.append((s, base + start))
            start = m.end()
        s = para[start:]
        if s.strip(): sents.append((s, base + start))
    out, buf = [], ""
    bstart = bend = 0
    for s, ss in sents:
        if len(buf) + len(s) + 1 <= MAX_CHARS:
            if not buf: bstart = ss
            bend = ss + len(s)
            buf = f"{buf} {s}".strip()
        else:
            if buf: out.append((buf, bstart, bend))
            while len(s) > MAX_CHARS:                # pathological single sentence
                cut = s.rfind(",", 0, MAX_CHARS)
                cut = cut if cut > MAX_CHARS//2 else MAX_CHARS
                piece = s[:cut].strip()
                pstart = ss + (len(s[:cut]) - len(s[:cut].lstrip()))
                out.append((piece, pstart, pstart + len(piece)))
                rest = s[cut:]
                ss += cut + (len(rest) - len(rest.lstrip()))
                s = rest.strip()
            buf = s; bstart = ss; bend = ss + len(s)
    if buf: out.append((buf, bstart, bend))
    return out

def chunks_for(text):
    """Sentence-grouped chunks, never splitting mid-sentence."""
    return [c for c, _, _ in chunk_spans(text)]

def write_wav(path, audio):
    tmp = path.with_suffix(".part")
    a = np.clip(np.asarray(audio, dtype=np.float32), -1, 1)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((a * 32767).astype("<i2").tobytes())
    tmp.replace(path)

def read_wav(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)/32767


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
    ap.add_argument("--only"); ap.add_argument("--voice", default="af_heart")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--plan", action="store_true", help="chunk only, no synthesis")
    a = ap.parse_args()

    papers = json.loads((ROOT/"papers.json").read_text())["papers"]
    sel = select_ids(papers, a.only)
    todo = [p for p in papers if (TEXT/f"{p['id']}.txt").exists() and p["id"] in sel]
    plans = [(p, chunks_for((TEXT/f"{p['id']}.txt").read_text())) for p in todo]
    total = sum(len(c) for _, c in plans)
    print(f"{len(plans)} papers, {total} chunks, voice={a.voice} speed={a.speed}")
    for p, c in plans:
        print(f"   {p['id'][:38]:<39} {len(c):>5} chunks")
    if a.plan: return 0

    from kokoro import KPipeline
    pipe = KPipeline(lang_code="a", device="cpu")
    done_all = time.time(); made = cached = suspect = 0

    for p, chs in plans:
        cd = CACHE/p["id"]; cd.mkdir(parents=True, exist_ok=True)
        t0 = time.time(); nmade = 0
        for i, ch in enumerate(chs):
            dest = cd/f"{i:05d}.wav"
            if dest.exists() and dest.stat().st_size > 1000: cached += 1; continue
            segs = [au.detach().cpu().numpy() if hasattr(au, "detach") else np.asarray(au)
                    for _, _, au in pipe(ch, voice=a.voice, speed=a.speed)]
            audio = np.concatenate(segs) if segs else np.zeros(1, np.float32)
            dur = len(audio)/SR
            cps = len(ch)/max(dur, 1e-6)
            if not (CPS_LO <= cps <= CPS_HI):
                suspect += 1
                print(f"     ! suspect chunk {p['id']}/{i:05d}: {len(ch)} chars -> {dur:.2f}s ({cps:.1f} c/s)")
                (cd/f"{i:05d}.suspect").write_text(f"{len(ch)} chars, {dur:.2f}s, {cps:.1f} cps\n")
            write_wav(dest, audio); made += 1; nmade += 1
            if nmade % 25 == 0:
                el = time.time()-t0
                print(f"     {p['id'][:34]:<35} {i+1}/{len(chs)}  {el/60:.1f}m elapsed", flush=True)
        print(f"  [ok] {p['id'][:38]:<39} {len(chs)} chunks ({nmade} new) {(time.time()-t0)/60:.1f}m")
    print(f"\n{made} generated, {cached} from cache, {suspect} suspect, {(time.time()-done_all)/60:.1f}m total")
    return 0

if __name__ == "__main__": sys.exit(main())
