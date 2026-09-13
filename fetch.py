#!/usr/bin/env python3
"""Fetch the open-access PDFs. Paywalled papers are routed to dropin/.

Only downloads sources that are openly published (arXiv, JAIR, author and
course pages). Anything else you supply yourself from a copy you have lawful
access to; this tool is for format-shifting your own reading, nothing more.
"""
import json
import pathlib
import shutil
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
PDFS, DROPIN = ROOT/"pdfs", ROOT/"dropin"
UA = {"User-Agent": "Mozilla/5.0 (paper-audiobook; personal use)"}

def looks_like_pdf(p):
    try:
        with open(p,"rb") as f: return f.read(5) == b"%PDF-"
    except Exception: return False

def main():
    papers = json.loads((ROOT/"papers.json").read_text())["papers"]
    ok, miss = [], []
    for p in papers:
        dest = PDFS/f"{p['id']}.pdf"
        drop = DROPIN/f"{p['id']}.pdf"
        if dest.exists() and looks_like_pdf(dest):
            print(f"  [have]  {p['id']}"); ok.append(p['id']); continue
        if drop.exists() and looks_like_pdf(drop):
            shutil.copy2(drop, dest); print(f"  [drop]  {p['id']}  <- dropin/"); ok.append(p['id']); continue
        if not p.get("url"):
            print(f"  [NEED]  {p['id']}  -> put PDF at dropin/{p['id']}.pdf   ({p['venue']})")
            miss.append(p['id']); continue
        tmp = dest.with_suffix(".part")
        try:
            req = urllib.request.Request(p["url"], headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp,"wb") as f:
                shutil.copyfileobj(r, f)
            if not looks_like_pdf(tmp):
                tmp.unlink(missing_ok=True); raise ValueError("not a PDF (login wall or HTML?)")
            tmp.replace(dest)
            print(f"  [get]   {p['id']}  {dest.stat().st_size/1e6:.1f} MB"); ok.append(p['id'])
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"  [FAIL]  {p['id']}  {type(e).__name__}: {str(e)[:70]}")
            print(f"          -> put PDF at dropin/{p['id']}.pdf")
            miss.append(p['id'])
    print(f"\n{len(ok)}/{len(papers)} available." + (f"  Missing: {', '.join(miss)}" if miss else ""))
    return 0

if __name__ == "__main__":
    sys.exit(main())
