# mo-test-coverage-bot

LLM-powered slash-command bot for analyzing [`matrixorigin/matrixone`](https://github.com/matrixorigin/matrixone) PRs and auto-generating cross-repo test PRs in [`matrixorigin/mo-nightly-regression`](https://github.com/matrixorigin/mo-nightly-regression).

Lives outside matrixone so the database repo stays free of CI plumbing, LLM secrets, and prompt churn.

## Architecture

```
matrixone PR comment "/analyze-pr"
        │
        ▼   .github/workflows/test-coverage-bot-bridge.yml  (~50 lines, only matrixone touch-point)
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
| `/analyze-pr` | [analyze_pr.py](scripts/analyze_pr.py) | Posts a 6-test-type coverage table on the source PR |
| `/gen-chaos-pr` | [gen_chaos_pr.py](scripts/gen_chaos_pr.py) | Generates a chaos scenario and opens a PR in `mo-nightly-regression` |

Roadmap (~50 LoC each by copying [gen_chaos_pr.py](scripts/gen_chaos_pr.py)):
- `/gen-stability-pr`, `/gen-bigdata-pr`, `/gen-pitr-pr`, `/gen-snapshot-pr`

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
| `CROSS_REPO_TOKEN` | PAT with `repo` scope on `matrixorigin/mo-nightly-regression` (push branches, open PRs). Required only for `/gen-*-pr` commands. |

### 3. (Optional) Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_API_BASE` | `https://models.github.ai/inference` | LLM endpoint base URL |
| `LLM_MODEL` | `openai/gpt-4.1` | Model name |

### 4. Add the bridge workflow to matrixone

Copy the workflow in [examples/matrixone-bridge.md](examples/matrixone-bridge.md) to:
```
matrixorigin/matrixone/.github/workflows/test-coverage-bot-bridge.yml
```

And add one secret on matrixone: `BOT_DISPATCH_TOKEN` (PAT with `Contents: write` on this bot repo). That is the **entire** matrixone-side footprint.

### 5. (Optional) Manual test before bridging

Trigger `Test Coverage Bot` workflow via the Actions tab → `workflow_dispatch`:
- `event_type: analyze-pr`
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

For `/gen-chaos-pr`, also set `CROSS_REPO_TOKEN` to a token with write access to your `mo-nightly-regression` fork (and edit `TARGET_REPO` in `gen_chaos_pr.py` to point to your fork during testing).

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
