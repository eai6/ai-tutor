# qwen3.8-27B vs qwen3.6-27B — human-annotated, on the production engine

**Question:** is Qwen3.8-27B a better local tutor than Qwen3.6-27B **on the
engine we ship** (`aws_deployment`), judged **by hand**?

**Status:** not started. §3 is the work.

> **Two supersessions, both recorded because the reasoning matters:**
> 1. Targeted `offline-harness-copy` — abandoned, wrong engine (§2).
> 2. Used the **math** slice and an LLM rubric judge — abandoned. Math was
>    chosen because the LLM judge saw no spread on geography. Under human
>    grading geography discriminates fine (§1.2), so that reasoning was an
>    artifact of an instrument we no longer use.

---

## 1. The measurement

### 1.1 Instrument: human annotation, not an LLM rubric

Sessions are generated, then graded by hand in the viewer's **Grade** tab against
the eight pedagogical dimensions in `ai_tutor/apps/benchmark/pedagogy.py` — the
same module behind `/dashboard/benchmark/sessions/`. Pass is all-or-nothing over
applicable dimensions (`pedagogy.session_passes`); `n/a` is excluded rather than
counted as failure.

**The LLM rubric judge is switched off** (§3.3). Deterministic assertions
(`expected_reason`, `max_turn_count`, `no_repeated_tutor_phrase_within_window`,
`no_tool_syntax_in_any_turn`) still run — they are not LLM-based and they catch
mechanical breakage for free.

The student-sim (`claude-haiku-4-5`) still runs. It generates the student's turns;
it is not a judge.

### 1.2 Slice: the 34 `hg1` scenarios

The 34 v2+geography scenarios Daniel already hand-graded for
`qwen3.6-27b-instruct` (`offline_eval/manual_grades/mt100_2026-08-19_daniel.json`,
2026-08-19, 215 graded sessions, 0 peeked). Choosing them buys an **exact
old-engine human baseline on identical items**, free.

Human pass rates on precisely these 34, old engine:

| model | human |
|---|---|
| claude-opus-5 | 30/30 · 100.0% |
| **qwen3.6-27b-instruct** | **31/34 · 91.2%** |
| gemini-3.5-flash | 20/29 · 69.0% |
| gpt-5.4-mini | 20/31 · 64.5% |
| qwen3-4b-jetson | 16/26 · 61.5% |
| qwen3-8b-jetson | 15/30 · 50.0% |

Compare the LLM judge on geography∩v2: opus-5 100%, qwen3.6 100%,
gemini-3.5-flash 98%, gpt-5.4-mini 100%, qwen3-8b 84%. **The judge saw a
4-point spread where humans saw fifty.** That is the whole reason this run is
hand-graded, and it is worth remembering before anyone proposes re-adding a
rubric judge to save annotation time.

Persona and scenario-type coverage is broad — average / capable / struggler /
probe_resistant / non_responder / error_prone, across remediation, refusal_chain,
error_cascade, long_session, self_correction, engagement_recovery, speedrun and
session_completion. The full list is §7.

### 1.3 Two arms — and two answer surfaces

**Round 1 asks a second question alongside the model one: does the answer
SURFACE matter as much as the model?**

| arm | surface | why |
|---|---|---|
| `qwen3.8-27b-instruct` | **free text** | a local 27B reads prose options fine; measuring it on buttons would test a UI this tier would not ship |
| `qwen3-4b-jetson` | **A–D picker** | the arm the buttons exist for — device session 29, where it read "northing" as option B |

The surface is set by `ModelProfile.answer_surface`, not by the run command, so
`engine._uses_answer_picker` stays the single predicate behind both the tutor
prompt's `<answer_surface>` block and the frontend's button payload. When those
two disagree you get device session 30 — the tutor hinting about the horizontal
axis while the on-screen buttons still belong to the vertical-axis question,
invisible from either side alone.

Before this, the predicate keyed on `provider == 'local_ollama'`, i.e. it read
"local" as "small enough to need buttons". That held while every local arm was
2B–8B and is wrong for a 27B.

