"""
/gen-chaos-pr  -- generate a chaos test scenario for the source PR and
open a cross-repo PR against Ariznawlll/mo-nightly-regression.

Flow:
  1. Fetch source PR diff/files.
  2. Load chaos + relevant skill docs.
  3. Ask LLM to emit a JSON spec:
       {
         "scenario_name": "cdc_startup_race",
         "summary": "...",
         "files": [
           {"path": "mo-chaos-config/chaos_<name>.yaml", "content": "..."},
           {"path": "mo-chaos-config/scripts/verify_<name>.sh", "content": "..."}
         ],
         "registry_patch": {
           "path": "mo-chaos-config/chaos_test_case.yaml",
           "append": "<yaml snippet to append>"
         }
       }
  4. Apply spec to a clone of mo-nightly-regression (main branch).
  5. Open PR; reply on source PR with the generated PR URL.
"""

from __future__ import annotations

import json
import os
import re
import sys

import _common as c


TARGET_REPO = os.environ.get("CHAOS_TARGET_REPO", "Ariznawlll/mo-nightly-regression")
TARGET_BASE = os.environ.get("CHAOS_TARGET_BASE", "main")

SYSTEM_PROMPT_TMPL = """你是 MatrixOne Chaos 测试专家。任务：根据 PR diff 设计一个 chaos 场景，生成 mo-nightly-regression 仓库需要新增/修改的文件。

## MO 知识库

{skills}

## 仓库结构（mo-nightly-regression, main 分支）

- `mo-chaos-config/chaos_<scenario>.yaml` — chaos 场景配置（目标 pod、故障类型、持续时间）
- `mo-chaos-config/scripts/verify_<scenario>.sh` — 注入故障后的验证脚本（连接 MO，跑断言 SQL，检查日志）
- `mo-chaos-config/chaos_test_case.yaml` — 注册表，需要把新场景追加进去

### 示例 chaos yaml 结构
```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: <scenario-name>
  namespace: mo-system
spec:
  action: pod-kill
  mode: one
  selector:
    namespaces: [mo-system]
    labelSelectors:
      matrixorigin.io/component: CNSet
  duration: '60s'
```

### 示例 verify 脚本结构
```bash
#!/usr/bin/env bash
set -euo pipefail
MO_HOST=${{MO_HOST:-127.0.0.1}}
MO_PORT=${{MO_PORT:-6001}}
mysql -h "$MO_HOST" -P "$MO_PORT" -uroot -p111 -e "<assertion sql>"
# additional assertions...
```

## 输出要求（必须严格输出一个 JSON 代码块，不要其他内容）

```json
{{
  "scenario_name": "snake_case_short_name",
  "summary": "一句话说明该场景测什么",
  "rationale": "为什么这个 PR 需要这个 chaos 场景（结合 diff 解释）",
  "files": [
    {{"path": "mo-chaos-config/chaos_<name>.yaml", "content": "<完整 yaml>"}},
    {{"path": "mo-chaos-config/scripts/verify_<name>.sh", "content": "<完整脚本>"}}
  ],
  "registry_patch": {{
    "path": "mo-chaos-config/chaos_test_case.yaml",
    "append": "<追加到文件末尾的 yaml 片段>"
  }}
}}
```

## 约束
- scenario_name 只能用小写字母、数字、下划线
- 所有路径相对仓库根目录
- 故障注入 mode/selector/duration 必须合理
- verify 脚本必须可独立运行并能 fail-fast（exit non-zero 表示问题）
- 如果 PR 不需要 chaos 测试，返回 `{{"skip": true, "reason": "..."}}`
"""


SCENARIO_RE = re.compile(r"^[a-z0-9_]{3,60}$")


