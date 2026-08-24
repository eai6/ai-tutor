# Running an eval arm on a rented GPU box

Three commands. Everything else in this file is why they look the way they do.

```bash
# 1. rent — and CHECK IT STARTED (see "The stopped instance" below)
./venv/bin/vastai search offers 'gpu_name=RTX_3090 disk_space>=90 rentable=true reliability>0.98' -o dph
./venv/bin/vastai create instance <ID> --image pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime --disk 90 --ssh --direct
./venv/bin/vastai show instances          # actual_status MUST be "running"
./venv/bin/vastai attach ssh <ID> "$(cat ~/.ssh/id_ed25519_vast.pub)"

# 2. set up (~20 min: deps + a 17 GB model pull)
bash offline_eval/box_setup.sh <ip> <ssh_port>

# 3. run one arm, then fetch BEFORE tearing down
bash offline_eval/box_run.sh   <ip> <port> offline_eval/models_27b_only.txt "math --sample 34 --seed 0" math_27b_v3
bash offline_eval/box_fetch.sh <ip> <port> math_27b_v3
./venv/bin/vastai destroy instance <ID>
```

## Why on the box, and not through an SSH tunnel

Tunnelling inference to a laptop failed three arms:

| arm | damage |
|---|---|
| qwen3.8-27b geography (first attempt) | 24 of 34 sessions deadlocked; median session 1 turn |
| math-27b (first attempt) | 33 connection failures, 6 deadlocks |
| math-27b (second attempt) | 123 connection failures, 30 fatal, 15 deadlocks |

Three mitigations were tried and each reduced but did not remove it: moving the
forward off port 11434 (the Mac's own ollama owns it), a supervisor that
health-checks the endpoint rather than the ssh process, and a longer retry
ladder for connection errors. The forward is the failure class. Running the
app, the DB and the model on one host removes it.

The tunnel is still fine for short work — a smoke test, a single scenario.

## The five traps

**1. The stopped instance.** `vastai create instance` can return
`success: False` and still hand back a contract id. The instance sits at
`intended_status: stopped`, billing, and from the outside looks exactly like
"still pulling the image". On 2026-08-24 that cost ~40 minutes of waiting for
something that was never starting. Check `actual_status`; if it is not
`running`, `vastai start instance <id>`.

**2. SIGHUP kills remote work.** A long script started as
`ssh host 'bash script.sh'` dies when the local ssh does — and a local
`nohup ... &` does not help, because the *remote* bash is what gets the HUP.
Slow work must be `setsid nohup`'d ON the box. Both `box_setup.sh` and
`box_run.sh` do this.

**3. The repo is private.** `git clone https://github.com/eai6/ai-tutor.git`
hangs asking for a username. `box_setup.sh` ships a ~12 MB tarball instead,
which also keeps a GitHub token off a rented third-party host.

**4. Python 3.12.** `requirements.txt` pins Django 6.0.2, which needs 3.12+;
common GPU images ship 3.11 and pip fails with
`No matching distribution found for Django==6.0.2`. `box_setup.sh` builds a
conda env. **Do not downgrade Django to fit the image** — a different Django
is a different app, and the run stops being comparable to the boards it is
meant to join.

**5. macOS AppleDouble files.** `tar` on macOS writes a `._name` companion for
every file. The box then holds 802 `*.yaml` — 401 real scenarios and 401 junk —
and `discover_scenarios` globs `*.yaml`, so the eval would parse the junk as
scenarios. This is the only trap here that produces a WRONG ANSWER rather than
a delay; the others merely waste time. `box_setup.sh` sets `COPYFILE_DISABLE=1`
and excludes `._*`, and `box_run.sh` skips them defensively.

## The DB is shipped, not rebuilt

`box_setup.sh` copies `db.sqlite3` (~58 MB). It carries the warm-up steps, 450
mastery rows and 427 prerequisite edges that make the warm-up reachable at all.
Rebuilding on the box would risk silent divergence in exactly the fixtures the
run depends on — and the warm-up was structurally absent from every board
before 2026-08-24 without anyone noticing.

## Preconditions are checked, not assumed

`box_run.sh` refuses to start unless the subset selects a non-empty set and the
warm-up is reachable. Each has silently invalidated a run:

- a subset tag that did not travel would run over the wrong scenarios, or none;
- a missing model tag makes `run_matrix.sh` print `pull failed — skipping` and
  produce an empty board;
- the warm-up depends on seeded mastery and is invisible when missing.

## Fetch before you destroy

Results exist only on the rented host until `box_fetch.sh` runs. A destroyed
instance takes the board and its trace with it.
