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
]

DIFF_LIMIT_CHARS = 30000


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

def run(cmd: list[str], check: bool = True, env: dict | None = None) -> str:
    """Run a command, return stdout (str). Raises on non-zero when check=True."""
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if check and proc.returncode != 0:
        sys.stderr.write(f"$ {' '.join(cmd)}\n{proc.stdout}{proc.stderr}\n")
        raise RuntimeError(f"command failed (exit {proc.returncode}): {' '.join(cmd)}")
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
    for doc in selected:
        path = os.path.join(SKILL_DIR, doc)
        try:
            with open(path, "r", encoding="utf-8") as f:
                parts.append(f"=== {doc} ===\n{f.read().rstrip()}\n")
        except FileNotFoundError:
            sys.stderr.write(f"warning: skill doc missing: {path}\n")
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


def extract_json_block(text: str) -> dict:
    """Extract the first ```json ... ``` block from LLM output and parse it."""
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
    return json.loads(text[start:end])


# ---------------------------------------------------------------------------
# GitHub interactions
# ---------------------------------------------------------------------------

def post_pr_comment(pr_number: str, repo: str, body: str) -> None:
    # Use --body-file via stdin to avoid arg-length limits and shell escaping.
    proc = subprocess.run(
        ["gh", "pr", "comment", pr_number, "--repo", repo, "--body-file", "-"],
        input=body, text=True, capture_output=True,
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
    workdir: str = "/tmp/cross-repo-work",
) -> str:
    """Clone target repo, write files, push branch, open PR. Returns PR URL."""
    if not token:
        raise RuntimeError("CROSS_REPO_TOKEN not set; cannot push to target repo")

    os.makedirs(workdir, exist_ok=True)
    clone_dir = os.path.join(workdir, target_repo.replace("/", "_"))
    if os.path.exists(clone_dir):
        run(["rm", "-rf", clone_dir])

    auth_url = f"https://x-access-token:{token}@github.com/{target_repo}.git"
    run(["git", "clone", "--depth", "1", "--branch", base_branch, auth_url, clone_dir])

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
        run(["git", "push", "-u", "origin", head_branch])

        pr_url = run([
            "gh", "pr", "create",
            "--repo", target_repo,
            "--base", base_branch,
            "--head", head_branch,
            "--title", title,
            "--body", body,
        ], env={"GH_TOKEN": token}).strip()
        return pr_url
    finally:
        os.chdir(cwd)
