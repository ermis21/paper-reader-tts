# Design: Synchronized PDF Reader + Margin Notes

Status: PROPOSED — 2026-09-13
Scope: **core only** — synced highlight, click-to-seek, anchored margin notes.
No note export, no speed control, no resume-position in this round.

## 1. What we're building

A **reader page** (`/reader/{paper_id}`) where you listen to a paper's narration
while looking at its **original PDF pages**. The sentence-group currently being
spoken is highlighted on the page and the view follows along. You can click any
highlightable passage to jump the audio there, and you can pin **notes** into a
margin gutter, anchored to the passage they refer to.

Decisions locked in with the user:

| Question | Decision |
|---|---|
| Highlight granularity | **Chunk-level** (sentence groups, 1–3 sentences) — free from the existing chunk cache; word-level karaoke explicitly deferred, but the data model must not preclude it |
| Reading surface | **Original PDF pages** (rendered client-side), *not* the narration text |
| Note anchors | **Narration-text chunks** (stable index + quote snippet), displayed in the PDF margin |
| Extras | None this round |

The fundamental tension in this design: the audio narrates
`text/{id}.txt` — extracted, de-citationed, speech-normalised text — while the
user reads the raw PDF. Bridging the two is the technical core of this feature
(§4). Everything else is ordinary web work.

## 2. Architecture at a glance

```
 pdfs/{id}.pdf ──┐
                 ├─► align.py ─► cache/{id}/align.json ─┐
 text/{id}.txt ──┤   (offline, per paper, cached)       │
                 │                                      ├─► /api/papers/{id}/alignment
 cache/{id}/*.wav┘   (durations → timeline)             │
                                                        │
 sqlite workspace ── notes table ─► /api/papers/{id}/notes (CRUD)
                                                        │
                 static/reader.html ◄───────────────────┘
                   PDF.js pages + highlight overlay + audio + margin notes
```

- **One new offline script**: `align.py`. No changes to `extract.py` /
  `synth.py` / `build.py` outputs — alignment is a pure consumer of existing
  artifacts. Re-running it never invalidates audio cache.
- **One new page**: `static/reader.html`. `static/index.html` (the library) is
  untouched except for a "Read" link per card. The front-end invariant
  (never rebuild DOM around a live `<audio>`) is trivially honoured: the
  reader's `<audio>` lives in a fixed player bar that is created once and
  never moved.
- **One new table**: `notes` in `workspace.sqlite` (via `workspace._migrate`).
- **Four new endpoints** (§5).

## 3. Data model

### 3.1 `cache/{id}/align.json` (generated, cacheable)

Lives in the chunk cache directory **on purpose**: `extract.py`'s
`retire_stale_cache()` already moves `cache/{id}/` aside when the text changes,
so a stale alignment can never silently survive a re-extraction.

```json
{
  "version": 1,
  "text_sha1": "…",                 // of text/{id}.txt — belt-and-braces staleness check
  "built_at": "2026-09-13T12:00:00",
  "gap_seconds": 0.18,              // copied from build.py GAP
  "chunks": [
    {
      "i": 0,
      "char_start": 0, "char_end": 87,      // into text/{id}.txt
      "t_start": 0.0, "t_end": 7.42,        // seconds into out/{id}.wav, gap-aware
      "rects": [                            // where this chunk lives on the PDF
        {"page": 0, "x0": 56.7, "y0": 102.3, "x1": 300.1, "y1": 188.9}
      ],
      "conf": 0.97                          // token-match confidence, 0..1
    }
  ]
}
```

Notes on the shape:

- `rects` is a **list**: a chunk can cross a page or column break. Each rect is
  a per-page bounding union in PDF user-space points (origin bottom-left,
  PyMuPDF native — the front end flips to top-left once, at render time).
- `conf < CONF_PAGE_ONLY` (proposed 0.6) → front end highlights **the whole
  page** faintly instead of a wrong rect. Wrong-position highlights are worse
  than coarse ones.
