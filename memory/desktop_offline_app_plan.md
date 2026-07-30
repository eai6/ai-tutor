# Offline Desktop App (Linux / Windows / macOS) — Plan (2026-07-30)

## Problem

Ship the AI Tutor as an installable desktop application for Linux, Windows, and
macOS that runs **with no internet at all**: `qwen3-4b` via Ollama does the
tutoring, all lesson content for the student's institution is pre-loaded into a
local database, and student data (sessions, turns, progress, competency) is
written to and stays on the device.

The target is pilot classrooms with unreliable or absent connectivity
(Seychelles, Mozambique). The existing production path — Django on Azure
Container Apps calling Anthropic — is unusable there. `serve.py` and `chat.py`
already prove the runtime works offline on one machine; this plan turns that
into something a teacher can install from a USB stick.

Mobile is **out** — see "Out of scope".

## Current state (from audit)

**The offline runtime already works.** Verified on this Mac 2026-07-30:
`ollama create qwen3-4b-jetson -f infra/ollama/Modelfile.qwen3-4b-jetson`, then
`./chat.py --lesson 1425`, tutors a real lesson end to end against local SQLite.

**Measured latency (M3 Pro, 18 GB, qwen3-4b Q4_K_M, `num_ctx` 16384):**

| | value |
|---|---|
| synthetic single generation (`scripts/bench_latency_local_vs_cloud.py`) | 1.8–2.4 s, TTFT 0.11 s, 37–41 tok/s |
| **real `./chat.py` turn, steady state** | **13.8–19.6 s** |
| real first turn (cold model + embedding load) | 33.2 s |

A real turn is 2–3 `/api/chat` calls (grader runs before tutor), each carrying
the full tool-schema prompt. The synthetic bench understates a real turn by
**7–9×**. Raw: `offline_eval/latency_mac_m3pro_2026-07-30.json`.

**Blocker — KB retrieval is silently dead on SQLite. FIXED 2026-07-30, see
"Phase 0 outcome".** Vectors moved from ChromaDB to Postgres+pgvector on
2026-05-24 (`config/settings.py:197-203`, `memory/pgvector_migration_plan.md`).
It left **three independent SQLite guards**, not one:

1. `kb_storage.query_chunks` — `if connection.vendor != 'postgresql': return _empty_result()`
2. `kb_storage.upsert_chunks` — same check, skipped writing chunks entirely
3. `knowledge_base.py:177` — `self._storage_available = connection.vendor == 'postgresql'`,
   read by **eleven** methods that each bail out to an empty result

Any one of them was sufficient to empty `<kb_context>`, and
`apps/tutoring/simple_tutor/engine.py:1666` (`_retrieve_kb`) fails soft to `[]`,
so the tutor kept answering, ungrounded, with no warning — the
"silent-skip on missing dependencies" anti-pattern CLAUDE.md forbids. Today's
`./chat.py` session was running degraded this way; it may explain the repeated-
question behaviour observed on lesson 1425.

**The existing offline machinery is the wrong shape for desktop.**
`apps/api/views/offline_pack.py` + `LessonPackVersion` build **per-lesson JSON
packs** for a *remote* RN client to interpret with its own TS state machine.
Desktop runs the real Django app, so it needs *rows in a local DB*, not JSON
packs. The pack endpoint stays for mobile; desktop does not use it.

**Sizes that drive packaging:**

| item | size |
|---|---|
| qwen3-4b Q4_K_M (Ollama) | 2.5 GB |
| `media/` (figures/images) | 142 MB |
| `db.sqlite3` (dev, full curriculum) | 41 MB |
| torch + sentence-transformers + transformers | ~500 MB installed |
| all-MiniLM-L6-v2 weights | ~90 MB |

torch is pulled in **only** to produce 384-d embeddings —
`kb_storage.embed()` (`:85-90`) and the grader's embedding gate
(`apps/tutoring/simple_tutor/grader.py:1037`). Half a gigabyte of deep-learning
framework for one small encoder.

