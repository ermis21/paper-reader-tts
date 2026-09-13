# paper-audiobook

Turn academic PDFs into narrated audio you can organise, browse and listen to —
and, optionally, into a chaptered **M4B**.

A local web app plus a four-stage CLI pipeline. PDFs go in; speech-ready text,
per-chunk cached WAVs and per-paper audio come out. It runs entirely on your own
machine, on the **CPU**, and it is resumable: every chunk is cached on disk, so
an interrupted render continues by being re-run.

> **Licence: AGPL-3.0-or-later, and that is not a preference.** Three separate
> dependencies force copyleft — PyMuPDF (AGPL-3.0-or-later), and Kokoro's
> English G2P chain, which loads `phonemizer-fork` (GPL-3.0-or-later) and
> bundled eSpeak NG (GPL-3.0-or-later) on every run. Swapping the PDF library
> alone does **not** make a permissive licence possible. See
> [Licence](#licence) and [`THIRD_PARTY_LICENCES.md`](THIRD_PARTY_LICENCES.md).

---

## Contents

- [What it is](#what-it-is)
- [Quickstart](#quickstart)
- [Install](#install)
- [The web UI](#the-web-ui)
- [The CLI pipeline](#the-cli-pipeline)
- [Audio quality linting](#audio-quality-linting)
- [Architecture](#architecture)
- [Extraction quality — the rules that were paid for](#extraction-quality--the-rules-that-were-paid-for)
- [Why Kokoro, and why the CPU](#why-kokoro-and-why-the-cpu)
- [The front-end invariant](#the-front-end-invariant)
- [HTTP API](#http-api)
- [Sources and copyright](#sources-and-copyright)
- [Security](#security)
- [Limitations](#limitations)
- [Licence](#licence)

---

## What it is

Long papers are hard to read and easy to listen to. This turns a directory of
PDFs into an audio library:

    PDF → text extraction → speech normalisation → TTS → chunk cache → per-paper WAV → [M4B]

Two ways to drive it, over the same data:

- **A web UI** (`webapp.py` + `static/index.html`) — drag a PDF, a pile of PDFs,
  or a whole folder tree onto the page. It extracts, narrates and assembles with
  live progress, and gives you an inline player, a virtual folder tree to
  organise things, and per-document download links.
- **A CLI pipeline** (`fetch.py` → `extract.py` → `synth.py` → `build.py`) —
  scriptable, resumable, driven by a `papers.json` manifest.

Both read the same workspace, so you can upload in the browser and finish in a
terminal.

## Quickstart

```bash
git clone <this repo> paper-audiobook && cd paper-audiobook
./install.sh --cpu-torch        # creates ./venv (a few GB of model deps)
./start.sh                      # http://127.0.0.1:3002
```

Open the page, drop a PDF on it, wait. A 30-page paper is roughly ten minutes
of CPU for about 35 minutes of audio. Progress is live, and everything is
cached, so you can close the tab or kill the process and pick up where it left
off.

There is no cloud service, no API key and no account. Nothing leaves the
machine except the model weights it downloads once from Hugging Face, and
whatever `fetch.py` downloads because you asked it to.

## Install

Requirements:

- **Python ≥ 3.12.** 3.12 is what this is tested on. Do not reach for the
  newest CPython — at the time of writing 3.14 has no wheels for `torch` or the
  spaCy stack, so the install fails outright rather than the app failing later.
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**, used to
  create the venv and install pinned requirements.
- **`ffmpeg` — optional.** Needed *only* by `./build.py --m4b`. Extraction,
  narration and the per-paper WAVs in `out/` all work without it; the `/m4b`
  endpoint simply returns 404. Install it from your package manager if you want
  a chaptered audiobook file.
- Disk: the model is small (82M parameters), but audio is not. Uncompressed
  24 kHz mono WAV runs about **170 MB per hour**, and the chunk cache holds
  roughly the same again until you delete it.

```bash
./install.sh              # plain PyPI torch
./install.sh --cpu-torch  # CPU-only torch: ~3 GB less, and 15 fewer proprietary
                          # NVIDIA CUDA wheels this project never loads
./install.sh --force      # recreate an existing venv (it refuses by default)
```

`install.sh` never touches your data: `pdfs/`, `text/`, `cache/` and `out/` are
left alone even with `--force`.

Exact pins live in `requirements.txt`; `pyproject.toml` carries project metadata
and the `ruff` configuration.

## The web UI

```bash
./start.sh                       # foreground on http://127.0.0.1:3002
PA_HOST=0.0.0.0 ./start.sh       # exposed — read SECURITY.md first
```

`start.sh` sources `./local.env` if it exists, so machine-specific settings
(bind address, port, thread count, `HF_HOME`) live in a file you own rather
than in the shipped code. Copy `local.env.example` to get started.

**The shipped default binds to `127.0.0.1` on purpose.** This app is
unauthenticated, and an upload starts a subprocess pipeline. `PA_HOST` is the
deliberate opt-out; please read [`SECURITY.md`](SECURITY.md) before you use it,
and never forward the port from a router.

What the UI gives you:

- **Workspace** — a folder tree in the sidebar, breadcrumbs and folder tiles.
  Make a folder by hand with **+ New folder** (it lands in the folder on screen)
  or a tree row's **+** (inside that row's folder); rename or delete one from its
  tree row, and drag a folder onto another to nest it. Row actions stay visible
  on a phone, which has no hover. Drag a document's grip, or use its *Move to*
  menu (which works on a phone), to file it.
- **Library** — one card per document, under the paper's real title read off
  its PDF ([Titles](#titles)), with an inline
  player, duration, chunk count and download links for the PDF, the WAV and the
  text. Client-side search and sort; one player at a time with a now-playing
  marker and a per-document speed control. Failed renders show their error on
  the card and in the queue, with a retry button. Audio is served with HTTP
  Range support, so seeking inside a 300 MB WAV works.
- **Upload** — a file, a pile of files, or a whole folder. The directory tree is
  recreated as folders, and every PDF is queued through extract → narrate →
  assemble with live progress. One worker thread handles the queue, so an upload
  never contends with itself for CPU.
- **Resume** — playback position is remembered per document in `localStorage`
  under `pa:pos:<id>`. These are ninety-minute papers; you will not finish one
  in a sitting.

### Running it as a service

The repository ships a **template**, not a unit, because a unit is
machine-specific:

```bash
./install-service.sh                    # renders paper-audiobook-web.service.local
PA_HOST=0.0.0.0 ./install-service.sh    # ... exposed, if you mean it

sudo install -m 644 -o root -g root paper-audiobook-web.service.local \
     /etc/systemd/system/paper-audiobook-web.service
sudo systemctl daemon-reload && sudo systemctl enable --now paper-audiobook-web
journalctl -u paper-audiobook-web -f
```

`install-service.sh` uses no `sudo` and writes nothing outside the checkout:
it fills in your user, group, home and working directory, and stops so you can
read the result. The rendered unit sets `CUDA_VISIBLE_DEVICES=` and applies
`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full` and
`ProtectHome=read-only`.

### Restarting it by hand

```bash
ss -ltnpH 'sport = :3002'        # find the listener, kill THAT pid
./start.sh
```

Match on the **port**, never on the process name: `pkill -f webapp.py` also
matches the shell you typed it into.

## The CLI pipeline

```bash
cp papers.example.json papers.json   # a starting manifest; edit freely
./fetch.py                 # open-access PDFs -> pdfs/ ; anything else -> dropin/
./extract.py               # pdfs/ -> text/*.txt  + a per-paper quality report
./synth.py [--only ID]     # text/ -> cache/<id>/NNNNN.wav   (resumable)
./build.py --m4b           # cache/ -> out/<id>.wav [-> out/audiobook.m4b]
./run_all.sh               # narrate everything, two balanced parallel workers
./workspace.py --status    # what the workspace holds, and where
./lint_audio.py --tier1    # audio quality across the whole chunk cache
```

Useful flags:

```bash
./synth.py --plan                 # chunk counts, no synthesis
./synth.py --voice am_michael     # a different narrator (54 available)
./synth.py --speed 1.15           # faster narration
./build.py --only ID              # reassemble one paper
./workspace.py --export           # rewrite papers.json from the database
tail -f synth.log                 # progress
ls cache/*/*.suspect              # anything the duration check flagged
```

Resume after any interruption by re-running. Cached chunks are skipped.

**`--only` prefers an exact id and only then falls back to substring.**
`--only 01-cook` still works, but `--only cook-notes` no longer also matches
`01-cook-how-complex-systems-fail`. With folder uploads creating many ids at
once, substring-only matching silently re-extracts the wrong papers.

`./extract.py` prints a quality table — pages, keep %, citations stripped,
words, estimated minutes, average and longest sentence. **The longest-sentence
column is the tell:** a 300+ word "sentence" means a table or a code block got
reflowed into prose.

## Audio quality linting

`./lint_audio.py` checks the rendered chunk cache against the text that
produced it. Two tiers, because the cheap one is fast enough to run over
everything and the expensive one is not.

```bash
./lint_audio.py --tier1                 # every chunk, no ML, seconds
./lint_audio.py                         # + ASR round-trip on what Tier 1 flags
./lint_audio.py --sample 400            # ... plus a random clean control group
./lint_audio.py --full --yes            # ASR every chunk (prints the cost first)
./lint_audio.py --estimate              # just the cost of a full sweep
./lint_audio.py --report                # re-print from lint/*.json, measure nothing
./lint_audio.py --only ID --fix         # repair, verify, rebuild out/ID.wav
./lint_audio.py --rescan                # re-measure the audio, keep the transcripts
./lint_audio.py --reclassify            # re-decide every class, measure nothing
./lint_audio.py --refresh-asr           # ignore stored transcripts, transcribe again
```

Transcripts are carried forward between runs and each one records a fingerprint
of the text it was measured against, so re-running is cheap and a repaired
chunk is never judged by a ratio computed against text it no longer speaks.
`--reclassify` re-decides every class from the stored measurements, which is
how a threshold gets re-tuned without spending the 39 minutes again.

**Tier 1** re-derives chunk boundaries with `synth.chunks_for`, so chunk *N*
here is exactly the chunk *N* that was narrated, and measures both sides:

- *audio* — duration against a duration predicted from the source characters
  (`secs = a*chars + b`, fitted per corpus on clean prose and outlier-trimmed),
  chars-per-second, peak, clipping, DC offset, RMS, leading / trailing /
  longest-internal silence, unreadable, truncated and zero-frame files, and
  gaps in the chunk index sequence;
- *source text* — maths-symbol density, digit density, and the fraction of
  tokens a synthesiser can actually pronounce.

That last group is the one that earns its keep. **The dominant defect in a
paper audiobook is not the synthesiser losing its place, it is the extractor
handing it text that cannot be spoken.** eSpeak has no pronunciation for
U+1D70B MATHEMATICAL ITALIC SMALL PI, so it reads the codepoint out: a
transcript of such a chunk begins *"Letter 1D70, Letter 1D703, Letter 1D45C"*.
Two minutes of that is what a listener reports as "it repeats every word
twice". Those chunks are classified `unspeakable`, not `repetition`, because
the two need opposite repairs.

**Tier 2** transcribes with faster-whisper `tiny.en` (int8, CPU) and compares
word sequences. `asr_words / src_words` is the headline: a clean chunk sits at
1.00. A difflib alignment splits it into insertion, deletion and substitution
rates, so "the audio says more than the text" (repetition) is distinguishable
from "the audio says less" (truncation); repeated-4-gram coverage of the
transcript confirms repetition on its own. Note that this ratio is a good
*detector* and a noisy *estimator*: on defective audio the transcript itself is
unstable, and the same chunk can measure 65 or 113 ASR words depending on beam
width. Tier 1's duration ratio has no such variance.

Classes: `ok`, `unspeakable`, `repetition`, `truncation`, `silence`, `level`,
`missing`, and `duration` (a Tier-1 duration anomaly Tier 2 has not
adjudicated). Output is a per-paper table ranked by severity plus
`lint/<id>.json` per paper and `lint/index.json`, which the web UI reads to put
a "N flagged" badge on each card.

Tier 1 alone is a *screen*, not a verdict: on this corpus it flags 544 chunks
of which Tier 2 clears 434, and the cheap candidate set (Tier-1 flags plus any
chunk containing a character eSpeak may not be able to pronounce) is 1004
chunks / 435 min of audio and contains 226 of the 228 chunks a full sweep
finds. The two it misses have clean-looking sources and normal durations;
only the transcript shows them up.

### Repair

**Kokoro is length-deterministic and waveform-nondeterministic.** The same
(text, voice, speed) yields a bit-different waveform every time — the vocoder's
noise source is stochastic — but *exactly* the same number of samples, run
after run. Splitting a chunk into three and synthesising the parts reproduces
the original total duration to within 20 ms. So **a retry cannot fix a bad
chunk and neither can a re-split**: the pathology is in the phoneme sequence,
which is a deterministic function of the text. The only lever is the text.

`--fix` therefore repairs by *cleaning the source of that chunk* — NFKD-folding
(which alone repairs the PDF's `ﬁ`/`ﬂ` ligatures) and dropping tokens no
synthesiser can pronounce — then re-synthesising. It is additive and verified:

- the original is copied to `cache/<id>/.lintbak/NNNNN.wav` before anything is
  written, and an existing backup is never overwritten;
- the replacement must pass **three** independent checks that the original
  failed — speech rate back inside `synth.py`'s own 8–32 chars/s band, ASR
  ratio inside [0.75, 1.35], and zero codepoint-spelling artefacts — otherwise
  the candidate is discarded and the cache is untouched;
- a chunk that passes every check is never replaced, whatever its class;
- `out/<id>.wav` is rebuilt with `build.py --only <id>` for every paper touched;
- every decision is appended to `lint/fixes.jsonl`.

`.suspect` markers are left exactly as `synth.py` wrote them — the linter's
flag set is a strict superset, since it runs the same 8–32 chars/s rule as one
of its checks — except that `--fix` removes the marker of a chunk it repaired
and re-verified, because the marker then asserts something untrue.


## Architecture

    workspace.py     the store: folders, items, and the papers.json view
    webapp.py        FastAPI: library, upload, folders, items, ranged files
    fetch.py         open-access download / drop-in routing
    titles.py        a paper's real title, read off page 1 of its PDF
    extract.py       PDF -> speech-ready text
    synth.py         Kokoro -> cache/<id>/NNNNN.wav   (CPU, resumable)
    build.py         cache -> out/<id>.wav [-> M4B]
    lint_audio.py    chunk cache -> lint/<id>.json    (CPU, two tiers)
    static/index.html            the UI, one self-contained file, no build step
    tests/                       front-end regression tests
    workspace.sqlite             the workspace (folders + items)      [local]
    papers.json                  generated view of it, for the CLI    [local]

### One item, not two

A document is **one thing**. Its PDF, its extracted text, its per-chunk cache
and its narrated WAV are *properties* of that one thing, derived from its id:

    pdfs/<id>.pdf   text/<id>.txt   cache/<id>/NNNNN.wav   out/<id>.wav

The UI shows one card per document, titled by the PDF, with the audio attached
to it — never an `x.pdf` row and a separate `x.wav` row that can drift apart.
An uploaded PDF is titled by the title read off the PDF itself ([Titles](#titles)),
falling back to its filename; rename anything at any time, and the rename wins.

### The folder tree is virtual — and that is a security property

Folders exist **only as rows in `workspace.sqlite`**. A folder name is never
used to build a filesystem path. Files stay flat, keyed by a slugified id. Two
consequences:

1. **A hostile folder name cannot escape anywhere.** Uploading a directory
   called `../../etc` creates a folder *named* `.. etc` and nothing else. Every
   file endpoint resolves a flat `^[A-Za-z0-9._-]+$` basename inside one fixed
   base directory and refuses everything else.
2. **Reorganising moves nothing.** Filing a document into a folder is a
   metadata write, so hours of rendered audio are never at risk from a drag.

### Schema — `workspace.sqlite`

```sql
folders(
  id        INTEGER PRIMARY KEY,      -- 0 is the virtual root; no row exists for it
  parent_id INTEGER NOT NULL DEFAULT 0,
  name      TEXT    NOT NULL,
  created   REAL    NOT NULL)
UNIQUE(parent_id, name)               -- a clash is suffixed "(2)", never an error

items(
  id        TEXT PRIMARY KEY,         -- slug; keys pdfs/, text/, cache/, out/
  folder_id INTEGER NOT NULL DEFAULT 0,
  kind      TEXT NOT NULL,            -- 'paper' | 'attachment'
  name      TEXT NOT NULL,            -- file name shown/saved; a paper's follows its title
  title     TEXT NOT NULL,            -- display name, read off the PDF for uploads
  authors, year, venue, url, access, notes,   -- manifest fields, preserved
  ord       INTEGER NOT NULL,         -- manifest order == M4B chapter order
  stored    TEXT,                     -- attachments: basename under files/
  bytes     INTEGER NOT NULL DEFAULT 0,
  created   REAL NOT NULL,
  deleted   INTEGER NOT NULL DEFAULT 0,   -- archive flag; files are NEVER unlinked
  orig_name TEXT,                     -- the name it was uploaded as (search, undo)
  title_source TEXT NOT NULL DEFAULT '')  -- pending|filename|metadata|layout|manual|manifest

meta(k, v)                            -- schema version, collection title/author
```

`parent_id`/`folder_id` default to `0`, the virtual root, rather than `NULL`, so
that `UNIQUE(parent_id, name)` actually constrains top-level names — SQLite
treats NULLs as distinct.

### `papers.json` is a generated view

The four CLI scripts read `papers.json`, so **it is regenerated from the
database on every mutation**, atomically (`.part` + rename). The CLI therefore
needs to know nothing about the database. Hand-editing `papers.json` still works
*additively*: any id present there and missing from the database is imported into
the root folder on the next start. Rows you delete from the file come back — the
database is the source of truth for what exists.

### Uploading a folder

`<input type="file" webkitdirectory multiple>` for the picker, and
`DataTransferItem.webkitGetAsEntry()` for dragging a folder onto the drop zone
(plain multi-file and single-file drops both still work). Each file's relative
path reaches the server as either a `path` form field immediately **before** its
file part, or as the multipart **filename** itself. Both are accepted; the
explicit field wins. Directory chains are recreated as folder *rows*,
get-or-create, so re-uploading into the same tree does not duplicate it. The
browser sends large trees in batches of 20 files / 200 MB, so one failure cannot
lose the whole upload.

| file | result |
|---|---|
| `.pdf` with a `%PDF-` header | a **document**: stored, [titled from the PDF](#titles), queued for extract → narrate → assemble |
| anything else | an **attachment**: stored under `files/<id><ext>`, listed, downloadable, not renderable |
| `.pdf` with a bad header | **rejected** and reported in `skipped` — never stored under a name that claims to be a PDF |
| over the size cap | rejected and reported |

Caps: 80 MB per PDF, 25 MB per attachment, 400 files and 1024 MB per request,
12 levels of nesting. Uploads stream to `tmp/` with a hard byte cap enforced
*while reading*, so an oversized file never lands anywhere and memory stays
bounded regardless of how big the tree is.

Attachments are served `Content-Type: application/octet-stream`,
`X-Content-Type-Options: nosniff`, `Content-Disposition: attachment` — an
uploaded `.html` or `.svg` can never execute as same-origin script.

### Titles

An upload is named after its file only until its real title has been read off
the PDF: `2602.03249v2.pdf` becomes *Accordion-Thinking: Self-Regulated Step
Summaries for Efficient and Readable LLM Reasoning*, and `Chinchilla.pdf` becomes
*Training Compute-Optimal Large Language Models*. The card title and the file
names a PDF or WAV is shown and saved under follow it (`LoRA: Low-Rank …` is
saved as `LoRA - Low-Rank ….pdf`, legal on every OS). Files on disk stay keyed
by id, so nothing moves.

`titles.py` is local and deterministic — no network, no model:

1. **Typesetting.** The biggest horizontal text in the upper 70% of page 1,
   wrapped lines rejoined. The rotated arXiv side stamp, bare labels
   (*Abstract*, *Article*), drop caps and superscript marks are ignored.
2. **Document metadata**, trusted only when its words are printed at that title
   size. It is often junk (`final1.dvi`, `PLME0208_696-701.indd`), but when it
   checks out it spells the title better than the text layer does (OCR slips,
   ligatures), so it wins.

ALL-CAPS titles are recased from how the paper spells each word elsewhere
(*LORA: LOW-RANK ADAPTATION* → *LoRA: Low-Rank Adaptation*), and a line-end
hyphen is removed only if the paper itself uses the joined word. A title that
runs off the page is incomplete, so it is declined, as is anything implausible.
When nothing is trustworthy the upload keeps its filename — a wrong title is
worse than an ugly one.

Measured on this project's 68-PDF library against hand-checked titles: **66
correct, 2 declined, 0 wrong**, about 15 ms per PDF. The declines are a TeX
Type 3 PDF whose text layer carries no usable font sizes, and a Web of Science
cover page whose title is cut off inside the PDF itself.

Where a title came from is recorded in `title_source`: `pending` (uploaded, not
read yet), `filename` (read, nothing trustworthy), `metadata` / `layout`
(detected), `manual` (renamed by you) or `manifest` (from `papers.json`).
Detection only ever replaces `pending` or `filename`, compare-and-set inside the
write transaction, so **a rename always wins**. The name a paper was uploaded as
is kept in `orig_name`, and search still matches it.

Detection runs as a **subprocess** of the server, because parsing an uploaded
PDF is an attack on MuPDF (see `SECURITY.md`). It runs for each upload batch at
once, for any `pending` row at startup, and before extraction, so narration
speaks the real title.

```bash
./titles.py paper.pdf            # what would be detected, and from which signal
./titles.py --retitle            # dry run over the workspace
./titles.py --retitle --apply    # write it
./titles.py --retitle --retry    # also retry rows where nothing was found
```

**Retitling does not re-narrate.** Existing audio keeps the header it was
rendered with. Chunks are cached by *position*, not content, so a changed title
would misalign every chunk after it. When `extract.py` finds that a paper's text
has changed, it moves `cache/<id>/` aside to `cache/<id>.stale-<time>/`, and the
next render narrates the paper afresh; the old chunks are kept, not deleted. The
header also no longer reads upload placeholders aloud ("By uploaded. Published
in uploaded."). A fully described manifest entry is spoken exactly as before,
so its cache stays valid.

### Deleting is never destructive

- **Delete a folder** → the folder row goes; its subfolders and items move **up**
  to its parent. Nothing is lost.
- **Archive a document** → the row is hidden and dropped from `papers.json`.
  **The PDF, the extracted text, the chunk cache and the WAV all stay on disk.**
  Re-rendering a 90-minute paper costs an hour of CPU; the app will not spend
  that on your behalf. To reclaim the space, remove `pdfs/<id>.pdf`,
  `dropin/<id>.pdf`, `text/<id>.*`, `cache/<id>/`, any `cache/<id>.stale-*/`
  and `out/<id>.wav` by hand.

**Restore path** — the database only ever gains structure; the artefacts are the
truth. Delete `workspace.sqlite`, put your manifest entries back into
`papers.json`, and run `./workspace.py --init`. Every rendered WAV and cached
chunk is still on disk under the same ids, so the library comes straight back.

## Extraction quality — the rules that were paid for

**The hard part is not the TTS.** A two-column paper extracted naively gives
interleaved columns, running heads on every page, unlistenable inline citations,
and a references section that is 30–40% of the file. `extract.py` handles
column-ordered extraction, repeated header/footer detection, de-hyphenation
across line breaks, citation stripping (parenthetical, bracketed and narrative
forms), figure and table captions, equation lines, and cutting at
References/Appendix.

Four rules in there look arbitrary. Each replaced something that produced
unlistenable audio, and each is worth keeping:

- **The reference cut uses an absolute page floor, not a page fraction.** One
  118-page paper in the test set starts its references on page 9 — so a
  percentage rule swallowed the entire appendix and turned a 34-minute paper
  into 4.4 hours of agent-trajectory dumps. The rule is `page_index >= 2 and
  chars_seen > 4000`.

- **Sentences are packed up to `MAX_CHARS` *across* paragraph boundaries.**
  Packing within each paragraph fragments formal papers badly: one paper came
  out as 971 chunks averaging ~15 words, where another packed across paragraphs
  gave 355 chunks of ~44. Short chunks are choppy to listen to and much slower
  to render.

- **Every chunk gets a duration sanity check.** Characters-per-second outside
  8–32 is flagged, written beside the chunk as `cache/<id>/NNNNN.suspect`, and
  reported. This catches a silent or runaway chunk before you meet it at 70 mph.

- **Loudness is normalised to a fixed RMS target.** A paper that renders 6 dB
  quieter than the last one is unlistenable against road noise.

Plus **missing-chunk detection** at assembly: gaps in the index sequence are
listed rather than silently concatenated over.

### Known defect: `sort=True` scrambles some two-column layouts

`extract.py` calls `page.get_text("text", sort=True)`. That flag sorts text
blocks by vertical then horizontal position across the *whole page* — which on
a dense two-column paper interleaves the two columns line by line instead of
reading one column and then the other. The output is not "slightly worse text";
it is two columns spliced into sentences that were never written.

Measured across this project's test corpus, scoring how often a line-final
hyphen is completed by the token that follows it (it should be; if the columns
are interleaved it is not):

| paper | `sort=True` | `sort=False` |
|---|---:|---:|
| dense two-column ACM paper | **5.8%** | **72.4%** |
| five other papers (one- and two-column) | within 5 points either way, one favouring `sort=True` | |

**This is flagged, not fixed.** The effect is layout-dependent — `sort=False` is
not a blanket improvement — and changing extraction means re-narrating every
affected paper, which is hours of CPU. Decide it per corpus. If your papers are
dense two-column conference format, compare both settings on one file before
you render a library.

(If you inherited an audio library rendered before this was known, the papers
worth re-checking are the two-column ones.)

## Why Kokoro, and why the CPU

The obvious choice for TTS is whatever gives the best single-utterance quality,
usually an autoregressive model with voice cloning. For **batch long-form** that
is the wrong trade, on three axes:

| | autoregressive TTS | Kokoro-82M (this) |
|---|---|---|
| throughput | ~2.3× realtime, GPU | **3.3–3.7× realtime, CPU only** (measured) |
| hardware | needs a GPU to itself | **none** — CPU |
| long-form | documented hallucination past ~350 characters; drift over long runs | non-autoregressive, no drift |

The hardware point is the decisive one. A machine whose GPU is busy with
something else — a local LLM, training, a game — has no VRAM to spare, and TTS
that demands the GPU means choosing between the audiobook and the other
workload. On CPU the whole library renders in the background at 3.3–3.7×
realtime while the GPU keeps doing its job. An 82M-parameter non-autoregressive
model is small enough that this is not a sacrifice.

**This is a design constraint, not a default.** `synth.py` pins
`KPipeline(device="cpu")` and the systemd template sets `CUDA_VISIBLE_DEVICES=`,
so a render can never quietly take the GPU.

## The front-end invariant

Rebuilding the library with `innerHTML` on every poll tore the live `<audio>`
element out of the DOM and stopped playback after two or three seconds. This is
the bug the entire front end is shaped around. The UI therefore:

- creates each `<audio>` **exactly once**, in `attachAudio()`, guarded by
  `!slot.querySelector('audio')`, and patches text in place with `setText()`;
- **hides** cards instead of detaching them when you switch folders — a hidden
  `<audio>` keeps playing, a removed one does not;
- **reorders with CSS `order`** on a flex column, so a card's DOM node is
  appended once and never moved again (moving a node containing a playing media
  element is the same hazard);
- refuses to reap a card that has vanished from the payload **while its audio is
  playing**.

The only `innerHTML` assignment in the page builds a fresh, detached card
skeleton before any `<audio>` exists. **`#lib` is never assigned `innerHTML`.**

Two tests hold the line:

```bash
npm i                          # jsdom, test-only
npm test
```

`tests/dom_playback.test.js` asserts all of the above against a real DOM — 25
checks covering poll, folder switch, reorder, item disappearance, new items and
resume position. `tests/check_page_script.js` needs no dependencies at all: it
syntax-checks the inline page script and statically catches a reintroduced
`#lib.innerHTML`. `tests/dom_folders.test.js` covers making folders by hand
(the toolbar button, a tree row's **+**, devices with no hover) and search by
upload name.

The Python side — title detection, title provenance, the stale-cache guard —
has its own test, which synthesises its PDFs and needs no corpus:

```bash
./venv/bin/python tests/test_titles.py
```

## HTTP API

| method | path | |
|---|---|---|
| GET | `/api/library` | folders + items + jobs + totals |
| POST | `/api/upload` | one file, many files, or a whole tree |
| POST | `/api/folders` | `{name, parent_id}` |
| PATCH | `/api/folders/{id}` | `{name?, parent_id?}` — refuses cycles |
| DELETE | `/api/folders/{id}` | children move up |
| PATCH | `/api/items/{id}` | `{folder_id?, title?}` — move / rename; a paper's file name follows its title |
| DELETE | `/api/items/{id}` | archive (non-destructive) |
| POST | `/api/items/{id}/render` | (re-)queue narration, e.g. after a drop-in |
| GET | `/audio/{id}.wav` | Range-aware; saved under the paper's title |
| GET | `/pdf/{id}.pdf` | Range-aware, `nosniff`; saved under the paper's title |
| GET | `/text/{id}.txt` | the speech-ready text |
| GET | `/file/{stored}` | attachment, forced download |
| GET | `/m4b` | if built |

`/api/library` also carries a deprecated flat `papers` array in an older shape,
so anything polling the pre-workspace API keeps working.

### Environment variables

| | default | |
|---|---|---|
| `PA_HOST` | `127.0.0.1` | bind address. Read `SECURITY.md` before changing it. |
| `PA_PORT` | `3002` | port |
| `PA_SOURCE_URL` | *(unset)* | where your copy of the source lives. Shown as a link in the UI footer — this is the AGPL §13 source offer; set it if remote users use your instance. |
| `CUDA_VISIBLE_DEVICES` | — | set to empty to guarantee CPU-only |
| `OMP_NUM_THREADS` | — | synthesis threads |
| `HF_HOME` | — | where model weights are cached |

## Sources and copyright

**This project ships the pipeline, never a corpus.** No paper, no extracted
text and no generated audio is distributed with it. `pdfs/`, `dropin/`,
`text/`, `cache/`, `out/` and `files/` are all in `.gitignore` and must stay
there.

`fetch.py` downloads **only openly published copies** — arXiv, JAIR, author and
course pages — from URLs listed in your own manifest. It does not scrape,
does not go through paywalls, and does not attempt to defeat access controls.
Anything not openly available you supply yourself, from a copy you lawfully
have: drop it in as `dropin/<id>.pdf` and re-run `./fetch.py`, or just drag it
into the web UI.

`papers.example.json` is a manifest of **bibliographic metadata only** — titles,
authors, venues, and public URLs for the openly available ones. Copy it to
`papers.json` and edit it into your own reading list.

What you do with the audio is between you and the copyright holder. Making an
audio version of a paper for your own use is one thing; redistributing it is
another, and this tool does not help you with the second.

## Security

**Read [`SECURITY.md`](SECURITY.md) before exposing the web UI to anything.**
Short version: it is unauthenticated, it accepts uploads, an upload starts a
subprocess pipeline, and there is no sandboxing. Bound to `127.0.0.1` — the
default — that is fine. On `0.0.0.0` it is a deliberate decision you should make
with the threat model in front of you, and never for the open internet.

## Limitations

- **English only in practice.** Kokoro supports other languages, but the
  extraction heuristics, the speech normalisation table and the sentence
  splitter are all English-shaped.
- **Extraction is heuristic, and reading order is the weak point.** See the
  known `sort=True` defect above. Heavily tabular
  papers, slide decks, scanned images (no OCR) and mathematics-dense text do
  not. Check the quality table.
- **No OCR.** A scanned PDF with no text layer yields nothing.
- **Storage is uncompressed.** Roughly 170 MB per hour of audio in `out/`, plus
  a similar amount in `cache/` until you clear it. The M4B is the compressed
  artefact, and it needs `ffmpeg`.
- **One render at a time in the web UI.** A single worker thread drains the
  queue, deliberately — parallel renders on the same CPU are slower overall.
- **No authentication, ever.** It is a single-user tool. See `SECURITY.md`.
- **Archiving does not free disk.** By design; delete the artefacts by hand.

## Licence

**[AGPL-3.0-or-later](LICENSE).**

This is forced by the dependency set, not chosen for ideology. Three separate
triggers, any one of which would be sufficient on its own:

1. **`pymupdf` 1.28.2** — its own metadata reads `Dual Licensed - GNU AFFERO
   GPL 3.0 or Artifex Commercial License`. Absent a paid Artifex licence, the
   AGPL applies to the combined work. And because `webapp.py` is a network
   service, **AGPL §13 (remote network interaction)** is engaged: running a
   modified version for remote users obliges you to offer them its source.
   `PA_SOURCE_URL` exists for exactly that.
2. **`phonemizer-fork` 3.3.2 (GPL-3.0-or-later)** — hard-required by
   `misaki[en]`, which `kokoro` hard-requires, and imported unconditionally at
   `kokoro/pipeline.py:5`.
3. **bundled eSpeak NG (GPL-3.0-or-later)**, shipped as `libespeak-ng.so`
   inside `espeakng-loader` and bound at import time.

Apache-2.0, MIT, BSD and ISC dependencies combine into an (A)GPLv3 work without
difficulty; the compatibility runs that way and not the other.

> **For the owner / anyone forking this: the alternative is analysed, not
> taken.** Replacing PyMuPDF with `pypdfium2` (BSD-3/Apache-2.0), `pypdf`
> (BSD-3), `pdfminer.six` (MIT) or `pdfplumber` (MIT) is a **four-line** change
> to `extract.py` — the whole PyMuPDF surface is `import`, `open()`,
> `page_count` and one `get_text()` call. But be clear about what it buys:
>
> - it does **not** make a permissive licence possible, because triggers 2 and
>   3 live in the TTS stack. The result would be **GPL-3.0-or-later**;
> - what it *does* remove is the AGPL network clause, which is the part most
>   people actually object to;
> - the usual argument for staying — that `sort=True` provides reading order
>   nothing else can — **did not survive measurement**. See the known defect
>   above. On this corpus the flag is neutral on five papers and destructive on
>   one.
>
> A genuinely permissive release would additionally mean replacing Kokoro's
> English G2P, which costs roughly 5% of words (they are silently skipped) and
> changes how the audio sounds.
> [`THIRD_PARTY_LICENCES.md`](THIRD_PARTY_LICENCES.md) sets out the evidence and
> the four real options. **That call belongs to the project owner, and nothing
> here has been swapped.**

Every dependency's licence and version is enumerated in
[`THIRD_PARTY_LICENCES.md`](THIRD_PARTY_LICENCES.md).

Contributions: see [`CONTRIBUTING.md`](CONTRIBUTING.md).
# paper-reader-tts