- Future word-level karaoke slots in as an optional `"words": [...]` array per
  chunk without breaking version 1 consumers. (`version` field exists for
  this.)

### 3.2 `margin_notes` table (workspace.sqlite)

```sql
CREATE TABLE IF NOT EXISTS margin_notes(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  pid        TEXT NOT NULL,             -- items.id, deliberately NOT a foreign key
  chunk_idx  INTEGER NOT NULL,          -- anchor: narration chunk
  char_off   INTEGER NOT NULL DEFAULT 0,
  quote      TEXT NOT NULL,             -- first ~120 chars of the anchored chunk
  body       TEXT NOT NULL,
  created    REAL NOT NULL,             -- epoch seconds (house style)
  updated    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS margin_notes_pid ON margin_notes(pid, chunk_idx);
```

- Named `margin_notes`, not `notes`: `items.notes` already exists as a free-text
  column.
- **No FK / no cascade**: items are soft-deleted (`deleted=1`, files never
  unlinked), so notes survive archiving and return on restore.
- `quote` is the re-anchoring lifeline: if a paper is re-extracted and chunk
  indices shift, `reanchor_notes(pid, mapping)` repairs `chunk_idx` by
  fuzzy-finding `quote` in the new chunk stream. (Re-anchoring tool: phase 3.)

### Implementation notes (as built, Phase 1)

- The two-pointer walk became **difflib.SequenceMatcher** on skeleton-token
  streams: its matching blocks are monotonic in both sequences (no backwards
  jumps), tolerate arbitrary haystack skips (citations, captions, heads), and
  cost a few unmatched needle tokens around count-changing speech
  substitutions instead of cascade drift. Measured on the full 68-paper
  corpus: ~0.2–0.9 s per paper, 51/8862 chunks below the 0.6 page-fallback
  (table dumps, interleaved figure text, spoken headers) — exactly the
  pathological set predicted.
- Chunks with no rects at all get `page_only` interpolated from the nearest
  alignable neighbour, so follow-mode never loses the page.
- `synth.chunks_for` is now a thin wrapper over `synth.chunk_spans` (char
  offsets); byte-identical output verified over every extracted paper.
- Alignment endpoint: `GET /api/papers/{pid}/alignment` builds on demand when
  missing, text-sha1-stale, version-stale, or audio has since completed.
- Notes endpoints: `GET/POST /api/papers/{pid}/notes`, `PATCH/DELETE
  /api/notes/{nid}`; `quote` is populated server-side from the chunk text.

## 4. The alignment engine (`align.py`) — the hard part

Input: `pdfs/{id}.pdf`, `text/{id}.txt`, `cache/{id}/*.wav`.
Output: `cache/{id}/align.json`. Runtime: seconds per paper (no ML, no TTS).

### Step 1 — PDF word map

`page.get_text("words")` gives `(x0, y0, x1, y1, word, block, line, word_no)`
per word. We keep words in **reading order** using the same `sort=True`
semantics `extract.py` relies on (sort blocks by (y, x) — yes, this inherits
the documented two-column interleaving defect; see §8 risks). Each word gets a
stable ordinal `w`.

### Step 2 — Normalisation equivalence

The narration text was produced from the PDF text by `extract.py`'s pipeline:
running heads/page numbers removed, captions & math lines dropped, references
cut, citations/URLs stripped, dehyphenation, `SPEECH` substitutions
(`et al.` → "and colleagues", `%` → " percent", `&` → " and ", …).

We do **not** try to invert that pipeline. Instead both streams are reduced to
a common **skeleton token** form:

```
skeleton(tok) = lowercase, strip all non-alphanumeric, fold digits
```

and alignment runs with a **monotonic two-pointer walk**:

- Narration tokens are the *needle*: every one must be accounted for.
- PDF words are the *haystack*: arbitrarily skippable (heads, captions,
  citations, math, references — all the stuff extraction deleted).
