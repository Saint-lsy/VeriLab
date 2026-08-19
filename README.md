# VeriLab v1

VeriLab is a deliberately narrow, local-first experiment controller: a Codex Executor may propose
and submit immutable experiments, but only the Controller may issue a formal run ticket, only the
trusted grader may compute an authoritative metric, and only a fresh read-only Reviewer may admit
that metric to a comparison-key leaderboard.

```text
browser chat → Executor Codex → frozen ExperimentSpec → detached Git worktree
             → supervised process → sealed artifacts → trusted grader
             → fresh read-only Reviewer → verified leaderboard
```

The trust level is **recomputable evidence**, not hostile-root isolation. VeriLab prevents an
Executor-authored `score.json` from becoming an official score, detects accidental database/event
and artifact drift, and fails closed when grading or review evidence is incomplete. It does not
claim to resist a malicious root user, a process with the same UID intentionally reading private
labels, or kernel compromise.

## What is implemented

- Strict `ExperimentSpec` v1 validation: argv-only commands, repository-relative `cwd`, safe
  artifact globs, declared resources, secret references, and a clean reachable Git commit.
- One FIFO formal pipeline using detached worktrees, process groups, PID + `/proc` start-time
  identity, heartbeat/log/CPU/GPU samples, timeout, cancellation, exit receipts, and restart audit.
- Controller-private versioned policy, pinned grader-code hashes, artifact roles, metric/cohort/
  protocol contract, immutable `policies/<policy-hash>.json` snapshots, and a policy-bound
  comparison key. Queued experiments keep the policy snapshot active when Controller defaults
  later change.
- Hybrid sealing: artifacts up to 64 MiB (configurable) enter a SHA256 object store; larger files
  retain an absolute path, size, and hash. Pre-review drift fails verification. Post-acceptance loss
  preserves the historical row and marks its evidence degraded.
- Independent metric provenance (`reported`, `computed`, `verified`). The leaderboard reads only
  `verified`; a reported/computed primary-metric mismatch fails verification.
- Fresh Reviewer thread per attempt, read-only Codex sandbox, JSON Schema output, exact bundle hash,
  eight mandatory checks, and validated `event:<seq>` / `sha256:<digest>` references.
- SQLite canonical append-only SHA256 event chain with update/delete triggers. Status and leaderboard
  tables are projections; `verilab audit verify` recomputes the chain, reconstructs and compares
  projections, validates policy snapshots, and checks artifact health.
- FastAPI/Jinja local web UI, structured experiment timeline, audit inbox, SSE replay using
  `Last-Event-ID`, downloadable audit bundles, CSRF checks, and a narrow capability-authenticated CLI.

No CORAL or Argus package is imported at runtime.

## Repository boundaries

The Git repository contains only the reusable VeriLab application, tests, documentation, dependency
lock, and deterministic dummy example. Project-specific adapters and local experiment workspaces
belong under `results/`, whose contents are ignored except for its boundary documentation. Runtime
state, SQLite databases, capability tokens, datasets, predictions, checkpoints, and audit objects
must remain outside the core repository and are covered by `.gitignore` safeguards.

PANTHER, ISLES, and other challenge adapters may remain independent nested Git repositories under
`results/`. This preserves their own scientific history without mixing challenge-specific files
into VeriLab releases.

## Install

Python 3.11 or newer is required. With `uv`:

```bash
uv venv --python 3.12
uv sync --extra dev
.venv/bin/verilab --version
```

## Run the dummy end-to-end project

The example is a template because a formal experiment project must be its own clean Git repository.

```bash
cp -a examples/dummy-project /tmp/verilab-dummy
cd /tmp/verilab-dummy
git init
git add .
git commit -m "dummy baseline"
COMMIT=$(git rev-parse HEAD)
sed "s/__GIT_COMMIT__/$COMMIT/" spec.template.json > /tmp/verilab-dummy-spec.json

STATE=/tmp/verilab-dummy-state
VERILAB_BIN=/path/to/VeriLab/.venv/bin/verilab
"$VERILAB_BIN" policy install policy.json \
  --project-root "$PWD" --state-dir "$STATE"
"$VERILAB_BIN" serve \
  --project-root "$PWD" --state-dir "$STATE"
```

Open `http://127.0.0.1:8765`. Use the `中文` / `English` control in the upper-right corner to
switch the complete structured interface; the choice is stored in a browser cookie and persists
across the dashboard, experiment detail, and audit inbox. Immutable experiment content and raw
audit evidence remain in their original language. In another terminal, use the injected-style
narrow CLI:

```bash
export VERILAB_API_URL=http://127.0.0.1:8765
export VERILAB_CAPABILITY_FILE=/tmp/verilab-dummy-state/capability.token
"$VERILAB_BIN" submit /tmp/verilab-dummy-spec.json
"$VERILAB_BIN" status
```

The default Reviewer invokes the currently authenticated `codex exec`. It uses a new thread,
`--sandbox read-only`, `--json`, and `--output-schema`. Tests use a deterministic reviewer double so
the trust-state machinery is verified without network/model nondeterminism.

## Trusted policy

The policy is installed into the Controller state directory, never the Executor-writable experiment
repository at runtime. Installation resolves and hashes every `grader_code_paths` entry, adds those
digests to the policy, and thereby changes both `policy_hash` and `comparison_key` when the grader
contract changes.

Grader argv supports these Controller substitutions:

- `{manifest}`: sealed artifact manifest
- `{output}`: required grader JSON output
- `{run_dir}`: Controller-assigned run directory
- `{worktree}`: frozen detached experiment worktree
- `{project_root}`: configured experiment project root

The strict grader result is:

```json
{
  "schema_version": 1,
  "protocol_id": "public-oof-v1",
  "cohort": "dummy-four-cases",
  "metrics": {"accuracy": 0.75}
}
```

## State and audit

By default state lives at `~/.local/share/verilab/<project-id>/`; `--state-dir` can override it.

```text
state.sqlite3
objects/sha256/...
runs/<run-id>/
reviewer-bundles/<review-id>/
worktrees/<run-id>/
capability.token
policy.json
policies/<policy-hash>.json
```

The capability token is mode `0600` and authorizes only Controller HTTP mutations exposed by the
narrow CLI. It is not embedded in specs, events, artifacts, or bundles. The service listens on
`127.0.0.1` by default; use an SSH tunnel for remote access.

VeriLab's Codex driver follows the current official non-interactive CLI surface: JSONL events,
structured `--output-schema`, explicit sandbox mode, and executor-session resume. See the
[official Codex developer command reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
and [non-interactive mode guide](https://learn.chatgpt.com/docs/non-interactive-mode).

Run validation with:

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest
```

## Deliberate v1 exclusions

There is no pre-run LLM review, multi-user/RBAC layer, LAN listener, concurrent experiment runner,
distributed GPU scheduler, Docker/UID boundary, remote scorer, artifact pruning, historical CORAL/
Argus import, browser shell, or force-verified human override.
