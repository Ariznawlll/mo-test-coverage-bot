# mo-test-coverage-bot

LLM-powered slash-command bot for analyzing [`matrixorigin/matrixone`](https://github.com/matrixorigin/matrixone) PRs and auto-generating test PRs in [`Ariznawlll/mo-nightly-regression`](https://github.com/Ariznawlll/mo-nightly-regression), ready to be manually merged into upstream [`matrixorigin/mo-nightly-regression`](https://github.com/matrixorigin/mo-nightly-regression).

Lives outside matrixone so the database repo stays free of CI plumbing, LLM secrets, and prompt churn.

## Architecture

```
matrixone PR comment "/gen-coverage-tests"
        │
        ▼   .github/workflows/test-coverage-bot-bridge.yml  (~40 lines, only matrixone touch-point)
   gh api repos/<bot>/dispatches  →  repository_dispatch
        │
        ▼   THIS REPO  .github/workflows/test-coverage-bot.yml
        │ • sparse-checkout matrixone main:docs/ai-skills
        │ • route by event_type → scripts/<handler>.py
        ▼
   scripts/_common.py
        │ • fetch_pr / load_skills / call_llm
        │ • post_pr_comment / react_to_comment
        │ • open_cross_repo_pr  (push branch + open PR in mo-nightly-regression)
```

## Slash commands

| Command | Handler | Effect |
|---------|---------|--------|
| `/gen-coverage-tests` | [auto_test_pr.py](scripts/auto_test_pr.py) | Analyzes 6-test-type coverage and auto-opens cross-repo PRs for every ⚠️ gap |

`/auto-test-pr` is kept as a compatibility alias for existing comments and bridge deployments. New user-facing docs should use `/gen-coverage-tests`.

Legacy one-shot commands `/analyze-pr` and `/gen-chaos-pr` are kept in the script directory for local debugging, but the matrixone bridge only forwards `/gen-coverage-tests` and its `/auto-test-pr` alias. Do not advertise the legacy debug commands in user-facing output.

See [docs/gen-coverage-tests.md](docs/gen-coverage-tests.md) for the full usage and implementation notes.

## Setup

### 1. Create the repo
```
matrixorigin/mo-test-coverage-bot      (or your fork)
```

### 2. Configure secrets (Settings → Secrets and variables → Actions)

| Secret | Purpose |
|--------|---------|
| `LLM_API_TOKEN` | LLM endpoint token. Default endpoint is GitHub Models — use a PAT with `models:read`. |
| `SOURCE_REPO_TOKEN` | PAT with `repo` scope on `matrixorigin/matrixone` (read PR diff, post comments, set reactions). |
| `CROSS_REPO_TOKEN` | PAT with `repo` scope on `Ariznawlll/mo-nightly-regression` (push branches, open PRs). |
| `BVT_CROSS_TOKEN` | Optional PAT for BVT PRs. If unset, `CROSS_REPO_TOKEN` is reused. |
| `BVT_MO_PASSWORD` | Optional password used by mo-tester when `BVT_GEN_RESULT=true`. |

### 3. (Optional) Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_API_BASE` | `https://models.github.ai/inference` | LLM endpoint base URL |
| `LLM_MODEL` | `openai/gpt-4.1` | Model name |
| `BVT_TARGET_REPO` | `Ariznawlll/matrixone` | Repo where generated BVT PRs land |
| `BVT_GEN_RESULT` | `false` | Set to `true` to run mo-tester in `genrs` mode and commit `.result` with BVT PRs. May also be configured as a secret. |
| `BVT_MO_HOST` | unset | Public MO/MySQL-compatible host for BVT result generation. May also be configured as a secret. |
| `BVT_MO_PORT` | `3306` | Public MO/MySQL-compatible port for BVT result generation. May also be configured as a secret. |
| `BVT_MO_USER` | `root` | User for BVT result generation. May also be configured as a secret. |
| `BVT_RESULT_DATABASE` | unset | Required when `BVT_GEN_RESULT=true`. Must be a dedicated empty scratch database named `mo_test_coverage_bot` or prefixed with `mo_test_coverage_bot_`; protected/existing production databases are refused. |
| `BVT_RESULT_USER_DENYLIST` | `root,test_coverage,test_team` | Comma-separated MySQL users that must never be used for automated BVT result generation. |
| `BVT_PROTECTED_DATABASES` | built-in production/system list | Comma-separated database names that generated BVT SQL must never `USE`, create/drop/alter, or reference explicitly. |
| `MO_TESTER_REPO` | `https://github.com/matrixorigin/mo-tester.git` | mo-tester repository to clone when generating `.result`. |
| `MO_TESTER_REF` | `main` | mo-tester branch/tag to use. |
| `MO_TESTER_DIR` | unset | Optional preinstalled mo-tester directory on a self-hosted runner. |
| `NIGHTLY_TARGET_REPO` | `Ariznawlll/mo-nightly-regression` | Repo where big-data/PITR/Snapshot PRs land |
| `CHAOS_TARGET_REPO` | `Ariznawlll/mo-nightly-regression` | Repo where Chaos PRs land |
| `DEDUP_SIMILARITY_THRESHOLD` | `0.88` | Similarity threshold for skipping generated tests that already exist in the target repo. Applies to BVT, Chaos, stability, big-data, PITR, and Snapshot. |
| `STABILITY_TARGET_REPO` | `Ariznawlll/mo-nightly-regression` | Repo where generated stability script-case PRs land. |
| `STABILITY_TARGET_BASE` | `main` | Base branch for generated stability script-case PRs. |
| `STABILITY_WORKFLOW_REPO` | `matrixorigin/mo-nightly-regression` | Repo containing the existing stability `workflow_dispatch`. |
| `STABILITY_WORKFLOW_FILE` | `stability-test-on-distributed.yaml` | Existing stability workflow file that launches generated `script/stability_cases/*.py` cases. |
| `SOURCE_REPO_ALLOWLIST` | `matrixorigin/matrixone` | Comma-separated source repos accepted from dispatch/workflow inputs |

### 4. Add the bridge workflow to matrixone

Copy the workflow in [examples/matrixone-bridge.md](examples/matrixone-bridge.md) to:
```
matrixorigin/matrixone/.github/workflows/test-coverage-bot-bridge.yml
```

And add one secret on matrixone: `BOT_DISPATCH_TOKEN` (PAT with `Contents: write` on this bot repo). That is the **entire** matrixone-side footprint.

### 5. (Optional) Manual test before bridging

Trigger `Test Coverage Bot` workflow via the Actions tab → `workflow_dispatch`:
- `event_type: gen-coverage-tests`
- `pr_number: 24178`
- `repo: matrixorigin/matrixone`

## Local development

```bash
cd scripts
ln -sf ../docs ./docs                          # workflow runtime layout
mkdir -p ../docs && \
  ln -sf ~/code/matrixone/docs/ai-skills ../docs/ai-skills

export PR_NUMBER=24178
export GITHUB_REPOSITORY=matrixorigin/matrixone
export GITHUB_TOKEN=$(gh auth token)
export LLM_API_TOKEN="$GITHUB_TOKEN"           # if using GitHub Models
python analyze_pr.py
```

For local generation, set `CROSS_REPO_TOKEN` to a token with write access to the configured nightly target repo. BVT generation uses `BVT_CROSS_TOKEN` when present, otherwise `CROSS_REPO_TOKEN`.

## Adding a new generator

1. Copy `scripts/gen_chaos_pr.py` → `scripts/gen_<type>_pr.py`.
2. Replace the system prompt + JSON output schema with the new test type's expectations (see knowledge base in `matrixone:docs/ai-skills/*.md`).
3. Adjust `TARGET_REPO` / `TARGET_BASE` if needed (e.g. `big_data` branch).
4. Add cases in `.github/workflows/test-coverage-bot.yml`:
   - `on.repository_dispatch.types`
   - the `Route to handler` step
5. Mirror the same in [examples/matrixone-bridge.md](examples/matrixone-bridge.md) so matrixone forwards the new command.

## Authorization model

The bridge workflow in matrixone restricts dispatching to comments from `MEMBER`/`OWNER`/`COLLABORATOR` (`author_association`). The bot repo trusts whatever matrixone forwards; do not point unrelated repos at the bot's `repository_dispatch` endpoint.
