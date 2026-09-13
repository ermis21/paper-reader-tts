# Security

Read this before you put the web UI anywhere other than `127.0.0.1`.

## What this software is

A **single-user, single-trust-domain tool.** It is designed to run on a machine
you control, for you, on an address only you can reach. It has no accounts, no
passwords, no sessions and no authorisation checks of any kind, and adding them
is not on the roadmap. Everything below follows from that one decision.

The CLI stages (`fetch.py`, `extract.py`, `synth.py`, `build.py`) have no
network surface at all beyond `fetch.py` downloading the URLs you put in
`papers.json`. The web UI (`webapp.py`) is the part with a threat model.

## Threat model

**Trusted:** you, the machine, everything already able to run code as your user.

**Not defended against:** anyone who can send an HTTP request to the port.

| | |
|---|---|
| **Authentication** | None. Every endpoint is open to anyone who can reach the port. |
| **Authorisation** | None. There is one library and every caller gets all of it. |
| **Transport** | Plain HTTP. No TLS. Uploads and downloads are in clear text. |
| **Uploads** | Anyone who can reach the port can upload files. |
| **Code execution** | An upload **starts a subprocess pipeline** (`extract.py` → `synth.py` → `build.py`) as your user. |
| **Sandboxing** | None. Those subprocesses run with your full user privileges and your whole filesystem. |
| **Parser exposure** | Uploaded PDFs are parsed by **MuPDF**, a large C library. A malicious PDF is an attack on that parser, not on this Python code. |
| **Resource limits** | Per-request caps only (see below). Nothing caps *total* disk use or how much CPU the render queue may consume over time. |

### The specific risks, in the order they matter

1. **Exposing the port is the whole risk.** Bound to `127.0.0.1` — the shipped
   default — the surface is roughly "other software on this machine". Bound to
   `0.0.0.0` it is "every host that can route to this machine", each of which
   can upload a file that makes your computer execute a pipeline. Never
   port-forward it from a router, and never put it on the public internet.

2. **Malicious PDFs reach a C parser.** `pymupdf`/MuPDF is mature but it is a
   memory-unsafe codebase processing hostile input. This is the most likely
   route to actual code execution. Keep `pymupdf` current, and treat "someone
   else can upload a PDF" as equivalent to "someone else can attack MuPDF as
   me".

3. **Cross-site request forgery on `POST /api/upload`.** This one bites even
   on loopback. A page you visit in a browser can submit a
   `multipart/form-data` POST to `http://127.0.0.1:3002/api/upload` without a
   CORS preflight, because that content type and method are CORS-safelisted.
   The attacker cannot *read* the response, but the upload and the render
   happen. The other mutating endpoints (`PATCH`/`DELETE`, and any JSON body)
   do require a preflight, and the app sends no CORS headers, so they are
   refused by the browser. **This is a known, unfixed gap** — see "Known
   limitations" below.

4. **DNS rebinding.** The app does not validate the `Host` header, so a
   loopback-only instance can still be reached by a browser tricked into
   resolving an attacker's hostname to `127.0.0.1`. Same consequence as (3).

5. **Denial of service.** Per-request caps exist (80 MB per PDF, 25 MB per
   attachment, 400 files and 1024 MB per request, 12 folder levels), and
   uploads stream to disk with the cap enforced *while reading*, so an
   oversized file never lands and memory stays bounded. But there is no global
   quota: a caller may repeat requests until the disk is full, and the render
   queue will happily accept more work than the machine can finish.

## What *is* defended, and how

These are deliberate, tested properties, not accidents.

- **Path traversal.** Every file-serving endpoint (`/audio`, `/pdf`, `/text`,
  `/file`) resolves its target through one guard, `_resolve()`: the name must
  match `^[A-Za-z0-9._-]+$`, must not contain `..`, and the fully resolved path
  must still be inside the one fixed base directory *after* symlinks are
  followed, and must be a regular file. Probes for `/audio/../papers.json`,
  `/pdf/..%2f..%2fetc%2fpasswd`, `/text/../workspace.sqlite`,
  `/file/../../webapp.py`, `/audio/sub%2Fdir.wav` and
  `/audio/%2e%2e%2fpapers.json` all return 404.

- **Folder names never touch the filesystem.** The workspace tree is virtual:
  folders exist only as rows in `workspace.sqlite`, and a folder name is never
  used to build a path. Uploading a directory called `../../etc` creates a
  folder *named* `.. etc` and nothing else. This is why a hostile directory
  tree in an upload is an inert naming problem rather than a traversal.

- **Ids are slugs, and never reach a shell.** An uploaded filename is reduced
  to `^[a-z0-9-]{1,60}$` before it keys anything on disk. Subprocesses are
  invoked with an argument list, never `shell=True`, so there is no command
  injection path even if the slug rules were wrong.

- **PDFs are validated by magic bytes,** not by extension. A `.pdf` whose first
  bytes are not `%PDF-` is rejected and reported, and is never stored under a
  name claiming to be a PDF.

- **Attachments cannot become same-origin script.** Anything that is not a PDF
  is served as `application/octet-stream` with `X-Content-Type-Options:
  nosniff` and `Content-Disposition: attachment`, so an uploaded `.html` or
  `.svg` downloads instead of executing against the app's origin.

- **Uploads are bounded while streaming.** `_spill()` enforces the byte cap as
  it reads, so memory use does not scale with the size of the upload and an
  over-cap file is discarded rather than written.

## Known limitations (accepted, not overlooked)

- No authentication, authorisation, TLS, CSRF token or `Host`/`Origin` check.
- The CSRF gap on `POST /api/upload` described above is real and unfixed. The
  cheapest fix, if you want one, is to reject state-changing requests carrying
  an `Origin` header that is not the app's own; a reverse proxy in front is the
  more usual answer.
- Subprocesses are not sandboxed. If you need that, run the whole app in a
  container or under the systemd hardening in
  `paper-audiobook-web.service.template`, which sets `NoNewPrivileges`,
  `PrivateTmp`, `ProtectSystem=full` and `ProtectHome=read-only`.
- Archiving a document is deliberately non-destructive: the PDF, the extracted
  text, the chunk cache and the WAV all stay on disk. "Deleted" in the UI does
  not mean "erased from the machine".

## If you want to expose it anyway

Both of these are reasonable; running it naked on `0.0.0.0` is not.

- **Put it behind a reverse proxy** that terminates TLS and requires
  authentication, and bind the app itself to `127.0.0.1`.
- **Put it on a private overlay network** (WireGuard, Tailscale or similar) and
  bind to that interface. Remember that this makes it reachable by *every*
  device on that network, phones included — which is usually the point, but it
  is not a security boundary against anything that gets onto the network.

Set the address in `local.env` (`PA_HOST=...`), or in the `Environment=` line
of your rendered systemd unit. See `local.env.example`.

## Reporting a vulnerability

Open an issue for anything that is already public or is a hardening
suggestion. For something exploitable that is not yet public, contact the
maintainer privately rather than filing a public issue, and please allow time
for a fix before disclosing.

Given the design above, note that "the app is unauthenticated" and "an upload
runs a subprocess" are **documented properties, not vulnerabilities**. A report
that this software is unsafe when deliberately exposed to a hostile network is
describing the first section of this file.
