# Third-party licences

Every package a normal install of this project pulls in, with the licence its
own metadata declares. This file is the evidence for the project's own licence,
so it is not decoration: **adding a dependency means adding a row here.**

Read alongside `LICENSE` (the full AGPL-3.0 text) and the "Licence" section of
`README.md`.

---

## Conclusion first: why this project is AGPL-3.0-or-later

Not a preference — the dependency set leaves no other lawful option, and there
are **three independent copyleft triggers**, any one of which is sufficient.
This matters, because the obvious "just swap the PDF library" fix removes only
the first of them.

### 1. PyMuPDF is AGPL-3.0-or-later

Straight from the installed metadata:

```
$ grep -E '^(Name|Version|License):' venv/lib/.../pymupdf-1.28.2.dist-info/METADATA
Name: pymupdf
Version: 1.28.2
License: Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License

$ cat venv/lib/.../pymupdf-1.28.2.dist-info/COPYING
Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License
```

"Dual licensed" here means *you choose one*: the AGPL, or a paid commercial
licence from Artifex. There is no third option in which you use PyMuPDF and
ship a permissive licence. Choosing the AGPL means the combined work — this
project — is AGPL.

And this project is precisely the case the AGPL was written for. `webapp.py` is
a network service: users interact with it remotely over HTTP. **AGPL section 13
("Remote Network Interaction")** therefore applies, and merely *running* the
modified program for remote users triggers the obligation to offer them its
source — no distribution of files required. That is why `PA_SOURCE_URL` exists
and why the UI footer carries a source link when it is set.

### 2. Kokoro's English path loads `phonemizer-fork`, which is GPL-3.0-or-later

```
Name: phonemizer-fork
Version: 3.3.2
Classifier: License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)
```

This is not optional and not avoidable by configuration. `kokoro` requires
`misaki[en]`; that extra **hard-requires** `phonemizer-fork` and
`espeakng-loader`. And the import is unconditional — `kokoro/pipeline.py` line 5
is `from misaki import en, espeak`, and `misaki/espeak.py` imports `phonemizer`
at module scope. Verified by running it:

```
$ python -c "import sys, kokoro; print('phonemizer' in sys.modules)"
True
```

For English (`lang_code='a'`, what this project uses) `KPipeline.__init__` then
constructs `espeak.EspeakFallback(...)` and passes it into `en.G2P(...)` as the
out-of-dictionary fallback. GPL-3.0 code is loaded into the process on every
run, on the default path.

### 3. `espeakng-loader` ships espeak-ng itself

The wheel declares no licence of its own, but it contains the binary:

```
espeakng_loader/libespeak-ng.so.1.52.0     (21 MB with espeak-ng-data/)
```

eSpeak NG is GPL-3.0-or-later. `misaki/espeak.py` binds it at import time via
`EspeakWrapper.set_library(espeakng_loader.get_library_path())`.

Also present: `num2words` (LGPL-2.1), pulled by the same `misaki[en]` extra.

### What follows

- **The project is `AGPL-3.0-or-later`.** That grant is for *this project's own
  source*. Note an inconsistency upstream: PyMuPDF's documentation says "AGPL"
  and links the AGPL-3.0 text without saying "or later", and its
  `src/__init__.py` even carries `SPDX-License-Identifier: GPL-3.0-only`. Nobody
  can grant later-version rights over code they do not own, so **treat the
  combined work as distributable under AGPL-3.0** and read the "-or-later" as
  applying to the parts this project wrote.
- GPL-3.0 §13 expressly permits
  combining GPLv3 code with AGPLv3 code, so triggers 2 and 3 sit inside an
  AGPL-3.0 work without conflict, and PyMuPDF's AGPL option governs the whole.
- **Apache-2.0, MIT, BSD and ISC dependencies are fine.** They are one-way
  compatible with (A)GPLv3: permissive code may be combined into a GPLv3/AGPLv3
  work, not the reverse. Apache-2.0 specifically is GPLv3-compatible (it is not
  GPLv2-compatible, which is why the version matters).
- **Swapping the PDF library alone does NOT buy a permissive licence.** See the
  next section. This is the single most important thing to understand before
  anyone spends a weekend on it.

---

## The alternative the owner should decide on: replacing PyMuPDF

**This is a decision for the project owner, not one made here. Nothing has been
swapped.** What follows is the analysis needed to make it.