**Other relevant facts:**
- `config/settings.py:128-144` already falls back to SQLite with WAL + 30 s
  busy timeout when `DATABASE_URL` is unset. No settings work needed.
- `serve.py:33-41` establishes the env contract (`TUTOR_MODEL_OVERRIDE`,
  `OLLAMA_*`, `HF_HUB_OFFLINE=1`). `HF_HUB_OFFLINE` is load-bearing:
  sentence-transformers otherwise spends 20.6 s on DNS retries then raises,
  taking the grader's embedding tier down with it (`chat.py:63-73`).
- `apps/llm/model_profiles.py:209` keys its Jetson profile on the **exact**
  spec `local_ollama/qwen3-4b-jetson`. A bare tag falls through to the generic
  `r"qwen3"` *cloud* profile → `num_ctx` 24192 → runner eviction each turn.
- `ModelConfig.resolve_runtime` (`apps/llm/models.py:401-409`) now treats
  `local_ollama` as keyless, so no seeded DB row is required.
  `offline_eval/seed_ollama_configs.py`'s docstring is stale on this point.
- `archives/AI-Tutor.spec` is a PyInstaller + pywebview spec for the *old Flask*
  app. Useful as precedent for the packaging shape, not reusable as-is.
- `/api/v1/sessions/<id>/sync/` (`apps/api/views/sync.py`) already exists for
  pushing offline session data back up.

## Target design

**One process tree, three OSes:**

```
Tauri shell (Rust, system webview)
  ├─ sidecar: ai-tutor-server   (PyInstaller-frozen Django + gunicorn/waitress)
  │     └─ SQLite at <app-data>/tutor.db     ← curriculum + student data
  └─ sidecar: ollama            (bundled binary)
        └─ model blobs at <app-data>/models/ ← qwen3-4b-jetson, 2.5 GB
```

The shell opens a window on `http://127.0.0.1:<free-port>/student/login/`. The
UI is the existing server-rendered Django templates — no separate frontend
build, no API rewrite.

**Shell choice: pywebview + PyInstaller.** Confirmed 2026-07-30, **reversing
this plan's original Tauri v2 decision**. What changed:

- The Django app must be frozen with PyInstaller under *any* shell. pywebview
  therefore adds **zero** new toolchains; Tauri adds two (Rust + Node).
- Rust is not installed on the build machine, and Tauri cannot cross-compile —
  it needs per-OS runners either way, so its packaging story is not the
  shortcut it looked like.
- `archives/AI-Tutor.spec` is direct precedent: this repo already shipped a
  pywebview + PyInstaller desktop app (of the older Flask version).
- Tauri's real advantage is a ~10 MB shell vs Electron's ~150 MB. Against a
  payload of a 2.5 GB model plus a frozen Python runtime, that margin does not
  decide anything.

Accepted cost: pywebview's Linux backend is GTK/WebKitGTK, which varies across
distros — hence AppImage as the first Linux target (see open question 3), and
the GTK system packages noted in `requirements-desktop.txt`. Windows needs the
WebView2 runtime, which ships with Windows 11 and is a redistributable on
Windows 10; the installer has to check for it.

**Content delivery: institution-scoped content packs.** A server-side command
builds a versioned, signed archive; the desktop app imports it into local
SQLite. Distributable over the network *or* from a USB stick, which is the case
that actually matters in the field.

```
content-pack-<institution>-<version>.tar.zst
  manifest.json        version, institution_id, built_at, checksums, schema rev
  curriculum.jsonl     Course → Unit → Lesson → LessonStep, ExitTicket + questions
  kb_chunks.jsonl      CurriculumKnowledgeBase chunks + precomputed 384-d embeddings
  media/               figures referenced by the above
```

Embeddings are **precomputed server-side** and shipped in the pack. The device
never needs to embed a corpus — only the student's short query at runtime.

**Fixing the KB blocker: a SQLite vector backend.** Add a non-Postgres branch to
`kb_storage.query_chunks()` doing brute-force cosine over stored embeddings —
no index, no extension, no new dependency. This mirrors the existing
`connection.vendor` dispatch rather than introducing an abstraction (Rule of
Three: this is the second backend, not the third).

