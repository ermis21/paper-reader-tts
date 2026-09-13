#!/usr/bin/env python3
"""Assemble cached chunks into per-paper audio and a chaptered M4B.

Loudness is normalised to a consistent target because road noise is unforgiving:
a paper that renders 6 dB quieter than the last one is unlistenable at speed.
"""
import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
CACHE, OUT, TEXT = ROOT/"cache", ROOT/"out", ROOT/"text"
M4B_NAME = "audiobook.m4b"     # out/<this>; the /m4b endpoint serves the same name
SR = 24000
GAP = 0.18          # seconds between chunks
TARGET_RMS = 0.10   # ~ -20 dBFS RMS, sensible headroom for spoken word in a car

def read_wav(p):
    with wave.open(str(p),"rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()),dtype="<i2").astype(np.float32)/32767

def write_wav(p, a):
    tmp = p.with_suffix(".part")
    a = np.clip(a,-1,1)
    with wave.open(str(tmp),"wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((a*32767).astype("<i2").tobytes())
    tmp.replace(p)

def normalise(a):
    rms = float(np.sqrt((a**2).mean()))
    if rms < 1e-6: return a
    g = TARGET_RMS/rms
    peak = float(np.abs(a).max())*g
    if peak > 0.97: g *= 0.97/peak      # never clip
    return a*g

def ms(x): return int(x*1000)


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
    ap.add_argument("--m4b", action="store_true", help="also build the chaptered M4B")
    ap.add_argument("--only", help="reassemble just this paper (skips re-reading the whole cache)")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    manifest = json.loads((ROOT/"papers.json").read_text())
    papers = manifest["papers"]
    sel = select_ids(papers, a.only)
    gap = np.zeros(int(GAP*SR), np.float32)
    chapters, parts, t = [], [], 0.0
    print(f"{'paper':<40}{'chunks':>7}{'missing':>8}{'susp':>6}{'minutes':>9}")
    for p in papers:
        if p["id"] not in sel:
            w = OUT/f"{p['id']}.wav"          # keep chapter timeline correct
            if w.exists():
                d = len(read_wav(w))/SR
                chapters.append((t, t+d, f"{p['order']:02d}. {p['title']}"))
                parts.append(w); t += d
            continue
        cd = CACHE/p["id"]
        if not cd.exists(): continue
        files = sorted(cd.glob("*.wav"))
        if not files: continue
        idx = [int(f.stem) for f in files]
        missing = sorted(set(range(max(idx)+1)) - set(idx))
        susp = len(list(cd.glob("*.suspect")))
        segs = []
        for f in files:
            segs.append(read_wav(f)); segs.append(gap)
        audio = normalise(np.concatenate(segs))
        dur = len(audio)/SR
        write_wav(OUT/f"{p['id']}.wav", audio)
        print(f"{p['id'][:39]:<40}{len(files):>7}{len(missing):>8}{susp:>6}{dur/60:>9.1f}")
        if missing: print(f"    ! missing chunk indices: {missing[:10]}{'...' if len(missing)>10 else ''}")
        chapters.append((t, t+dur, f"{p['order']:02d}. {p['title']}"))
        parts.append(OUT/f"{p['id']}.wav"); t += dur
    print(f"\ntotal {t/3600:.2f} h across {len(parts)} papers -> out/")

    if not a.m4b: return 0
    if not shutil.which("ffmpeg"):
        print("\nffmpeg not installed - per-paper WAVs are in out/.")
        print("Install it, then re-run:  ./build.py --m4b")
        print("   sudo apt install -y ffmpeg")
        return 0
    meta = OUT/"chapters.txt"
    with open(meta,"w") as f:
        # Title and artist come from the manifest, so the M4B is labelled with
        # whatever collection you actually built.
        btitle = re.sub(r"[=;#\\\\]", " ", str(manifest.get("title", "Audiobook")))
        bartist = re.sub(r"[=;#\\\\]", " ", str(manifest.get("author", "Various")))
        f.write(f";FFMETADATA1\ntitle={btitle}\nartist={bartist}\n")
        for s,e,name in chapters:
            safe = re.sub(r"[=;#\\\\]", " ", name)
            f.write(f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={ms(s)}\nEND={ms(e)}\ntitle={safe}\n")
    lst = OUT/"concat.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    m4b = OUT/M4B_NAME
    cmd = ["ffmpeg","-y","-f","concat","-safe","0","-i",str(lst),"-i",str(meta),
           "-map_metadata","1","-c:a","aac","-b:a","64k","-ar","24000","-ac","1",
           "-movflags","+faststart",str(m4b)]
    print("\nencoding M4B ...")
    r = subprocess.run(cmd, cwd=OUT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-1500:]); return 1
    print(f"  -> {m4b}  ({m4b.stat().st_size/1e6:.1f} MB, {len(chapters)} chapters)")
    return 0

if __name__ == "__main__": sys.exit(main())