62% of the exit-ticket questions on the eight `hg1` lessons are `mcq` with
options (166 of 268, consistent across all eight), so the picker arm really is
mostly on buttons rather than occasionally.

**The baseline does not carry over to the 4B arm.** Daniel's qwen3-4b figure
(16/26) is a free-text session; a picker run is a different interface and is not
comparable to it. Only the 27B arm has a usable prior — and note that prior is
qwen3.6's 31/34, not qwen3.8's.

### 1.3b Arm bookkeeping

`qwen3.6-27b` has never run on the `aws_deployment` engine, so its numbers do not
transfer. Both arms run here:

| arm | gives |
|---|---|
| `qwen3.6-27b-instruct` | the baseline, re-measured on this engine |
| `qwen3.8-27b-instruct` | the candidate |

**68 sessions to hand-grade.** Daniel graded 215 in one pass, so this is
tractable.

### 1.4 What this can resolve

Paired — both arms on identical scenarios — so more sensitive than independent
samples. At n=34, **act on a 3+ session gap, not less.**

Second read, free: qwen3.6-new-engine vs its 91.2% old-engine human score is the
**engine delta**. Same scenarios, same instrument, same annotator pool. Cleaner
than the earlier plan's version because both sides are now human-graded — the
confound there was grader presence, which no longer applies.

---

## 2. Why not `offline-harness-copy`

Its tutor is not the shipped tutor:

```
27 of 33 shared simple_tutor files differ
engine.py   328 changed lines (118 import-path, ~210 substantive)
tools.py     84    prompts.py 36    model_choice.py 46
```