This plan originally asserted "well under 50 ms" for 10⁴ chunks. **Measured, that
was wrong by ~20×**: 1063 ms. Profiling showed the ranking is free and the cost
is entirely reading vectors back out of SQLite:

| stage | 10,000 chunks |
|---|---|
| DB load + deserialize | 1114 ms |
| stack into matrix | 4 ms |
| normalize + matmul | 5 ms |

So the backend caches a pre-normalised matrix per institution, keyed on a
`(count, max(updated_at))` fingerprint. Warm queries are **7.2 ms** unfiltered
and 9.8 ms filtered — a 147× improvement — and the ~1 s cold build happens once
per process. The desktop app should warm it during the splash screen rather than
paying it on the student's first turn.

**Dropping torch. DONE 2026-07-30.** `embed()` now dispatches on the existing
`settings.EMBEDDING_BACKEND` (`config/settings.py:329`): `'local'`
(sentence-transformers, unchanged default) or `'onnx'` (what the desktop build
ships). `scripts/export_minilm_onnx.py` exports from the *locally cached*
sentence-transformers weights, so the graph provably matches the encoder it
replaces. All three `embed()` callers — the curriculum KB, the grader's
embedding gate (`grader.py:1037`), and `apps/support/kb.py` — route through it
and benefit.

Measured, not estimated:

| | sentence-transformers | ONNX |
|---|---|---|
| cold load + first encode | 5814 ms | **144 ms** |
| per query (median) | 8.3 ms | 7.2 ms |
| imports torch | yes | **no** |

Disk, corrected twice. This plan first claimed "~500 MB → ~90 MB", then
"~268 MB saved". The accurate figure is **~340 MB**, because `onnxruntime` and
`tokenizers` are *already* in `requirements-core.txt` (chromadb / faster-whisper
pull them in regardless), so the ONNX path adds only the 87 MB exported model:

| dropped | | kept |
|---|---|---|
| torch | 372 MB | onnxruntime — already required |
| transformers | 51 MB | tokenizers — already required |
| sentence-transformers | 3.8 MB | models/minilm-l6-v2 — **+87 MB** |
| **427 MB out** | | **87 MB in** |

`pip show` confirms torch and transformers are required *only* by
sentence-transformers, and sentence-transformers by nothing — so all three
leave together. The 40× faster cold start still matters more than the disk: it
comes straight off the measured 33.2 s first turn.

Parity is enforced twice — the export script fails if worst-case cosine drops
below 0.999, and `apps/curriculum/tests/test_onnx_embedding_parity.py` checks
per-probe cosine, L2 normalisation, neighbour ordering, and batch-vs-single
equivalence. Measured worst-case cosine: **1.000000**. A missing artifact raises
rather than silently falling back to torch, so a desktop build cannot quietly
re-acquire the dependency it was meant to drop.

**Student data is local and authoritative.** `TutorSession`, `SessionTurn`,
`StudentLessonProgress`, `StudentCompetencyRecord` all write to the same local
SQLite. No cloud dependency. An **opt-in** "upload progress" action posts to the
existing `/api/v1/sessions/<id>/sync/` when a network is present; nothing is
uploaded silently.

## Data model changes

Minimal — the desktop app runs the same schema.

**New (server side only):**

```python
# apps/curriculum/models.py
class ContentPackVersion(models.Model):
    institution   = models.ForeignKey(Institution, on_delete=models.CASCADE)
    version       = models.PositiveIntegerField()          # monotonic per institution
    built_at      = models.DateTimeField(auto_now_add=True)
    schema_rev    = models.CharField(max_length=40)        # latest migration applied at build
    checksum      = models.CharField(max_length=64)        # sha256 of the archive
    size_bytes    = models.BigIntegerField()
    lesson_count  = models.PositiveIntegerField()
    chunk_count   = models.PositiveIntegerField()
    class Meta:
        unique_together = [('institution', 'version')]
```

Named `ContentPackVersion`, deliberately distinct from the mobile
`LessonPackVersion` (`apps/tutoring/models.py`) — different scope (institution
vs lesson), different consumer, different lifecycle. Do not overload the latter.