- At each step, try to match the next narration token against PDF words within
  a lookahead window `W` (proposed 60 words — large enough to jump a stripped
  parenthetical citation, small enough to prevent cross-paragraph drift).
- On match: advance both. On repeated failure (no match within `W`): emit the
  narration token as *unmatched*, advance needle only (drops confidence).
- Guard against the classic failure — **repeated phrases** ("the the",
  section titles repeated in body): prefer the match that keeps the haystack
  pointer moving the least (nearest match), and never let the pointer move
  backwards.

Speech substitutions that change token *count* (`et al.` 2 tokens →
"and colleagues" 3) are handled by a small **equivalence table** applied to the
skeleton stream on the narration side (expand before walking) plus
many-to-one matching allowed within a 4-token window. This is the fiddliest
part; it is also fully unit-testable on synthetic strings before ever touching
a real PDF.

### Step 3 — Chunk mapping

`chunks_for()` from `synth.py` is deterministic. We refactor it (importably,
no behaviour change) to also yield character offsets, giving each chunk its
`char_start/char_end` into `text/{id}.txt` and therefore its span of narration
tokens. Each matched narration token knows its PDF word → page + bbox. A
chunk's `rects` = per-page union of its words' line bboxes (line-level union,
not word-level — word rects look jittery; line rects read as a clean
highlighter stroke). `conf` = matched tokens / total tokens.

### Step 4 — Timeline

Chunk durations come from the cached WAV headers (24 kHz mono — frames / SR is
exact). `build.py` concatenates with `GAP = 0.18 s` between chunks and applies
gain-only normalisation (durations unchanged), so:

```
t_start(i) = Σ dur(0..i-1) + GAP · i
t_end(i)   = t_start(i) + dur(i)
```

`gap_seconds` is read from `build.py` by import, not copy-pasted, so a future
retune can't desynchronise the reader.

If a paper has no audio yet (text extracted, not narrated), `align.json` is
still built with `t_start/t_end = null`: notes work immediately, highlighting
activates once audio exists. The webapp regenerates timings on demand.

### Step 5 — Confidence report

Per paper, print: chunks matched ≥0.95 / ≥0.6 / below, and the pages where
low-confidence chunks cluster. This becomes a lint row, in the spirit of
`lint_audio.py` — alignment quality is checkable without opening the UI.

## 5. Backend additions (webapp.py + workspace.py)

| Endpoint | Purpose |
|---|---|
| `GET /reader/{pid}` | Serve `static/reader.html` |
| `GET /api/papers/{pid}/alignment` | Serve `cache/{pid}/align.json`; build it on demand if missing or `text_sha1` stale (synchronous — seconds; acceptable, and idempotent) |
| `GET /api/papers/{pid}/notes` | List notes, ordered by `chunk_idx` |
| `POST /api/papers/{pid}/notes` | Create `{chunk_idx, char_off, body}` → fills `quote` server-side from the chunk text |
| `PATCH /api/notes/{nid}` / `DELETE /api/notes/{nid}` | Edit / remove |

Existing `/audio/{name}` (already ranged — seeking works) and `/pdf/{name}`
(served with nosniff, range-capable — PDF.js can stream it) need **no changes**.

`workspace.py`: `notes` table in `_migrate`, plus `add_note / list_notes /
update_note / delete_note / reanchor_notes(pid, mapping)` helpers, mirroring
the existing folder/item helper style. All queries parameterised (the file
already does this consistently).

The library card in `index.html` gains one link: `Read →` beside the existing
controls, opening `/reader/{pid}` in a new tab. One-line change, no
restructure.

## 6. Reader front-end (`static/reader.html`)