### How much code depends on PyMuPDF: four lines

The entire API surface, in `extract.py`:

```python
import pymupdf                                   # line 17
doc = pymupdf.open(pdf_path)                     # line 88
stats["pages"] = doc.page_count                  # line 89
pages.append(pg.get_text("text", sort=True))     # line 93
```

Everything else in the 200-line extractor — running-head detection,
de-hyphenation, citation stripping, the reference cut, the caption and
equation filters, speech normalisation — is plain Python over a list of
per-page strings. **The mechanical effort of the swap is an afternoon, and most
of it is testing.**

### Why it is still not a small change

The one call that matters is `get_text("text", sort=True)`. That `sort=True` is
doing the single hardest job in the pipeline: **putting text blocks into human
reading order**, which on a two-column academic paper means emitting the whole
left column before the right, rather than interleaving them line by line.
Everything downstream assumes it worked. If reading order degrades, the output
is not "slightly worse text" — it is two columns spliced together into
sentences that do not exist, which the citation stripper and sentence packer
will then happily narrate at you for ninety minutes.

The candidates, all permissive:

| library | licence | reading-order story |
|---|---|---|
| `pypdf` | BSD-3-Clause | Extracts in content-stream order. No block sorting; two-column handling is left to the caller (there are `visitor_text`/layout-mode options, but no equivalent of `sort=True`). |
| `pdfminer.six` | MIT | Has a real layout analysis engine (`LAParams`, `LTTextBox`) and can group text into boxes. Column ordering is achievable but is a tuning exercise per document class, not a flag. |
| `pdfplumber` | MIT | Built on `pdfminer.six`; adds word/table extraction and cropping. Column order can be done by cropping the page into columns, which means detecting the columns first. |