`aws_deployment` also has `warm_up.py` (`a8ef3cd`, "open every lesson with a
warm-up from a lesson already learned") which the harness branch lacks entirely,
and it is wired in — `_is_warm_up_step`, `_settle_warm_up_step`, a `warm_up`
phase, its own `_OPENING_INSTRUCTION`. Every session opens differently.

The manual-grading stack is **aws_deployment-only** anyway: `manual_grades/`,
`ai_tutor/apps/benchmark/pedagogy.py`, `tests/test_viewer_grading.py`. The
harness branch has an older `build_viewer.py` with no Grade tab.

---

## 3. The work

Source for ports is `offline-harness-copy` at `56868df`. Layout differs:
`apps/…` → `ai_tutor/apps/…`.

### 3.1 Mint the `hg1` tag — 34 files

Append `hg1` to the `tags: [...]` line of the 34 scenarios in §7.

**The v2/v1 tag port is NOT needed.** Earlier plans required copying 100 scenario
files so `--subset v2,math` would resolve; `--subset hg1` needs only these 34.
Porting v2 remains optional future work for anyone wanting mt100 continuity.

### 3.2 Code ports

| from `offline-harness-copy` | to `aws_deployment` | note |
|---|---|---|
| `infra/ollama/Modelfile.qwen3.6-27b-instruct` | same path | verbatim |
| `infra/ollama/Modelfile.qwen3.8-27b-instruct` | same path | verbatim |
| both 27b `ModelProfile` entries | `ai_tutor/apps/llm/model_profiles.py` | aws has neither |
| `filter_by_subset` | `ai_tutor/apps/tutoring/…/run_eval.py` | aws is single-tag only |
| `evals/test_subset_filter.py` | same path | rewrite `ROOT/'apps'` → `ROOT/'ai_tutor'/'apps'`; slice-size assertions become `hg1`=34 |
| `OLLAMA_API_BASE` fallback | `ai_tutor/apps/llm/models.py` | |
| `OLLAMA_API_BASE` on the seeded row | `offline_eval/seed_ollama_configs.py` | port the hunk — aws version already uses `ai_tutor.` imports |
| `offline_eval/run_matrix.sh` | same path | §3.4 |
| `offline_eval/models_qwen38.txt` | same path | **both arms** |

Not needed: `_make_colab_nb_mt100.py`, the notebooks, `MT100_RUNBOOK.md` — the
Vast path does not use Colab.

### 3.3 New: switch off the rubric judge

No flag exists. `evals/runner.py:591` runs it whenever `scenario.rubric` is set,
and `passed = deterministic_passed and rubric_passed and not sim_error`.

Add an opt-out (`--no-rubric`, or `EVAL_SKIP_RUBRIC=1`) that skips Layer 3 and
leaves `rubric_passed=True`. **It must record `rubric_skipped: true` in the result
JSON.** Without that, `passed` silently degrades to "deterministic assertions
only" — a far weaker bar — and `aggregate.py` prints it as a pass rate that reads
like the mt100 board's. That misreading is the main hazard of this whole change.

### 3.4 `run_matrix.sh` — the sharp edge

aws_deployment's copy **has no Modelfile build**. It does a bare
`ollama pull "$tag"`, and `qwen3.8-27b-instruct` is not a registry tag — it would
404 and the arm would be silently skipped ("pull failed — skipping").

It also **has no identity probe** (0 mentions) — the gate that catches a thinking
model masquerading as instruct.

The harness version is strictly ahead (verified: its only aws-unique lines are the
bare pull it replaced), so port the file, then fix the embedded Django code:

```python
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')  # → 'ai_tutor.config.settings'
from apps.llm.model_profiles import get_model_profile               # → from ai_tutor.apps.llm...
```

### 3.5 Viewer wiring

Add the run to `offline_eval/build_viewer.py`:

```python
FLAT_RUNS = [
    ("mt100", "30_mt100_18arm_board"),
    ("hg1_prod", "40_hg1_prod_qwen36_vs_qwen38"),
]
```

The viewer tolerates a missing rubric (`rr = r.get("rubric_result") or {}`), so
transcripts render and grade normally; the Agreement tab simply has no judge side,
which is the point. Verdict keys are `run|model|scenario` and stable across
rebuilds, so grades reattach.

### 3.6 Verify before renting anything

```bash
./venv/bin/python -m pytest evals/ offline_eval/ tests/test_viewer_grading.py -q
DJANGO_SETTINGS_MODULE=ai_tutor.config.settings ./venv/bin/python -m pytest ai_tutor/apps/llm -q
bash -n offline_eval/run_matrix.sh
```

Then the two checks that cost nothing and catch the likeliest silent failures:

```
--subset hg1                                    → exactly 34
get_model_profile('local_ollama/qwen3.8-27b-instruct')
    → qwen instruct max_tokens=2048 num_ctx=32768 ollama_think=False
```

A wrong tag set silently runs different scenarios. A missed profile key falls
through to the generic `r"qwen3"` cloud profile (`num_ctx=None` → client sizes to
24192 → OOM risk).

---

## 4. Run

### 4.1 Box — Vast RTX 3090 24 GB, ~$0.12/hr

```bash
vastai set api-key "$VAST_API_KEY"
vastai search offers 'gpu_name=RTX_3090 disk_space>=60 rentable=true reliability>0.98' -o dph
vastai create instance <ID> --image ollama/ollama --disk 60 --ssh
```

Not the $0.029 V100: Volta has no flash attention, so `OLLAMA_KV_CACHE_TYPE=q8_0`
silently does not apply and KV lands at f16 (~6.4 GB vs ~3.2 GB). 3090 is Ampere.

```
weights + vision projector   17.74 GB
KV @ 32k, q8_0               ~3.2 GB
                             ~21 GB of 24 GB
```

Two arms × 34 ≈ 5–6 h ≈ **~$0.70** of GPU. Both fit one instance; `run_matrix.sh`
is resume-safe per arm. Dropping the rubric judge also removes one
full-transcript Sonnet call per session.

### 4.1b Operational notes — every one of these cost time on the dry run

**`DEBUG=True` is mandatory.** `run_matrix.sh` calls `seed_ollama_configs.py`
first and exits if it fails. That script does not go through `manage.py`, so
`settings.py` raises `ImproperlyConfigured` ("SECRET_KEY is still the
development default while DEBUG is False") and the whole sweep dies before it
touches a model. The Colab notebook writes `DEBUG=True` into its env for the
same reason.

**`AI_TUTOR_ROOT="$PWD"` is mandatory.** `run_matrix.sh` defaults `ROOT` to a
hardcoded path on another developer's machine.

**Attach the SSH key to each new instance.** `~/.ssh/id_ed25519_vast` exists and
is registered on the account, but a fresh instance does not get it
automatically:

    ./venv/bin/vastai attach ssh <instance_id> "$(cat ~/.ssh/id_ed25519_vast.pub)"

Without it: `Permission denied (publickey)`.

**Budget ~10 minutes of model pull before anything generates.** The 27b base is
17 GB and the registry serves it at ~50 MB/s regardless of the box's link speed;
the sha256 verify of a 16 GB layer adds a few more minutes. The 4b is ~90 s.
Instance storage does not survive `destroy`, so this repeats every rental.

**Version skew is expected and fine.** The local CLI (0.15.2) is far behind the
`ollama/ollama` image server (0.32.15). `list`, `pull` and `create` were all
verified working across it on 2026-08-23 — the create is what matters, since a
failure there makes `run_matrix.sh` print "create failed — skipping" and drop
the arm silently.

### 4.2 Serve + tunnel

**Vast's `--ssh` replaces the image entrypoint, so Ollama is NOT running when
the instance comes up**, and `--env` flags passed to `create instance` do not
reach it. Start it by hand:

```bash
# on the instance. OLLAMA_HOST=127.0.0.1 is the security-critical part: the
# image sets 0.0.0.0, and vast maps 11434 to a PUBLIC port. Ollama has no
# authentication, so that would put an open inference API on the internet.
cat > /root/start_ollama.sh <<'EOF'
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0
export OLLAMA_KEEP_ALIVE=1h
export OLLAMA_MAX_LOADED_MODELS=1
exec /bin/ollama serve
EOF
chmod +x /root/start_ollama.sh
setsid nohup /root/start_ollama.sh > /root/ollama.log 2>&1 < /dev/null &

# on the Mac — the tunnel is now the ONLY route in
ssh -i ~/.ssh/id_ed25519_vast -N -L 11434:127.0.0.1:11434 -p <port> root@<ip>
```

Confirm the public port is dead before proceeding:
`curl -m 8 http://<ip>:<mapped_11434_port>/api/tags` must fail.

### 4.3 Smoke, then two gates

```bash
TUTOR_CALL_MODE=two EVAL_SKIP_RUBRIC=1 \
MODELS_FILE="$PWD/offline_eval/models_qwen38.txt" \
MODE="--multi-turn --subset hg1 --sample 1 --seed 0" \
RESULTS_DIR=/tmp/hg1_smoke \
bash offline_eval/run_matrix.sh
```

Never point a sample run at the real results dir: `run_matrix.sh` treats "a JSON
exists here" as "done" with no content check, so a 1-scenario file blocks the real
run unless re-run with `FORCE=1`.

**Gate 1 — instruct mode.** `cat /tmp/hg1_smoke/identity.log` → want
`thinking_chars=0`.

> **Settled 2026-08-23.** A dry run on a rented 3090 produced:
> `qwen3.8-27b-instruct profile_think=False thinking_chars=0 content_chars=1
> -> answers directly (instruct-style)`. **`ollama_think=False` does reach
> Qwen3.8's gate.** Keep the check — it is nearly free and guards against a
> tag rebuilt from a different base — but it is now confirmation, not a coin
> flip. The reasoning below is why it was ever in doubt: 3.6's
hybrid template demonstrably gated on Ollama's top-level `think` flag, but
Qwen3.8 moved its primary control to `reasoning_effort` and routes
`enable_thinking` through `chat_template_kwargs`, which is vLLM-only — Ollama
drops it. The tag ships the card's *thinking* defaults (temp 1.0, top_p 0.95,
pp 0). `THINKS` → **stop** (this would now mean the tag was rebuilt from a
different base, not that the mechanism never worked).

**Gate 2 — full GPU offload.** `ollama ps` → want `100% GPU`. The profile
deliberately does not pin `num_gpu` (mirroring 3.6), so Ollama autofits to *free*
memory; this codebase has measured 17–31 of 34 layers on GPU, decode tracking it
at 6.7 vs 13.5 tok/s. Fix at runtime (`OLLAMA_NUM_GPU=99`, lower `num_ctx`), never
by editing the profile — both arms must keep identical knobs.

### 4.4 Generate

```bash
TUTOR_CALL_MODE=two EVAL_SKIP_RUBRIC=1 \
MODELS_FILE="$PWD/offline_eval/models_qwen38.txt" \
MODE="--multi-turn --subset hg1" \
RESULTS_DIR="$PWD/offline_eval/multi_turn_results/hg1_prod" \
bash offline_eval/run_matrix.sh

vastai destroy instance <id>      # Vast bills storage even while stopped
```

`hg1_prod`, deliberately not `mt100` — these rows must never sit beside
old-engine, judge-scored numbers.

---

## 5. Annotate

```bash
python3 offline_eval/build_viewer.py     # pure stdlib
open offline_eval/viewer_deploy/index.html
```

1. **Import** `offline_eval/manual_grades/mt100_2026-08-19_daniel.json` first, so
   the old-engine verdicts are in the page and you can see the 3.6 reference
   beside the new sessions. Merge is per session, newer `ts` wins.
2. Grade all 68 in the **Grade** tab. **Do not reveal the judge** — there is no
   rubric here anyway, but `peeked: true` excludes a session from pass-rate
   figures.
3. All eight dimensions or it does not count: a partial record is excluded from
   scoring entirely, and an unanswered dimension is *not* a "No".
4. **Export, rename, commit.** Grades live in `localStorage` — per browser
   profile, gone with a cleared cache, invisible to anyone else:

```bash
mv ~/Downloads/manual_grades_68.json \
   offline_eval/manual_grades/hg1_prod_2026-08-<dd>_<who>.json
```

Two annotators on the same sessions would give an inter-rater check the mt100
grading never had. Optional, and the Import merge makes it cheap.

---

## 6. Risks

1. ~~**Thinking suppression unverified on 3.8**~~ — **resolved 2026-08-23**, verified by dry run (§4.3). Gate 1 stays as a cheap regression check.
2. **`passed` misread after the rubric is off** — §3.3. Record `rubric_skipped`.
3. **Mis-minted `hg1`** — §3.6's count check catches it for free.
4. **~3 GB VRAM headroom** — gate 2. Costs time, not correctness.
5. **n=34** — act on a 3+ session gap.
6. **Single annotator** — the old-engine baseline is one person's judgement.
   Consistent with itself, which is what a paired comparison needs, but not an
   inter-rater-validated number.
7. **Quantization** — q4_K_M both arms. A hosted API (OpenRouter serves 3.8 at
   fp8/bf16) would not be comparable to either.

---

## 7. The `hg1` set — 34 scenarios

```
average_geo_direction_001                          long_session_struggler_1470_03
average_geo_scale_001                              non_responder_geo_scale_001
baseline_full_session_error_prone_1467_17          non_responder_geo_session_001
capable_geo_clarification_001                      probe_resistant_geo_scale_001
capable_geo_direction_001                          probe_resistant_geo_session_001
engagement_recovery_non_responder_1468_17          refusal_chain_non_responder_1467_07
engagement_recovery_non_responder_1469_04          refusal_chain_non_responder_1468_06
error_cascade_error_prone_1466_04                  refusal_chain_probe_resistant_1469_05
error_cascade_struggler_1468_12                    remediation_after_exit_ticket_fail_average_1466_09
error_cascade_struggler_1469_16                    remediation_after_exit_ticket_fail_average_1469_14
error_prone_geo_direction_001                      remediation_after_exit_ticket_fail_error_prone_1467_08
help_intensive_struggler_1465_07                   remediation_after_exit_ticket_fail_error_prone_1468_07
long_session_capable_1466_06                       self_correction_average_1469_07
long_session_probe_resistant_1465_07               self_correction_average_1470_06
long_session_probe_resistant_1470_12               session_completion_error_prone_1466_03
session_completion_struggler_1470_07               short_session_probe_resistant_1465_10
speedrun_capable_1466_11                           struggler_geo_direction_001
```