**New (device side only), in a `desktop` app:**

```python
class DeviceState(models.Model):          # single row, pk=1
    institution_id      = models.PositiveIntegerField()
    pack_version        = models.PositiveIntegerField(null=True)
    pack_imported_at    = models.DateTimeField(null=True)
    device_id           = models.UUIDField(default=uuid4)   # stable id for sync
    last_sync_at        = models.DateTimeField(null=True)
```

**No changes** to `CurriculumChunk`. Verified 2026-07-30 by round-tripping a
384-d vector through the local SQLite DB: `VectorField` writes to a text column
and reads back as a `numpy.ndarray` of the right shape and dtype. The
contingency `embedding_json` field this plan originally hedged on is **not
needed** — dropped.

Migration strategy: the desktop app runs `migrate` on first launch and on every
version upgrade. The pack manifest carries `schema_rev`; importing a pack whose
`schema_rev` is ahead of the installed app is refused with a "please update"
message rather than a partial import.

## Backend changes

**`apps/curriculum/kb_storage.py`**
- `search()` (`:274`): replace the `!= 'postgresql'` early-return with dispatch
  to `_search_pgvector()` / `_search_sqlite_bruteforce()`.
- `_search_sqlite_bruteforce()`: load `(id, content, embedding, *metadata)` for
  the institution, cosine against the query vector via NumPy, return top-K in
  the existing ChromaDB-shaped dict.
- `collection_stats()` (`:313`): report `backend='sqlite-bruteforce'` and a real
  chunk count instead of a hardcoded `0`.
- `embed()` (`:85`): swap sentence-transformers for onnxruntime.

**`apps/tutoring/simple_tutor/engine.py`**
- `_retrieve_kb()` (`:1666`): keep failing soft, but `logger.warning` when the
  backend reports zero chunks for an institution that should have them. Silent
  degradation is what hid this for two months.

**`apps/curriculum/management/commands/build_content_pack.py`** (new)
- `--institution <id> [--out DIR] [--sign]`. Serializes curriculum + KB +
  media, writes the archive, creates a `ContentPackVersion`. Institution
  scoping per CLAUDE.md: `Q(institution=inst) | Q(institution__isnull=True)`.

**`apps/desktop/management/commands/import_content_pack.py`** (new)
- `<path> [--force]`. Verifies checksum + `schema_rev`, imports inside one
  transaction, updates `DeviceState`. Idempotent; re-importing the same version
  is a no-op.