A fourth candidate is worth naming because it measured best of the permissive
options in published comparisons: **`pypdfium2`** (BSD-3-Clause / Apache-2.0,
wrapping Google's PDFium). Independent benchmarks — the `py-pdf/benchmarks`
Levenshtein suite and the DocLayNet "Scientific" subset in arXiv:2410.09871 —
put it level with or ahead of PyMuPDF on order-sensitive metrics (97% / 0.800
vs PyMuPDF's 96% / 0.809), at comparable speed. Note those benchmarks run
PyMuPDF *without* `sort=True`.

So the swap trades **one flag** for **a column-detection problem you now own**.
That is the real cost, and it is unbounded in a way the four-line diff is not.

### An important complication: `sort=True` is not doing what the code claimed

While assessing this, the flag itself turned out to be a defect on some
layouts. `sort=True` orders blocks by vertical then horizontal position across
the *whole page*, which on a dense two-column paper interleaves the columns
line by line. Measured over this project's own corpus by hyphen-rejoin rate:

| paper | `sort=True` | `sort=False` |
|---|---:|---:|
| a dense two-column ACM paper | **5.8%** | **72.4%** |
| five other papers | within 5 points either way (one favours `sort=True`) | |

So the premise "PyMuPDF's `sort=True` is what the extraction quality rests on"
is **not supported** — on most of this corpus it makes no difference, and on
one paper it is actively destructive. That weakens the case for staying on
PyMuPDF *for reading order specifically*, but it is **not** a reason to simply
delete the flag: the effect is layout-dependent, and changing extraction
re-renders every paper. See the README's "Extraction quality" section. This is
flagged, not fixed — it is a quality decision for the owner.

### The three options, plainly

1. **Keep PyMuPDF, ship AGPL-3.0-or-later.** What is implemented. Costs
   nothing technically; the licence is "viral" for anyone who deploys a
   modified network service, which for a self-hosted personal tool is close to
   irrelevant. **Note this does not remove triggers 2 and 3** — see below.
2. **Buy an Artifex commercial licence for PyMuPDF.** Removes trigger 1 only.
   Pointless on its own.
3. **Drop the AGPL but stay copyleft.** Replace `pymupdf` with `pypdfium2`
   (or `pdfminer.six`/`pdfplumber`). Triggers 2 and 3 remain, so the result is
   **GPL-3.0-or-later**, not permissive — but the network-service clause goes
   away. This is the option most people actually want when they say "get off
   the AGPL", and it costs a four-line diff plus extraction re-validation.
4. **Go fully permissive.** Requires all of the above **and** replacing the
   Kokoro/misaki English G2P path, because the GPL is in the TTS stack. That
   is the expensive one and it changes how the audio sounds: espeak is the
   out-of-dictionary fallback, and misaki's Apache-2.0 lexicon alone misses
   roughly **5% of words** on academic prose (Kokoro's behaviour for an
   unknown word is to skip it silently). Dropping espeak means dropping about
   one word in twenty — words like "heteroscedasticity", which is exactly the
   vocabulary academic papers are made of.

**The honest summary: the AGPL here comes from the TTS engine as much as from
the PDF library.** A "swap PyMuPDF for pypdf and relicense MIT" plan would be
wrong, and would ship a licence violation. Replacing the PDF library buys you
GPL-3.0 instead of AGPL-3.0 — a real gain if the network clause is the problem,
and no gain at all if permissiveness is the goal. If permissive is the goal,
cost out the Kokoro G2P chain first, because that is where the price is.

---

## Direct dependencies

The packages this project imports or installs on purpose.

| package | version | licence | why it is here |
|---|---|---|---|
| `pymupdf` | 1.28.2 | **AGPL-3.0-or-later** or Artifex commercial | PDF text extraction in reading order (`extract.py`) |
| `kokoro` | 0.9.4 | Apache-2.0 | the TTS model pipeline (`synth.py`) |
| `misaki` | 0.9.4 | Apache-2.0 | Kokoro's G2P front end (transitive, but load-bearing) |
| `phonemizer-fork` | 3.3.2 | **GPL-3.0-or-later** | espeak backend, loaded via `misaki[en]` on the English path |
| `espeakng-loader` | 0.2.4 | ships eSpeak NG (**GPL-3.0-or-later**) | bundles `libespeak-ng.so` for the above |
| `num2words` | 0.5.14 | **LGPL-2.1** | number-to-words in G2P, via `misaki[en]` |
| `spacy` | 3.8.16 | MIT | POS tagging in the English G2P |
| `en_core_web_sm` | 3.8.0 | MIT | the spaCy English model misaki needs |
| `torch` | 2.14.0 | BSD-3-Clause | runs the Kokoro model (on CPU here) |
| `transformers` | 5.16.1 | Apache-2.0 | model loading |
| `numpy` | 2.5.3 | BSD-3-Clause | audio buffers (`synth.py`, `build.py`) |
| `fastapi` | 0.141.1 | MIT | the web API |
| `starlette` | 1.6.0 | BSD-3-Clause | imported directly for uploads and threadpool |
| `uvicorn` | 0.52.4 | BSD-3-Clause | the ASGI server |
| `python-multipart` | 0.0.32 | Apache-2.0 | multipart uploads; FastAPI does not pull it in |

Non-Python, optional, not redistributed:

| | licence | note |
|---|---|---|
| `ffmpeg` | LGPL-2.1+ / GPL-2+ depending on build | **optional**, used only by `./build.py --m4b`, invoked as a separate process. Not bundled, not linked. |

Front-end: the UI is self-contained HTML with no framework and no build step.
It loads webfonts from Google Fonts at runtime (Zilla Slab, Source Serif 4,
IBM Plex Mono — all SIL Open Font License 1.1). One JavaScript library is
vendored for the synced reader page (`static/reader.html`):

| package | version | licence | why it is here |
|---|---|---|---|
| `pdfjs-dist` | 4.10.38 | Apache-2.0 | renders PDF pages client-side so narration chunks can be highlighted on the original pages; `static/vendor/pdfjs/` (pdf.min.mjs + pdf.worker.min.mjs + its LICENSE) |

`jsdom` (MIT) is a test-only devDependency and is never shipped or served.

## Deliberately removed

Installed historically, imported nowhere, and dropped during the release prep:

| package | why removed |
|---|---|
| `pymupdf4llm` | never imported; only pulled `pymupdf` transitively, plus `tabulate` and `psutil` |
| `pymupdf-layout` | came in via the above; **`Classifier: License :: Other/Proprietary License`** on top of the same Artifex dual licence — an add-on this project has no use for |
| `soundfile` | never imported; audio is written with the stdlib `wave` module |

## Two upstream gaps worth knowing about

**`espeakng-loader` declares no licence at all.** Its wheel has no `License`
field, no `License-Expression`, no classifier and no LICENSE file — yet it
redistributes `libespeak-ng.so` and 19 MB of `espeak-ng-data`, which are
GPL-3.0-or-later. Upstream describes the ~50-line loader shim as MIT, but that
cannot cover the GPL binary it vendors, and the wheel carries no GPL notice and
no source offer. **This does not affect you when users `pip install`** — they
obtain it from PyPI and you are not the distributor. It *would* become your
problem if you ever ship a bundle, an image or an installer containing this
venv, at which point you inherit the obligation to provide espeak-ng's
corresponding source.

**`en_core_web_sm` is MIT, but its training corpus is not.** The wheel's
`LICENSE` is MIT (Explosion AI); its `LICENSES_SOURCES` file discloses that the
model was trained on **OntoNotes 5**, which is *"License: commercial (licensed
by Explosion)"* from the LDC, plus **WordNet 3.0** under the Princeton WordNet
licence. The distributed artefact is MIT and this does not block an AGPL
release, but the attribution belongs in any redistribution.

**Model weights are not shipped by this project.** `hexgrad/Kokoro-82M` is
downloaded at runtime by `huggingface_hub`; its model card licence is
Apache-2.0.

## A note on the NVIDIA wheels

On Linux, plain `torch` resolves **15 `nvidia-*` CUDA packages under NVIDIA's
proprietary licence** (`LicenseRef-NVIDIA-Proprietary`). This project is
CPU-only by design and loads none of them. They are aggregation, not linkage,
so they do not affect the licence of this work — but they are ~3 GB of
proprietary binaries you did not ask for. `./install.sh --cpu-torch` installs
the CPU-only build from PyTorch's own index and avoids all of them.

---

## Complete transitive set

Every package `uv pip install -r requirements.txt` resolves (113 packages,
resolved against Python 3.12). Licence text is as declared by each package's
own metadata — `License-Expression` where present, otherwise `License` or the
OSI classifier — and is reproduced rather than interpreted. A handful declare
their licence badly (`addict` says `UNKNOWN` but is MIT upstream; `isodate`
puts a copyright line in the field but is BSD-3-Clause; `cuda-toolkit` and
`espeakng-loader` declare nothing).

| package | version | licence (as declared) |
|---|---|---|
| `addict` | 2.4.0 | UNKNOWN |
| `annotated-doc` | 0.0.5 | MIT |
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.15.1 | MIT |
| `attrs` | 26.1.0 | MIT |
| `babel` | 2.18.0 | BSD-3-Clause |
| `blis` | 1.3.3 | BSD |
| `catalogue` | 2.0.10 | MIT |
| `certifi` | 2026.7.22 | MPL-2.0 |
| `charset-normalizer` | 3.5.1 | MIT |
| `click` | 8.5.0 | BSD-3-Clause |
| `cloudpathlib` | 0.25.0 | MIT License |
| `cloudpickle` | 3.1.2 | BSD-3-Clause |
| `confection` | 1.3.3 | MIT |
| `csvw` | 4.1.0 | Apache 2.0 |
| `cuda-bindings` | 13.3.1 | LicenseRef-NVIDIA-SOFTWARE-LICENSE |
| `cuda-pathfinder` | 1.8.1 | Apache-2.0 |
| `cuda-toolkit` | 13.0.3.0 | ? |
| `curated-tokenizers` | 0.0.10 | MIT |
| `curated-transformers` | 0.1.1 | MIT |
| `cymem` | 2.0.13 | MIT |
| `dlinfo` | 2.0.0 | MIT |
| `docopt` | 0.6.2 | MIT |
| `en_core_web_sm` | 3.8.0 | MIT |
| `espeakng-loader` | 0.2.4 | ? |
| `fastapi` | 0.141.1 | MIT |
| `filelock` | 3.32.6 | MIT (installed 3.32.5) |
| `fsspec` | 2026.7.0 | BSD-3-Clause |
| `h11` | 0.16.0 | MIT |
| `hf-xet` | 1.6.0 | Apache-2.0 |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `huggingface_hub` | 1.30.0 | Apache-2.0 |
| `idna` | 3.19 | BSD-3-Clause |
| `isodate` | 0.7.2 | Copyright (c) 2021, Hugo van Kemenade and contributors |
| `Jinja2` | 3.1.6 | BSD License |
| `joblib` | 1.6.0 | BSD-3-Clause |
| `jsonschema` | 4.26.0 | MIT |
| `jsonschema-specifications` | 2025.9.1 | MIT |
| `kokoro` | 0.9.4 | Apache License |
| `language-tags` | 1.3.1 | MIT |
| `loguru` | 0.7.3 | MIT License |
| `markdown-it-py` | 4.2.0 | MIT License |
| `MarkupSafe` | 3.0.3 | BSD-3-Clause |
| `mdurl` | 0.1.2 | MIT License |
| `misaki` | 0.9.4 | Apache License |
| `mpmath` | 1.3.0 | BSD |
| `murmurhash` | 1.0.15 | MIT |
| `networkx` | 3.6.1 | BSD-3-Clause |
| `num2words` | 0.5.14 | LGPL |
| `numpy` | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| `nvidia-cublas` | 13.1.1.3 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cuda-cupti` | 13.0.85 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cuda-nvrtc` | 13.0.88 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cuda-runtime` | 13.0.96 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cudnn-cu13` | 9.24.0.43 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cufft` | 12.0.0.61 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cufile` | 1.15.1.6 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-curand` | 10.4.0.35 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cusolver` | 12.0.4.66 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cusparse` | 12.6.3.3 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cusparselt-cu13` | 0.8.1 | NVIDIA Proprietary Software |
| `nvidia-nccl-cu13` | 2.30.7 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-nvjitlink` | 13.3.33 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-nvshmem-cu13` | 3.4.5 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-nvtx` | 13.0.85 | Apache 2.0 |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause |
| `phonemizer-fork` | 3.3.2 | GNU GENERAL PUBLIC LICENSE |
| `preshed` | 3.0.13 | MIT |
| `pydantic` | 2.13.5 | MIT |
| `pydantic_core` | 2.46.5 | MIT |
| `Pygments` | 2.21.0 | BSD-2-Clause |
| `pymupdf` | 1.28.2 | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Comm |
| `pyparsing` | 3.3.2 | MIT |
| `python-dateutil` | 2.9.0.post0 | Dual License |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `PyYAML` | 6.0.3 | MIT |
| `rdflib` | 7.6.0 | BSD-3-Clause |
| `referencing` | 0.37.0 | MIT |
| `regex` | 2026.9.3 | Apache-2.0 AND CNRI-Python |
| `requests` | 2.34.2 | Apache-2.0 |
| `rfc3986` | 1.5.0 | Apache 2.0 |
| `rich` | 15.0.0 | MIT |
| `rpds-py` | 2026.6.3 | MIT |
| `safetensors` | 0.8.0 | Apache Software License |
| `segments` | 2.4.0 | Apache 2.0 |
| `setuptools` | 84.0.0 | MIT |
| `shellingham` | 1.5.4 | ISC License |
| `six` | 1.17.0 | MIT |
| `smart_open` | 8.0.1 | MIT License |
| `spacy` | 3.8.16 | MIT |
| `spacy-curated-transformers` | 0.3.1 | MIT |
| `spacy-legacy` | 3.0.12 | MIT |
| `spacy-loggers` | 1.0.5 | MIT |
| `srsly` | 2.5.3 | MIT |
| `starlette` | 1.6.0 | BSD-3-Clause |
| `sympy` | 1.14.0 | BSD |
| `termcolor` | 3.3.0 | MIT |
| `thinc` | 8.3.13 | MIT |
| `tokenizers` | 0.23.2 | Apache Software License |
| `torch` | 2.14.0 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT |
| `tqdm` | 4.70.0 | MPL-2.0 AND MIT |
| `transformers` | 5.16.1 | Apache 2.0 License |
| `triton` | 3.8.0 | MIT |
| `typer` | 0.27.2 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |
| `typing-inspection` | 0.4.4 | MIT |
| `uritemplate` | 4.2.0 | BSD 3-Clause OR Apache-2.0 |
| `urllib3` | 2.7.0 | MIT |
| `uvicorn` | 0.52.4 | BSD-3-Clause |
| `wasabi` | 1.1.3 | MIT |
| `weasel` | 1.0.0 | MIT |
| `wrapt` | 2.4.0 | BSD-2-Clause |

---

*Regenerate the table by resolving `requirements.txt` into a clean environment
and reading each `*.dist-info/METADATA`. If you add a dependency, add its row.*