A second single-file page, same house style as `index.html` (inline
`<script>`/`<style>`, no build step, theme-aware via the same `data-theme`
attribute). One new vendored asset: **PDF.js** (Apache-2.0 — compatible with
the project's AGPL; noted in `THIRD_PARTY_LICENCES.md`). The browser's native
PDF viewer (iframe) was rejected: no overlay, no hit-testing, no control.

### Layout

```
┌──────────────────────────────────────────────────────────┐
│ ◄ Back   Title…        ▸ player bar (fixed)   follow: on │  ← <audio> lives here, created once
├───────────────────────────────────────────────┬──────────┤
│                                               │ ░ margin │
│            PDF pages (PDF.js canvas)          │ ░ gutter │
│            + highlight overlay divs           │ ░ notes  │
│                                               │ ░        │
└───────────────────────────────────────────────┴──────────┘
```

### Rendering & performance

- Pages render **lazily**: a page's canvas is only rendered when near the
  viewport (IntersectionObserver), and evicted far outside it. A 118-page
  paper must not materialise 118 canvases. Placeholder divs hold the scroll
  height (page dimensions from the first `getPage` — uniform in practice,
  verified per document).
- The highlight overlay is a per-page absolutely-positioned `<div>` layer
  above the canvas (below nothing interactive — `pointer-events: none` on
  highlights; clicks handled on the page container).
- Rect transform: PDF user-space (bottom-left origin) → CSS px (top-left):
  `y_css = (page_height − y1) · scale`, computed once per rect at paint time.

### Sync behaviour

- `timeupdate` on the audio (throttled to ~4 Hz) → binary search
  `chunks[].t_start` → current chunk → paint its `rects`; unpaint previous.
  Highlight = translucent accent fill (theme-aware, respects
  `prefers-reduced-motion` for the scroll animation).
- **Follow mode** (toggle, default on): when the current chunk's page differs
  from the viewport, smooth-scroll the rect into view, centred at 40% height.
  Any manual wheel/touchscroll pauses follow for 8 s (standard read-along
  etiquette — fighting the user's scroll is the #1 way these UIs annoy).
- **Click-to-seek**: click on a page → hit-test against chunk rects on that
  page (rects precomputed in CSS px at current scale) → `audio.currentTime =
  t_start`. A low-confidence chunk (page-level) seeks to its chunk too — the
  whole page is its hitbox.
- Chunk crossing page turns Just Works because `rects` is a list.

### Notes UI

- The margin gutter lists notes as cards, vertically positioned to track
  their anchor rect's page+y (CSS `order` + per-page grouping; absolute
  positioning would collide on dense pages — stacking in anchor order is
  honest and simple).
- Add: `+` button appears in the gutter when a chunk is selected (click a
  highlight rect — same hit-test as seek, but on the small "note" handle that
  appears beside the current selection; click page = seek, click handle =
  note). Editor is a textarea in the gutter; `Ctrl/Cmd+Enter` saves.
- Notes render Markdown-lite? **No** — core scope, plain text, `white-space:
  pre-wrap`. (Recorded as a non-goal.)
- Offline/edit conflicts: last-write-wins with `updated` timestamp displayed;
  single-user local app, no locking theatre.

### The invariant, transplanted

The reader page obeys the same front-end invariant as the library: the
`<audio>` element is created **exactly once**, in the fixed bar, and no
container between it and `<body>` is ever rebuilt. Page canvases and overlays
live in a sibling subtree that may be torn down freely. The existing
`tests/check_page_script.js` pattern is extended to statically assert this for
`reader.html` too.

## 7. Lifecycle & invalidation

| Event | Consequence |
|---|---|
| Re-extract changes `text/{id}.txt` | `retire_stale_cache()` moves `cache/{id}/` (and `align.json` with it) aside; alignment rebuilt on next reader open. Notes survive in sqlite and are re-anchored by `quote` (phase 3 tool) |
| Re-synth (new voice/speed), same text | Durations change → alignment timings stale. `align.py` rebuilds in seconds on next open (WAV mtimes vs `built_at` checked by the endpoint) |
| Paper deleted | `ON DELETE CASCADE` removes notes |
| align.py logic improves | Bump `version`; endpoint rebuilds on version mismatch |

## 8. Risks & honest limitations

1. **Two-column sort defect inheritance.** Alignment walks the PDF in the same
   `sort=True` order extraction used, so the known interleaving defect
   (README, "Extraction quality") mostly *cancels out* — both sides are wrong
   the same way. Where it doesn't cancel, `conf` drops and we degrade to
   page-level highlight. This is why the confidence escape hatch is a
   first-class part of the design, not an afterthought.
2. **Heavily-transformed regions.** Reference-adjacent paragraphs and
   caption-dense figure sections will have the lowest `conf`. Expected, bounded,
   visible in the lint report.
3. **PDF.js vendoring.** ~1 MB of static assets. Acceptable for a local app;
   pinned version, integrity noted in `THIRD_PARTY_LICENCES.md`.
4. **Long-paper memory.** Lazy page rendering is mandatory, not optional
   (118-page paper exists in this corpus).
5. **Gap drift.** `t_start` math assumes `build.py`'s exact GAP and no
   intro/outro padding. Verified against `build.py` source at implementation
   time; the alignment stores `gap_seconds` so any future change is explicit.

## 9. Testing

- `tests/test_align.py` (pytest): skeleton-tokeniser unit tests; synthetic
  two-pointer alignment cases (citation skip, `et al.` expansion, repeated
  phrase, page break mid-chunk); golden run on one real paper asserting
  `conf ≥ 0.95` for ≥90% of chunks (threshold tuned to corpus reality on first
  run, then locked).
- `tests/dom_reader.test.js` (jsdom, mirrors `dom_playback.test.js`):
  timeupdate→highlight mapping with a stub alignment, click-to-seek hit-test,
  follow-mode pause on user scroll, audio element never re-created.
- `tests/check_page_script.js`: extended to `reader.html` (syntax + no
  `innerHTML` on containers holding `<audio>`).
- Notes API: pytest over FastAPI TestClient — CRUD, cascade delete, quote
  population.

## 10. Phased implementation

- **Phase 1 — Alignment engine + API skeleton.** `align.py` (+ `chunks_for`
  offset refactor), `notes` migration, alignment endpoint, notes CRUD, pytest
  suite. No UI. Verifiable from the terminal.
- **Phase 2 — Reader page.** PDF.js vendoring, lazy pages, overlay highlight,
  follow mode, click-to-seek, "Read →" link on library cards.
- **Phase 3 — Notes UI + re-anchoring.** Margin gutter, editor, and the
  `quote`-based re-anchor pass for re-extracted papers.
- **Phase 4 — Hardening.** jsdom tests, lint row for alignment confidence,
  README/THIRD_PARTY_LICENCES updates.

## Addendum: Alignment v2 (highlighting overhaul, 2026-09-14)

The v1 chunk-level highlight sat static for a median 22.8 s per chunk, and
its block-merged rects painted over citation-stripped lines. Version 2:

- Each chunk carries **`segs`**: sentence sub-segments with their own char
  range, per-line rects, confidence and `page_only` fallback. The reader
  interpolates position within a chunk by **character fraction** (`f0`/`f1`)
  — Kokoro's pace within a chunk is steady enough that the highlight moves
  sentence-by-sentence with the voice. No re-synthesis, no per-word audio
  timestamps.
- Rects are **per-line, coverage-filtered** (`COVER_MIN = 0.35`): a PDF line
  that is mostly a stripped citation earns no rect, and kept lines union only
  their *matched* words' boxes. No cross-line merging — highlight blocks and
  citation holes read exactly as they sound.
- Click-to-seek targets the estimated **sentence** start (`t_start + f0·dur`).
- Chunk-level `rects`/`conf`/`page_only` remain for note anchors and the
  gutter; the endpoint rebuilds v1 files automatically on version mismatch.
