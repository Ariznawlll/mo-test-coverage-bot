# matrixone bridge workflow

This file is the **only** piece that needs to land in `matrixorigin/matrixone`.
It receives PR-comment slash commands and forwards them to the bot repo via
`repository_dispatch`.

Place it at `.github/workflows/test-coverage-bot-bridge.yml` in matrixone.

```yaml
name: Test Coverage Bot Bridge

# Forwards PR-comment slash commands to matrixorigin/mo-test-coverage-bot
# via repository_dispatch. All real logic lives in the bot repo.
#
# Supported commands (must appear at the very start of the comment body):
#   /gen-coverage-tests   analyze coverage and auto-generate missing test PRs
#   /auto-test-pr         legacy compatibility alias

on:
  issue_comment:
    types: [created]

permissions:
  pull-requests: read
  issues: read

jobs:
  forward:
    if: >-
      github.event.issue.pull_request &&
      contains(fromJSON('["MEMBER","OWNER","COLLABORATOR"]'), github.event.comment.author_association) &&
      (
        startsWith(github.event.comment.body, '/gen-coverage-tests') ||
        startsWith(github.event.comment.body, '/auto-test-pr')
      )
    runs-on: ubuntu-latest
    steps:
      - name: Determine command
        id: cmd
        env:
          COMMENT_BODY: ${{ github.event.comment.body }}
        run: |
          body="$COMMENT_BODY"
          case "$body" in
            /gen-coverage-tests*) echo "name=gen-coverage-tests" >> "$GITHUB_OUTPUT" ;;
            /auto-test-pr*) echo "name=auto-test-pr" >> "$GITHUB_OUTPUT" ;;
          esac

      - name: Dispatch to bot repo
        env:
          GH_TOKEN: ${{ secrets.BOT_DISPATCH_TOKEN }}
        run: |
          gh api repos/matrixorigin/mo-test-coverage-bot/dispatches \
            -f event_type='${{ steps.cmd.outputs.name }}' \
            -F client_payload[pr_number]='${{ github.event.issue.number }}' \
            -F client_payload[repo]='${{ github.repository }}' \
            -F client_payload[comment_id]='${{ github.event.comment.id }}'
```

## Required matrixone secret

| Name | Scope | Purpose |
|------|-------|---------|
| `BOT_DISPATCH_TOKEN` | matrixone repo secret | PAT (or fine-grained token) with `Contents: write` on `matrixorigin/mo-test-coverage-bot`. Used solely to call the `/dispatches` endpoint. |

That's the **entire** matrixone-side footprint: 1 workflow file (~50 lines) + 1 secret.
Everything else — Python, prompts, LLM keys, cross-repo PR creation — lives in the bot repo.

## Why `repository_dispatch` and not GitHub App / reusable workflow

- **GitHub App**: cleanest in the long run (0 lines in matrixone), but requires
  registering an App + hosting a webhook receiver. Defer.
- **Reusable workflow (`workflow_call`)**: would still require `LLM_API_TOKEN`
  + `CROSS_REPO_TOKEN` to be configured on matrixone — defeats the goal of
  keeping matrixone untouched.
- **`repository_dispatch`**: stateless, native, only one PAT-equivalent secret
  in matrixone (with very narrow scope: write to one repo, no source-code
  access).
