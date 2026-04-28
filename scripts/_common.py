"""
Shared framework for MatrixOne PR test-coverage automation.

Provides reusable building blocks for slash-command driven workflows:
  - PR diff & metadata fetching (via gh CLI)
  - Conditional skill-doc loading based on changed files
  - LLM API invocation (GitHub Models compatible / OpenAI compatible)
  - Cross-repo branch creation + PR opening (via gh CLI)
  - Comment posting & reaction toggling

Each slash command (e.g. /analyze-pr, /gen-chaos-pr) implements a thin
script that wires these primitives together with a domain-specific
prompt and (optionally) a generator that emits files to commit.

Environment variables:
  PR_NUMBER          Source PR number in matrixone repo
  GITHUB_REPOSITORY  e.g. matrixorigin/matrixone
  GITHUB_TOKEN       Default GH token (read PR, post comments)
  CROSS_REPO_TOKEN   PAT with write access to target repo (mo-nightly-regression)
  LLM_API_TOKEN      Token for LLM endpoint (defaults to GITHUB_TOKEN)
  LLM_API_BASE       Default: https://models.github.ai/inference
  LLM_MODEL          Default: openai/gpt-4.1
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable

import requests


SKILL_DIR = "docs/ai-skills"

# Secondary skill directory for bot-local docs (test-generation rules,
# target-repo layouts, etc.) that don't belong in the MO knowledge base.
# Resolved relative to the scripts/ working directory at runtime; set
# LOCAL_SKILL_DIR env var to override in tests.
LOCAL_SKILL_DIR = os.environ.get("LOCAL_SKILL_DIR", "../skills")

# Always-loaded skill docs (small, cheap, give the model baseline context).
ALWAYS_SKILLS = ["module-test-mapping.md", "testing-guide.md"]

# Conditional skill docs keyed by path-prefix substrings in the PR diff.
SKILL_RULES: list[tuple[str, str]] = [
    ("pkg/sql/", "sql-engine.md"),
    ("pkg/vm/engine/", "storage-engine.md"),
    ("pkg/txn/", "transaction.md"),
    ("pkg/lockservice/", "transaction.md"),
    ("pkg/backup/", "backup-restore.md"),
    ("snapshot", "backup-restore.md"),
    ("pitr", "backup-restore.md"),
    ("pkg/cdc/", "cdc.md"),
    ("pkg/fulltext/", "fulltext-vector.md"),
    ("pkg/vectorindex/", "fulltext-vector.md"),
    ("pkg/proxy/", "multi-cn.md"),
    ("pkg/fileservice/", "fileservice.md"),
    ("pkg/cnservice/", "architecture.md"),
    ("pkg/tnservice/", "architecture.md"),
    ("pkg/logservice/", "architecture.md"),
    # Bot-local: generator rules for mo-nightly-regression big_data suite.
    # Matches any scan/plan/storage change likely to need big-data coverage.
    ("pkg/sql/plan/", "big-data-test.md"),
    ("pkg/sql/colexec/", "big-data-test.md"),
    ("pkg/sql/compile/", "big-data-test.md"),
    ("pkg/vm/engine/disttae/", "big-data-test.md"),
]

DIFF_LIMIT_CHARS = 12000
SKILL_LIMIT_CHARS = 20000


@dataclass
class PRContext:
    number: str
    repo: str
    title: str = ""
    body: str = ""
    diff: str = ""
    files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

def _mask(text: str) -> str:
    """Strip embedded tokens so they never leak into logs or PR comments."""
    import re
    # GitHub PATs: ghp_/ghs_/gho_/ghu_/ghr_ + [A-Za-z0-9]{36,}
    text = re.sub(r"gh[pousr]_[A-Za-z0-9]{20,}", "***TOKEN***", text)
    # Anything that looks like `x-access-token:<token>@` in a URL
    text = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", text)
    return text


def run(cmd: list[str], check: bool = True, env: dict | None = None) -> str:
    """Run a command, return stdout (str). Raises on non-zero when check=True.

    All cmd args, stdout, stderr and raised messages are scrubbed of tokens
    so credentials embedded in URLs (e.g. x-access-token:ghp_...@github.com)
    never reach workflow logs or PR comments.
    """
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    safe_cmd = " ".join(_mask(a) for a in cmd)
    if check and proc.returncode != 0:
        sys.stderr.write(f"$ {safe_cmd}\n{_mask(proc.stdout)}{_mask(proc.stderr)}\n")
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {safe_cmd}"
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# PR fetching
# ---------------------------------------------------------------------------

def fetch_pr(pr_number: str, repo: str) -> PRContext:
    ctx = PRContext(number=pr_number, repo=repo)

    info_raw = run(["gh", "pr", "view", pr_number, "--repo", repo,
                    "--json", "title,body,headRefName,headRepository"])
    try:
        info = json.loads(info_raw)
        ctx.title = info.get("title", "")
        ctx.body = (info.get("body") or "")[:4000]
    except json.JSONDecodeError:
        pass

    files_raw = run(["gh", "pr", "diff", pr_number, "--repo", repo, "--name-only"], check=False)
    ctx.files = [f for f in files_raw.splitlines() if f.strip()]

    diff_raw = run(["gh", "pr", "diff", pr_number, "--repo", repo], check=False)
    ctx.diff = diff_raw[:DIFF_LIMIT_CHARS]

    return ctx


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------

def load_skills(changed_files: Iterable[str], extra: Iterable[str] = ()) -> str:
    """Concatenate relevant skill docs into one string."""
    selected: list[str] = list(ALWAYS_SKILLS)
    files_blob = "\n".join(changed_files).lower()
    for rule_match, doc in SKILL_RULES:
        if rule_match.lower() in files_blob and doc not in selected:
            selected.append(doc)
    for doc in extra:
        if doc not in selected:
            selected.append(doc)

    parts: list[str] = []
    total = 0
    for doc in selected:
        # Prefer the MO knowledge base (sparse-checked-out from matrixone's
        # docs/ai-skills/); fall back to bot-local skills/ for docs that
        # describe generator-side rules (e.g. big-data-test.md).
        content = None
        for candidate in (os.path.join(SKILL_DIR, doc),
                          os.path.join(LOCAL_SKILL_DIR, doc)):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    content = f.read().rstrip()
                break
            except FileNotFoundError:
                continue
        if content is None:
            sys.stderr.write(f"warning: skill doc missing: {doc}\n")
            continue
        chunk = f"=== {doc} ===\n{content}\n"
        if total + len(chunk) > SKILL_LIMIT_CHARS:
            # Truncate this doc to fit the cap and stop loading more.
            remaining = max(0, SKILL_LIMIT_CHARS - total)
            if remaining > 200:
                parts.append(chunk[:remaining] + "\n... [truncated]\n")
            sys.stderr.write(
                f"info: skill budget {SKILL_LIMIT_CHARS} reached after {doc}; "
                f"remaining docs skipped\n"
            )
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def call_llm(system_prompt: str, user_prompt: str, *, max_tokens: int = 4096,
             temperature: float = 0.3) -> str:
    api_base = os.environ.get("LLM_API_BASE") or "https://models.github.ai/inference"
    model = os.environ.get("LLM_MODEL") or "openai/gpt-4.1"
    token = os.environ.get("LLM_API_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("missing LLM token (set LLM_API_TOKEN or GITHUB_TOKEN)")

    url = f"{api_base.rstrip('/')}/chat/completions"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


_LENIENT_JSON = json.JSONDecoder(strict=False)


def extract_json_block(text: str) -> dict:
    """Extract the first ```json ... ``` block from LLM output and parse it.

    Uses a lenient decoder that tolerates unescaped control characters in
    strings (strict=False). LLMs frequently embed multi-line YAML/SQL/shell
    content inside a single JSON string value without escaping newlines,
    which a strict ``json.loads`` rejects with "Unterminated string".
    """
    start = text.find("```json")
    if start == -1:
        start = text.find("```")
        if start == -1:
            raise ValueError("no fenced code block found in LLM output")
        start = text.find("\n", start) + 1
    else:
        start = text.find("\n", start) + 1
    end = text.find("```", start)
    if end == -1:
        raise ValueError("unterminated code block in LLM output")
    payload = text[start:end]
    obj, _ = _LENIENT_JSON.raw_decode(payload.lstrip())
    return obj


# ---------------------------------------------------------------------------
# GitHub interactions
# ---------------------------------------------------------------------------

def post_pr_comment(pr_number: str, repo: str, body: str) -> None:
    # Mask any token-shaped substrings defensively: errors bubbled up from
    # subprocess calls may embed credentials (e.g. auth URLs), and we never
    # want those landing in a public PR comment.
    safe_body = _mask(body)
    # Use --body-file via stdin to avoid arg-length limits and shell escaping.
    proc = subprocess.run(
        ["gh", "pr", "comment", pr_number, "--repo", repo, "--body-file", "-"],
        input=safe_body, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr comment failed: {proc.stderr}")


def react_to_comment(comment_id: str, repo: str, content: str) -> None:
    """content: 'eyes' | 'rocket' | 'confused' | '+1' | '-1' | 'laugh' | 'hooray' | 'heart'."""
    if not comment_id:
        return
    run(["gh", "api", f"repos/{repo}/issues/comments/{comment_id}/reactions",
         "-f", f"content={content}", "--silent"], check=False)


# ---------------------------------------------------------------------------
# Cross-repo PR creation
# ---------------------------------------------------------------------------

@dataclass
class GeneratedFile:
    """A file to commit into the target repo."""
    path: str          # relative to repo root, e.g. "mo-chaos-config/foo.yaml"
    content: str
    mode: str = "100644"  # git file mode


def open_cross_repo_pr(
    *,
    target_repo: str,
    base_branch: str,
    head_branch: str,
    title: str,
    body: str,
    files: list[GeneratedFile],
    token: str,
    head_repo: str | None = None,
    workdir: str = "/tmp/cross-repo-work",
) -> str:
    """Write files, push branch, open a PR on ``target_repo``.

    Args:
        target_repo: where the PR lands (e.g. ``matrixorigin/matrixone``).
        head_repo:   where the branch is pushed. When different from
                     ``target_repo`` this runs the standard fork-based
                     workflow (push to fork, PR to upstream). Defaults to
                     ``target_repo`` for same-repo generators.
        token:       PAT with write access to ``head_repo`` and permission
                     to open a PR on ``target_repo``. For fork workflows
                     the fork owner's PAT is sufficient.
    """
    if not token:
        raise RuntimeError("token not set; cannot push to target repo")

    push_repo = head_repo or target_repo

    os.makedirs(workdir, exist_ok=True)
    clone_dir = os.path.join(workdir, target_repo.replace("/", "_"))
    if os.path.exists(clone_dir):
        run(["rm", "-rf", clone_dir])

    # Always clone upstream's base branch so we build on top of the latest
    # state, even when the fork is stale. Push destination is swapped below.
    upstream_url = f"https://x-access-token:{token}@github.com/{target_repo}.git"
    run(["git", "clone", "--depth", "1", "--branch", base_branch, upstream_url, clone_dir])

    cwd = os.getcwd()
    try:
        os.chdir(clone_dir)
        run(["git", "config", "user.name", "mo-test-bot"])
        run(["git", "config", "user.email", "mo-test-bot@users.noreply.github.com"])
        run(["git", "checkout", "-b", head_branch])

        for gf in files:
            full = os.path.join(clone_dir, gf.path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(gf.content)

        run(["git", "add", "-A"])
        # If nothing changed, abort gracefully.
        diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode
        if diff_check == 0:
            raise RuntimeError("generator produced no file changes; nothing to commit")

        run(["git", "commit", "-m", title])

        # Repoint origin to the push destination (the fork, if different).
        if push_repo != target_repo:
            push_url = f"https://x-access-token:{token}@github.com/{push_repo}.git"
            run(["git", "remote", "set-url", "origin", push_url])
        run(["git", "push", "-u", "origin", head_branch])

        # For cross-fork PRs, `gh pr create --head` must be `owner:branch`.
        if push_repo != target_repo:
            fork_owner = push_repo.split("/")[0]
            head_ref = f"{fork_owner}:{head_branch}"
        else:
            head_ref = head_branch

        pr_url = run([
            "gh", "pr", "create",
            "--repo", target_repo,
            "--base", base_branch,
            "--head", head_ref,
            "--title", title,
            "--body", body,
        ], env={"GH_TOKEN": token}).strip()
        return pr_url
    finally:
        os.chdir(cwd)
