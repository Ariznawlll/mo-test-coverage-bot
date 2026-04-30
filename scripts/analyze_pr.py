"""
/analyze-pr  -- post a 6-test-type coverage analysis as a PR comment.

Reads PR diff + relevant skill docs, asks the LLM to grade coverage across
BVT / stability / chaos / big-data / PITR / snapshot, and posts the markdown
report back to the source PR.
"""

from __future__ import annotations

import os
import sys

import _common as c


SYSTEM_PROMPT_TMPL = """你是 MatrixOne 测试分析专家。任务：分析 PR diff，结合 MO 知识库，判断 6 类测试的覆盖情况。

## MO 知识库

{skills}

## 6 类测试

| 类型 | 仓库 | 说明 |
|-----|------|------|
| BVT | matrixone test/distributed/cases/ | 轻量级 SQL 回归测试 |
| 稳定性 | mo-nightly-regression (main) | TPCH/TPCC/Sysbench/Fulltext-vector 长时间运行 |
| Chaos | mo-nightly-regression (main) | 故障注入（杀 CN/TN/LogService）+ 工作负载 |
| 大数据 | mo-nightly-regression (big_data) | 大规模数据 load + 查询 |
| PITR | mo-nightly-regression (main) | Point-In-Time Recovery 备份恢复 |
| Snapshot | mo-nightly-regression (main) | Snapshot 备份恢复 |

## 输出格式（严格遵守，不要添加额外内容）

## PR #<NUMBER> 测试覆盖分析

### 变更摘要
<简述改了什么、涉及哪些模块>

### 6 类测试覆盖情况

| 测试类型 | 覆盖状态 | 说明 |
|---------|---------|------|
| BVT | ✅/⚠️/➖ | <说明> |
| 稳定性 | ✅/⚠️/➖ | <说明> |
| Chaos | ✅/⚠️/➖ | <说明> |
| 大数据 | ✅/⚠️/➖ | <说明> |
| PITR | ✅/⚠️/➖ | <说明> |
| Snapshot | ✅/⚠️/➖ | <说明> |

图例: ✅ 已覆盖  ⚠️ 需补充  ➖ 不相关

### 建议
<每类需补充的测试给出具体建议；如有 ⚠️ 项，末尾统一提示一次"如需自动补全测试，可评论 `/auto-test-pr`"即可>

## 约束
- 只根据 skill 文档和 diff 内容分析，不要猜测未列出的实现细节
- 对每种测试类型必须给出明确判断
- 不确定时标记为 ➖ 不相关
- 不要推荐 `/gen-chaos-pr`、`/gen-bigdata-pr`、`/gen-stability-pr`、`/gen-pitr-pr`、`/gen-snapshot-pr` 等命令，这些已全部合并到 `/auto-test-pr`
"""


def main() -> int:
    pr_number = os.environ["PR_NUMBER"]
    repo = os.environ.get("SOURCE_REPO") or os.environ["GITHUB_REPOSITORY"]
    bot_repo = os.environ.get("BOT_REPO") or os.environ.get("GITHUB_REPOSITORY") or repo

    pr = c.fetch_pr(pr_number, repo)
    if not pr.diff.strip():
        c.post_pr_comment(pr_number, repo,
                          f"## PR #{pr_number} 测试覆盖分析\n\n⚠️ 无法获取 PR diff。")
        return 0

    skills = c.load_skills(pr.files)
    system_prompt = SYSTEM_PROMPT_TMPL.format(skills=skills)
    user_prompt = (
        f"## PR #{pr_number}\n"
        f"**标题：** {pr.title}\n"
        f"**描述：** {pr.body}\n\n"
        f"## 改动文件\n{chr(10).join(pr.files)}\n\n"
        f"## Diff\n```\n{pr.diff}\n```\n"
    )

    print(f"analyze: PR #{pr_number}, files={len(pr.files)}, diff={len(pr.diff)} chars",
          file=sys.stderr)
    result = c.call_llm(system_prompt, user_prompt)

    run_url = ""
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        run_url = f"\n\n---\n*由 AI Test Analyzer 自动生成 · " \
                  f"[workflow run](https://github.com/{bot_repo}/actions/runs/{run_id})*"

    c.post_pr_comment(pr_number, repo, result + run_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