**`desktop_server.py`** (new, repo root — sibling of `serve.py`)
- Binds `127.0.0.1` on an OS-assigned free port (not `0.0.0.0:8000` —
  `serve.py`'s LAN binding is wrong for a single-user desktop app), prints the
  chosen port as JSON on stdout for the Tauri shell to read, serves via
  `waitress` (pure-Python, works on Windows; gunicorn does not).
- Same env contract as `serve.py:33-41`.
- `--first-run` performs migrate + superuser-less local student creation.

**`apps/desktop/bootstrap.py`** (new)
- Health checks the shell needs: Ollama reachable, `qwen3-4b-jetson` tag
  present, pack imported, migrations current. Returns structured JSON so the
  splash screen can render real progress instead of a spinner.

## Frontend / shell changes

**`desktop/` (new, Tauri v2 project)**

- `src-tauri/tauri.conf.json` — sidecar declarations, bundle targets, app
  identifier, icons.
- Rust `main.rs`:
  1. Spawn `ollama serve` sidecar with the tuned env (`OLLAMA_FLASH_ATTENTION=1`,
     `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_NUM_PARALLEL=1`,
     `OLLAMA_MAX_LOADED_MODELS=1`) — these must match how the server was
     launched or the client's fit preflight misprojects the KV cache
     (`chat.py:52-58`).
  2. Spawn `ai-tutor-server`, read the port from its stdout.
  3. Poll `/desktop/health/` until ready, showing bootstrap progress.
  4. Open the main window on the login page.
  5. On exit, terminate both sidecars.
- Splash screen states: *Starting engine* → *Loading model (2.5 GB)* →
  *Importing lessons* → *Ready*. First launch is slow (model load ~30 s cold,
  measured); a spinner with no explanation reads as a hang.

**Django template changes**
- A `DESKTOP_MODE` setting hides cloud-only UI: staff/institution admin,
  invites, anything that posts to Azure. Add a `{% if not desktop_mode %}` guard
  rather than forking templates.
- A first-run screen: pick student name/grade, import pack from file, done.

**Latency UX.** 14–20 s per turn is the reality on good hardware and will be
worse on a classroom laptop. Streaming is essential, not optional: the engine
already supports `--stream` (`apps/tutoring/simple_tutor/stream_filter.py`,
`memory/offline_streaming_plan.md`), and only the second LLM call streams
because the grader must run first. Show an explicit "checking your answer…"
state for the grader phase so the first several seconds aren't dead air.

## Out of scope

- **Mobile / Android / iOS.** Dropped 2026-07-30. On-device qwen3-4b projects to
  1–5 min per turn on phone-class hardware (from `feedback_on_device_llm_findings.md`
  measurements scaled to a 4B). `mobile/` and `apps/api/views/offline_pack.py`
  are untouched by this plan.
- **Multi-student devices / classroom sync server.** One device, one student
  profile. A shared-device mode is v2.
- **Teacher dashboard in the desktop app.** Students only. Teachers use the web
  dashboard.
- **Content authoring offline.** Content generation, uploads, and image
  generation remain cloud-only — they need frontier models.
- **Automatic pack updates over the air.** v1 imports a file the teacher
  supplies. Auto-update is v2.
- **Code signing certificates.** Flagged as a blocker below, not solved here.
- **Cloud-model fallback when online.** v1 is local-only, always. Mixing paths
  doubles the states to test.
- **Replacing Ollama with embedded llama.cpp.** Ollama's sidecar is bigger than
  strictly necessary but is proven on this stack.

## Phased delivery

Solo-dev days of focused work. Calendar time will be longer alongside existing
Django work.

| Phase | Work | Days |
|---|---|---|
| **0. De-risk the runtime** | ~~SQLite vector backend~~ ✅; ~~warn-on-empty in `_retrieve_kb`~~ ✅; ~~ONNX embedding swap + parity test~~ ✅; ~~matrix cache~~ ✅. Remaining: CPU-only decode measurement; re-measure turn latency with KB restored | 4 (≈3 done) |
| **1. Content packs** | `ContentPackVersion`; `build_content_pack`; `import_content_pack`; media handling; round-trip test on a real institution | 5 |
| **2. Local runtime** | `desktop_server.py` (waitress, free port, JSON handshake); `apps/desktop` app + `DeviceState`; `bootstrap.py` health checks; `DESKTOP_MODE` template guards; first-run screen | 4 |
| **3. Tauri shell** | Tauri v2 project; sidecar lifecycle for Ollama + server; splash with real progress; window/menu/quit behaviour; streaming UX for the 14–20 s turn | 5 |
| **4. Packaging** | PyInstaller spec for Django (hidden imports, Django app discovery, static/template data files); Ollama binary per OS; `.msi` / `.dmg` / `.AppImage` + `.deb`; GH Actions matrix on windows/macos/ubuntu runners | 7 |
| **4b. Offline provisioning** | GGUF-based `Modelfile.qwen3-4b-desktop`; bundle builder; first-run "insert setup drive" flow; free-space preflight; **network-disabled CI provisioning test** | 3 |
| **5. Student data + sync** | Local-only accounts; opt-in upload via `/api/v1/sessions/<id>/sync/`; export-to-file for no-network transfer | 3 |
| **6. Field hardening** | Low-RAM behaviour (8 GB classroom laptop, no GPU); CPU-only decode rate measurement; crash recovery; disk-full and corrupt-pack handling; install docs | 4 |
| | **Total** | **~35 days** |

Phase 0 is genuinely blocking — the tutor is currently ungrounded on SQLite, and
shipping that would ship a quality regression to exactly the students with the
least support.

## Risks

**CPU-only decode.** Every number in this plan is from an M3 Pro with unified
memory and Metal. A classroom Windows laptop with an integrated GPU will run
qwen3-4b Q4 far slower — plausibly 5–10 tok/s, which puts a real turn at 60–120 s.
**This is the single biggest threat to the project and is not yet measured.**
Phase 6 measures it; if it lands above ~60 s, the answer is a smaller model
(qwen3.5-2b, already has a Modelfile at
`infra/ollama/Modelfile.qwen3.5-2b-jetson`) or a hardware floor in the pilot spec.
Consider pulling this measurement forward into Phase 0.

**Installer size.** ~3.5–4 GB with the model bundled. Over a Seychelles or
Mozambique connection that is not a download. USB distribution is the realistic
channel and should be designed for, not bolted on.

**Code signing.** Unsigned apps are blocked by Gatekeeper on macOS and
SmartScreen on Windows. Needs an Apple Developer account ($99/yr) and a Windows
Authenticode certificate ($100–400/yr), plus notarization in CI. Real money and
real lead time — start early.

**PyInstaller + Django.** Django's app registry and template/static discovery
are hostile to frozen builds; `archives/AI-Tutor.spec` shows the shape but was
Flask. Budget debugging time. If it fights back, the fallback is shipping an
embedded CPython with a vendored site-packages instead of a single frozen binary.

**Tool compliance on 4B.** `memory/tool_compliance_root_cause.md` and
`memory/jetson_qwen_tool_compliance_plan.md` document qwen3-4b emitting tool
calls on only 17/40 trials. The repeated-question behaviour seen in today's
`./chat.py` run may be this. Offline has no cloud fallback, so this matters more
here than anywhere else.

## Decisions

**D1 — Model delivery: USB-first, zero-internet provisioning.** Confirmed
2026-07-30. The installer ships *without* the model. A separate side-loadable
bundle carries the model and content, and **a complete machine setup must be
possible with no internet at any point** — that is a hard requirement, not a
fallback path. See "Zero-internet provisioning" below. A first-run download
remains available for whoever has bandwidth, but nothing may depend on it.

## Zero-internet provisioning

Every step from bare machine to tutoring student must work with the network
cable out. That rules out several things the obvious implementation would do.

**STATUS 2026-07-30 — the offline path is built and verified.**

| piece | state |
|---|---|
| Ollama binary vendored into the app | ✅ `manage.py stage_ollama` → `vendor/ollama/<platform>/` (32 MB + MIT LICENCE) |
| App runs its own Ollama, not a system one | ✅ verified with no system Ollama present; splash reports `Ollama 0.15.2 (bundled)` |
| Model install from a local GGUF, no registry | ✅ verified; tag created and generation correct (no `<think>` leak) |
| Model install by download | ✅ implemented (`ollama pull` → build tag); convenience only |
| Model bundle producer | ✅ `manage.py build_model_bundle` — GGUF + Modelfile generated from `ollama show --modelfile` |
| Content pack import from a file | ✅ `manage.py import_content_pack` + setup-screen picker |
| First-run setup screen | ✅ `/desktop/setup/`, splash routes there when unprovisioned |
| Native file picker (not a 2.5 GB HTTP upload) | ✅ pywebview `js_api` folder dialog |
| **Installer / PyInstaller build** | ❌ **not started** — the remaining gap |

**The model is NOT in the installer.** Decided 2026-07-30: a 2.5 GB installer is
impractical to distribute and re-download per app update, and the weights change
far less often than the app. The installer stays ~100 MB (app + vendored Ollama);
the model arrives afterwards from a USB file or a download.

**Ollama model import must not touch the registry.** `ollama create -f
infra/ollama/Modelfile.qwen3-4b-jetson` currently resolves `FROM qwen3:4b-instruct`
against registry.ollama.ai — fine on a connected dev machine, fatal in the
field. Two offline options:

- Copy a prepopulated `models/` tree (content-addressed `blobs/sha256-*` plus
  `manifests/registry.ollama.ai/library/qwen3/4b-instruct`) onto the device.
  Works, but couples the bundle to Ollama's internal store layout.
- **Recommended:** ship the raw GGUF and a Modelfile whose `FROM` points at the
  local file, then run `ollama create` at install time:

  ```
  FROM ./qwen3-4b-instruct-q4_k_m.gguf
  PARAMETER num_ctx 16384
  PARAMETER temperature 0.7
  PARAMETER top_p 0.8
  PARAMETER top_k 20
  ```

  Self-contained, no registry, no dependence on Ollama's store internals, and it
  keeps the `num_ctx` pin that `apps/llm/model_profiles.py:209` requires. Needs a
  second Modelfile variant (`Modelfile.qwen3-4b-desktop`) since the committed one
  is registry-based — keep both, they serve different contexts.

  Caveat: `ollama create` **copies** the GGUF into the blob store, so setup needs
  ~5 GB free transiently for a 2.5 GB model. Check free space before starting and
  fail with a clear message rather than mid-copy.

**The embedding model must be on the USB too.** The ONNX MiniLM (~90 MB) ships in
the bundle and is loaded from an absolute local path. With the ONNX swap
(Phase 0) there is no HF hub call at import, so `HF_HUB_OFFLINE` stops being
load-bearing — but until that lands, first run on a fresh machine would spend
20.6 s on DNS retries and then raise (`chat.py:63-73`). Another reason Phase 0
precedes packaging.

**Bundle layout** (one directory, copied to USB, ~3 GB):

```
ai-tutor-bundle-<version>/
  install/            per-OS installers (.msi, .dmg, .AppImage, .deb)
  models/
    qwen3-4b-instruct-q4_k_m.gguf
    Modelfile.qwen3-4b-desktop
    minilm-l6-v2.onnx
  content/
    content-pack-<institution>-<version>.tar.zst
  README.txt          teacher-facing, one page, no jargon
```

**First-run flow, fully offline:** installer → app launches → splash detects no
model → "Insert setup drive" → teacher picks the bundle directory → app runs
`ollama create`, imports the content pack, creates the student profile → ready.
No prompt at any point requires a network.

**Verification, not assumption.** Add a CI job that provisions a fresh container
with **networking disabled** and drives the whole first-run flow to a completed
tutoring turn. A provisioning path that is only ever tested on a connected
machine will acquire a network dependency by accident; this is the only way to
keep it honest.

## Open questions

1. ~~**Model delivery.**~~ Resolved — see D1.
2. **Which model ships.** Recommend: `qwen3-4b-jetson` as default, with
   `qwen3.5-2b-jetson` selectable for low-spec machines. Reason: both Modelfiles
   exist; the decision should follow the Phase 6 CPU measurement rather than
   precede it.
3. **Linux packaging target.** Recommend: `.AppImage` first, `.deb` second, skip
   `.rpm` for v1. Reason: AppImage sidesteps distro glibc/WebKitGTK variance,
   which is where cross-distro Tauri builds usually break.
4. **Does the desktop app need the exit-ticket flow at parity with web?**
   Recommend: yes, in Phase 2. Reason: `ExitTicket.passing_score` is the mastery
   signal (CLAUDE.md) — a desktop app that skips it produces no competency data.
5. **Institution binding.** Recommend: the pack carries `institution_id` and the
   device binds to it on first import, refusing packs from other institutions.
   Reason: preserves the multi-tenancy invariant on a device that has no server
   to enforce it.

## Next step

Phase 0, first task: add `_search_sqlite_bruteforce()` to
`apps/curriculum/kb_storage.py` and confirm `./chat.py --lesson 1425` renders a
populated `<kb_context>` block. That single change is worth doing regardless of
whether the desktop app ships — it fixes a silent quality regression affecting
every SQLite-backed run today.

Refs: memory/pgvector_migration_plan.md, memory/offline_streaming_plan.md,
memory/terminal_tutor_client_plan.md, memory/tool_compliance_root_cause.md,
memory/latency_bench_local_vs_cloud.md
