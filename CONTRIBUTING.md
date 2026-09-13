# Contributing

Small project, few rules, but the rules that exist are load-bearing — most of
them are scars. Please read the "invariants" section before changing the front
end or the extraction pipeline.

## Setting up

```bash
./install.sh --cpu-torch     # creates ./venv (refuses to clobber an existing one)
./start.sh                   # http://127.0.0.1:3002
npm i                        # jsdom, for the front-end tests only
```

`ffmpeg` is optional and only `./build.py --m4b` uses it.

## Before you open a pull request

```bash
uvx ruff check .             # or: ruff check .
npm test                     # page-script check + the jsdom playback test
```

Both must pass. `ruff` config lives in `pyproject.toml`; there is no separate
config file and no pre-commit hook to install.

If you touched the pipeline, also run the stages that are safe to repeat:

```bash
./extract.py                 # re-extracts and prints the quality table
./synth.py --plan            # chunk counts, no synthesis
```

## Licensing of contributions

This project is **AGPL-3.0-or-later** (see `LICENSE`, and
`THIRD_PARTY_LICENCES.md` for why it has to be). By contributing you agree
your contribution is licensed the same way. There is no CLA.

Adding a dependency means adding a row to `THIRD_PARTY_LICENCES.md` with its
licence and version — no exceptions, because that file is the evidence for the
project's own licence. A dependency that is GPL-incompatible (anything
proprietary, or a licence with an incompatible patent or advertising clause)
cannot be added at all.

## Invariants — please do not break these

**Never destroy or recreate a live `<audio>` element.** This is the one that
matters most. `static/index.html` creates each `<audio>` exactly once in
`attachAudio()`, guarded by `!slot.querySelector('audio')`, and patches text in
place with `setText()`. `#lib` is **never** assigned `innerHTML`. Cards are
*hidden*, not detached, when you switch folders; they are reordered with CSS
`order` on a flex column, never by moving DOM nodes; and a card that has
vanished from the payload is not reaped while its audio is playing. Rebuilding
the library on every poll is exactly what used to stop playback after two or
three seconds. `tests/dom_playback.test.js` asserts all of it against a real
DOM, and `tests/check_page_script.js` catches the mistake statically.

**Keep HTTP Range support.** Seeking inside a 300 MB WAV depends on
`_ranged()` returning `206` with a correct `Content-Range`. Any new
file-serving endpoint must go through it.

**Keep every file endpoint behind `_resolve()`.** It is the single traversal
guard. Do not open a path any other way, and do not relax the
`^[A-Za-z0-9._-]+$` rule. See `SECURITY.md`.

**Folder names must never become filesystem paths.** The workspace tree is
virtual on purpose; that is what makes a hostile folder name inert.

**Deletion is not destruction.** Archiving hides a row. The PDF, the extracted
text, the chunk cache and the rendered WAV stay on disk. Re-rendering a
90-minute paper costs an hour of CPU, and the app will not spend that on
someone's behalf. Do not add a code path that unlinks user artefacts.

**Synthesis stays on the CPU.** `synth.py` pins `KPipeline(device="cpu")` and
the systemd template sets `CUDA_VISIBLE_DEVICES=`. Kokoro is non-autoregressive
and renders at 3.3–3.7× realtime on CPU; moving it to a GPU buys little and
makes the tool unusable on a machine whose GPU is doing something else.

**Everything is resumable.** Every chunk is cached as its own WAV, so an
interrupted run continues by being re-run, and progress is readable from disk.
Do not introduce a stage that must complete in one go, or that only writes its
output at the end.

## Extraction quality — the rules that were paid for

These look arbitrary. They are not; each replaced something that produced
unlistenable audio.

- **References are cut at an absolute page floor, not a page fraction.**
  SWE-agent's references start on p9 of a 118-page PDF, so a percentage rule
  swallowed the entire appendix and turned a 34-minute paper into 4.4 hours of
  agent-trajectory dumps. The rule is `page_index >= 2 and chars_seen > 4000`.

- **Sentences are packed up to `MAX_CHARS` *across* paragraph boundaries.**
  Packing per paragraph fragments formal papers badly — one paper went to 971
  chunks averaging ~15 words where another, packed across paragraphs, gave 355
  of ~44. Short chunks are choppy to listen to and much slower to render.

- **Every chunk gets a duration sanity check.** Characters-per-second outside
  8–32 is flagged, written next to the chunk as `NNNNN.suspect`, and reported.
  This catches a silent or runaway chunk before you meet it in a car.

- **Loudness is normalised to a fixed RMS target.** A paper that renders 6 dB
  quieter than the last one is unlistenable against road noise.

- **`--only` prefers an exact id and only then falls back to substring.** With
  folder uploads creating many ids at once, substring-only matching silently
  re-extracts and re-assembles the wrong papers.

- **The longest-sentence column in `./extract.py`'s table is the tell.** A
  300+ word "sentence" means a table or a code block reflowed into prose. If
  you change the extraction heuristics, watch that number.

## Style

- Match the surrounding code. The batch scripts use compact one-line clauses
  (`if not s: kept.append(""); continue`); `E701`/`E702` are switched off in
  `pyproject.toml` for exactly that reason. Do not restyle files you are not
  otherwise changing.
- Comments should say **why**, especially where the code looks odd. Most of
  the odd-looking code here is odd for a reason that cost someone an evening.
- No new runtime dependency without a good reason; see the licensing note
  above. The front end has **no build step and no framework**, and the page is
  one self-contained HTML file. Please keep it that way.

## What is deliberately not in the repository

`pdfs/`, `dropin/`, `text/`, `cache/`, `out/` and `files/` are never committed:
they hold third-party papers and the audio derived from them. `workspace.sqlite`
and the generated `papers.json` are local state. If a pull request adds any of
those, `.gitignore` has been edited — please put it back.