def main() -> int:
    pr_number = os.environ["PR_NUMBER"]
    repo = os.environ.get("SOURCE_REPO") or os.environ["GITHUB_REPOSITORY"]
    cross_token = os.environ.get("CROSS_REPO_TOKEN", "")

    pr = c.fetch_pr(pr_number, repo)
    if not pr.diff.strip():
        c.post_pr_comment(pr_number, repo, f"⚠️ /gen-chaos-pr: 无法获取 PR #{pr_number} 的 diff。")
        return 0

    skills = c.load_skills(pr.files)
    system_prompt = SYSTEM_PROMPT_TMPL.format(skills=skills)
    user_prompt = (
        f"## 源 PR #{pr_number}（{repo}）\n"
        f"**标题：** {pr.title}\n"
        f"**描述：** {pr.body}\n\n"
        f"## 改动文件\n{chr(10).join(pr.files)}\n\n"
        f"## Diff（截断到 {len(pr.diff)} 字符）\n```\n{pr.diff}\n```\n"
    )

    print(f"gen-chaos: PR #{pr_number}", file=sys.stderr)
    raw = c.call_llm(system_prompt, user_prompt, max_tokens=6000)
    try:
        spec = c.extract_json_block(raw)
    except (ValueError, json.JSONDecodeError) as e:
        c.post_pr_comment(pr_number, repo,
                          f"❌ /gen-chaos-pr: LLM 输出解析失败：{e}\n\n<details><summary>原始输出</summary>\n\n```\n{raw[:4000]}\n```\n</details>")
        return 1

    if spec.get("skip"):
        c.post_pr_comment(pr_number, repo,
                          f"➖ /gen-chaos-pr: 跳过。原因：{spec.get('reason', '不需要 chaos 测试')}")
        return 0

    name = spec.get("scenario_name", "")
    if not SCENARIO_RE.match(name):
        c.post_pr_comment(pr_number, repo,
                          f"❌ /gen-chaos-pr: scenario_name `{name}` 不合法（要求 [a-z0-9_]{{3,40}}）。")
        return 1

    files: list[c.GeneratedFile] = []
    for f in spec.get("files", []):
        path, content = f.get("path"), f.get("content")
        if not path or content is None:
            continue
        mode = "100755" if path.endswith(".sh") else "100644"
        files.append(c.GeneratedFile(path=path, content=content, mode=mode))

    registry = spec.get("registry_patch")
    if registry and registry.get("path") and registry.get("append"):
        files.append(c.GeneratedFile(
            path=f"{c.APPEND_PREFIX}{registry['path']}",
            content=registry["append"],
        ))

    if not files:
        c.post_pr_comment(pr_number, repo, "❌ /gen-chaos-pr: LLM 未生成任何文件。")
        return 1

    head_branch = f"bot/chaos-{name}-pr{pr_number}"
    title = f"chaos: add {name} scenario for matrixone#{pr_number}"
    body = (
        f"## Auto-generated by `/gen-chaos-pr`\n\n"
        f"**Source PR:** {repo}#{pr_number} — {pr.title}\n\n"
        f"**Scenario:** `{name}`\n\n"
        f"**Summary:** {spec.get('summary', '(none)')}\n\n"
        f"**Rationale:** {spec.get('rationale', '(none)')}\n\n"
        "---\n*由 AI Test Analyzer 自动生成。请人工 review 配置合理性后合并。*"
    )

    try:
        pr_url = c.open_cross_repo_pr(
            target_repo=TARGET_REPO,
            base_branch=TARGET_BASE,
            head_branch=head_branch,
            title=title,
            body=body,
            files=files,
            token=cross_token,
            path_allowlist=("mo-chaos-config/",),
        )
    except c.DuplicateGeneratedTest as e:
        c.post_pr_comment(
            pr_number,
            repo,
            f"➖ /gen-chaos-pr: 已有重复或高度相似测试，跳过新增。"
            f"生成文件 `{e.generated_path}` 命中已有文件 `{e.existing_path}`"
            f"（相似度 {e.score:.2f}）。",
        )
        return 0

    c.post_pr_comment(pr_number, repo,
                      f"🚀 /gen-chaos-pr: 已为 chaos 场景 `{name}` 创建 PR：{pr_url}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
