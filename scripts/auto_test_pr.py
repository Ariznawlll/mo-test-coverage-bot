"""
/auto-test-pr  -- 全自动测试覆盖补充。

单条命令完成：
  1. 分析 PR diff，评估 6 类测试覆盖情况（同 /analyze-pr）
  2. 解析哪些类型标记为 ⚠️（需补充）
  3. 对每个 ⚠️ 类型自动操作：
     - Chaos    → 生成 chaos YAML + verify 脚本，向 mo-nightly-regression 提 PR
     - 稳定性   → 使用 mo-nightly-regression 现有 workflow_dispatch，不生成配置 PR
     - BVT      → 生成 BVT SQL，向 matrixone fork 提 PR
     - 大数据   → 生成 big_data SQL，向 mo-nightly-regression 提 PR
     - PITR     → 生成 PITR 配置，向 mo-nightly-regression 提 PR
     - Snapshot → 生成 Snapshot 配置，向 mo-nightly-regression 提 PR
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

import _common as c


# ---------------------------------------------------------------------------
# Step 1: 分析 + 结构化输出
# ---------------------------------------------------------------------------

ANALYZE_SYSTEM_TMPL = """你是 MatrixOne 测试分析专家。任务：分析 PR diff，评估 6 类测试覆盖情况，并给出可执行建议。

## MO 知识库

{skills}

## 6 类测试

| 类型 | 仓库 | 说明 |
|-----|------|------|
| BVT | matrixone test/distributed/cases/ | 轻量级 SQL 回归测试 |
| 稳定性 | mo-nightly-regression workflow_dispatch | 现有 `stability-test-on-distributed.yaml`，无需新增配置文件 |
| Chaos | mo-nightly-regression (main) | 故障注入（杀 CN/TN/LogService）+ 工作负载 |
| 大数据 | mo-nightly-regression (big_data) | 大规模数据 load + 查询 |
| PITR | mo-nightly-regression (main) | Point-In-Time Recovery 备份恢复 |
| Snapshot | mo-nightly-regression (main) | Snapshot 备份恢复 |

## 输出要求（必须严格输出以下结构，不要额外内容）

先输出 markdown 分析报告，然后输出一个 JSON 代码块：

## PR #<NUMBER> 测试覆盖分析

### 变更摘要
<简述改了什么>

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
<具体建议，BVT 和大数据类给出示例 SQL>

```json
{{
  "needs_chaos": true/false,
  "needs_stability": true/false,
  "needs_bvt": true/false,
  "needs_bigdata": true/false,
  "needs_pitr": true/false,
  "needs_snapshot": true/false
}}
```

## 约束
- 只根据 skill 文档和 diff 内容分析，不猜测
- 对每种类型必须给出明确判断
- 稳定性测试只判断是否建议运行现有 `stability-test-on-distributed.yaml`，不要建议新增 `stability-test/` 配置文件
- JSON 代码块必须在报告最后，单独一个 ```json ... ``` 块
- **不要在建议里提示用户手动运行任何 slash 命令**（如 `/gen-chaos-pr`、`/gen-bigdata-pr` 等已废弃）。
  本命令 `/auto-test-pr` 会根据上述 JSON 自动为每个 ⚠️ 类型生成对应的跨仓 PR，无需用户再触发其他命令。
"""


def extract_needs(raw: str) -> dict:
    """从 LLM 输出中提取结构化 needs_* 判断。"""
    try:
        return c.extract_json_block(raw)
    except (ValueError, json.JSONDecodeError):
        return {}


def strip_json_block(raw: str) -> str:
    """去掉末尾的 json 代码块，只保留 markdown 部分。"""
    return re.sub(r"```json\s*\{.*?\}\s*```", "", raw, flags=re.DOTALL).rstrip()


def duplicate_skip_message(label: str, err: c.DuplicateGeneratedTest) -> str:
    """Format duplicate-test detection as a normal skip result."""
    return (
        f"➖ {label}：已有重复或高度相似测试，跳过新增。"
        f"生成文件 `{err.generated_path}` 命中已有文件 `{err.existing_path}`"
        f"（相似度 {err.score:.2f}）。"
    )


# ---------------------------------------------------------------------------
# Step 2: Chaos 生成（与 gen_chaos_pr.py 共享逻辑）
# ---------------------------------------------------------------------------

NIGHTLY_DEFAULT_REPO = "Ariznawlll/mo-nightly-regression"

CHAOS_TARGET_REPO = os.environ.get("CHAOS_TARGET_REPO", NIGHTLY_DEFAULT_REPO)
CHAOS_TARGET_BASE = os.environ.get("CHAOS_TARGET_BASE", "main")

CHAOS_SYSTEM_TMPL = """你是 MatrixOne Chaos 测试专家。根据 PR diff 设计一个 chaos 场景，生成 mo-nightly-regression 所需的文件。

## MO 知识库

{skills}

## 仓库结构（mo-nightly-regression, main 分支）

- `mo-chaos-config/chaos_<scenario>.yaml` — chaos 场景配置
- `mo-chaos-config/scripts/verify_<scenario>.sh` — 验证脚本
- `mo-chaos-config/chaos_test_case.yaml` — 注册表

### 示例 chaos yaml
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

### 示例 verify 脚本
```bash
#!/usr/bin/env bash
set -euo pipefail
MO_HOST=${{MO_HOST:-127.0.0.1}}
MO_PORT=${{MO_PORT:-6001}}
mysql -h "$MO_HOST" -P "$MO_PORT" -uroot -p111 -e "<assertion sql>"
```

## 输出（只输出一个 JSON 代码块）

```json
{{
  "scenario_name": "snake_case_name",
  "summary": "一句话说明",
  "rationale": "为什么这个 PR 需要这个 chaos 场景",
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
- scenario_name 只能用小写字母、数字、下划线（3-60字符）
- 所有路径相对仓库根目录
- verify 脚本必须 fail-fast
- 如果不需要 chaos 测试，返回 `{{"skip": true, "reason": "..."}}`
"""

SCENARIO_RE = re.compile(r"^[a-z0-9_]{3,60}$")


def gen_chaos(pr: c.PRContext, skills: str, cross_token: str) -> str | None:
    """生成 chaos PR，返回 PR URL 或 None（失败时返回错误说明）。"""
    system_prompt = CHAOS_SYSTEM_TMPL.format(skills=skills)
    user_prompt = (
        f"## 源 PR #{pr.number}（{pr.repo}）\n"
        f"**标题：** {pr.title}\n\n"
        f"## 改动文件\n{chr(10).join(pr.files)}\n\n"
        f"## Diff\n```\n{pr.diff}\n```\n"
    )

    print("auto-test-pr: generating chaos scenario", file=sys.stderr)
    raw = c.call_llm(system_prompt, user_prompt, max_tokens=6000)
    try:
        spec = c.extract_json_block(raw)
    except (ValueError, json.JSONDecodeError) as e:
        return f"❌ Chaos PR 生成失败（LLM 输出解析错误）：{e}"

    if spec.get("skip"):
        return f"➖ Chaos：{spec.get('reason', '不需要 chaos 测试')}"

    name = spec.get("scenario_name", "")
    if not SCENARIO_RE.match(name):
        return f"❌ Chaos PR 生成失败：scenario_name `{name}` 不合法"

    files: list[c.GeneratedFile] = []
    for f in spec.get("files", []):
        path, content = f.get("path"), f.get("content")
        if path and content is not None:
            mode = "100755" if path.endswith(".sh") else "100644"
            files.append(c.GeneratedFile(path=path, content=content, mode=mode))

    patch = spec.get("registry_patch", {})
    if patch.get("path") and patch.get("append"):
        files.append(c.GeneratedFile(
            path=f"{c.APPEND_PREFIX}{patch['path']}",
            content=patch["append"],
        ))

    if not files:
        return "❌ Chaos PR 生成失败：LLM 未输出有效文件"

    branch = f"auto/chaos-{name}-pr{pr.number}"
    pr_title = f"test(chaos): {spec.get('summary', name)} (from matrixone PR #{pr.number})"
    pr_body = (
        f"Auto-generated chaos scenario for matrixone PR "
        f"[#{pr.number}](https://github.com/{pr.repo}/pull/{pr.number}).\n\n"
        f"**场景说明：** {spec.get('summary', '')}\n\n"
        f"**原因：** {spec.get('rationale', '')}\n\n"
        f"---\n*由 auto-test-pr bot 自动生成*"
    )

    try:
        pr_url = c.open_cross_repo_pr(
            target_repo=CHAOS_TARGET_REPO,
            base_branch=CHAOS_TARGET_BASE,
            head_branch=branch,
            files=files,
            title=pr_title,
            body=pr_body,
            token=cross_token,
            path_allowlist=("mo-chaos-config/",),
        )
        return f"✅ Chaos PR 已提交：{pr_url}"
    except c.DuplicateGeneratedTest as e:
        return duplicate_skip_message("Chaos", e)
    except Exception as e:
        return f"❌ Chaos PR 提交失败：{e}"


# ---------------------------------------------------------------------------
# Step 3: 稳定性测试说明
# ---------------------------------------------------------------------------

STABILITY_WORKFLOW_REPO = os.environ.get(
    "STABILITY_WORKFLOW_REPO",
    "matrixorigin/mo-nightly-regression",
)
STABILITY_WORKFLOW_FILE = os.environ.get(
    "STABILITY_WORKFLOW_FILE",
    "stability-test-on-distributed.yaml",
)

TEST_NAME_RE = re.compile(r"^[a-z0-9_]{3,60}$")


def gen_stability(pr: c.PRContext, skills: str, cross_token: str) -> str | None:
    print("auto-test-pr: stability uses existing workflow; no PR generated",
          file=sys.stderr)
    workflow_url = (
        f"https://github.com/{STABILITY_WORKFLOW_REPO}/actions/workflows/"
        f"{STABILITY_WORKFLOW_FILE}"
    )
    return (
        "➖ 稳定性：mo-nightly-regression 已有固定 workflow "
        f"`{STABILITY_WORKFLOW_FILE}`，不需要新增配置文件或提交 PR。"
        f"如需验证，请在 {workflow_url} 手动触发 `workflow_dispatch`，"
        "选择目标 `Repo`/`Ref` 以及 TPCH/TPCC/Sysbench 规模参数。"
    )


# ---------------------------------------------------------------------------
# Step 4: BVT case 生成 → PR to matrixone
# ---------------------------------------------------------------------------

BVT_TARGET_REPO = os.environ.get("BVT_TARGET_REPO", "Ariznawlll/matrixone")
BVT_TARGET_BASE = os.environ.get("BVT_TARGET_BASE", "main")
# By default BVT PRs land directly on the bot owner's matrixone fork. If
# BVT_TARGET_REPO is later pointed back at upstream, BVT_HEAD_REPO can remain
# a fork to open the PR cross-fork.
BVT_HEAD_REPO = os.environ.get("BVT_HEAD_REPO", BVT_TARGET_REPO)

BVT_PROTECTED_DATABASES = {
    "information_schema",
    "big_data_test",
    "dashboard",
    "db1",
    "dragonfly-shanghai-idc",
    "frontend_demo",
    "grafana",
    "jumpserver",
    "mysql",
    "performance_schema",
    "superset_meta",
    "sys",
    "sysbench_db",
    "test",
    "test01",
    "test_commitid",
    "test_cus_reg",
    "test_merge",
    "test_nightly",
    "test_nightly_aws_rc",
    "test_nightly_bak",
    "test_nightly_dis",
    "test_nightly_tke",
    "test_nightly_tke_bak",
    "test_nightly_tke_rc",
    "test_perf_aws",
    "test_qsq",
    "cdc_test",
    "db1_bak",
    "serial_extract_nth_element_fast_path",
}


def _protected_databases() -> set[str]:
    protected = set(BVT_PROTECTED_DATABASES)
    raw = os.environ.get("BVT_PROTECTED_DATABASES", "").strip()
    if raw:
        protected.update(item.strip().lower() for item in raw.split(",") if item.strip())
    return protected


BVT_SYSTEM_TMPL = """你是 MatrixOne BVT 测试专家。根据 PR diff 生成轻量级 SQL 回归测试（BVT）的 `.sql` 文件。**不要**生成 `.result`——预期输出由 bot 或 reviewer 在真实 MO 上用 mo-tester 产出。

## MO 知识库

{skills}

## BVT 说明

- 测试文件位于 matrixone 仓库 `test/distributed/cases/<module>/` 目录
- `.sql` 文件包含要执行的 SQL 语句（每条 SQL 以 `;` 结尾）
- 文件名用 snake_case，反映测试场景
- 每个测试场景聚焦一个功能点，用 `-- comment` 解释每段 SQL 的意图
- SQL 必须是**自闭环**的：`CREATE TABLE` → `INSERT`/DML → `SELECT` 验证 → `DROP TABLE` 清理，避免污染其他 case
- 所有临时表名必须带有 `bvt_<test_name>_` 前缀，避免和已有表重名
- 只使用当前 mo-tester case database 下的非限定表名；不要 `USE <db>`，不要 `CREATE/DROP/ALTER DATABASE`
- 严禁读取或修改这些已有数据库：{protected_databases}

## 输出（只输出一个 JSON 代码块）

```json
{{
  "test_name": "snake_case_name",
  "summary": "一句话说明",
  "module": "子目录名（如 dml/insert、ddl/table、optimizer 等）",
  "sql_content": "<完整 .sql 文件内容>"
}}
```

## 约束
- test_name 只能用小写字母、数字、下划线（3-60 字符）
- SQL 必须完整可执行，包含必要的 CREATE TABLE / DROP TABLE 清理
- SQL 中不要出现 `<database>.<table>` 形式引用；特别不要引用上述受保护数据库
- **不要输出 `result_content` 字段，不要推测 SELECT 结果**——bot/reviewer 会用 `mo-tester -m genrs` 自动生成 golden `.result`
- 如果 PR 不需要 BVT 测试，返回 `{{"skip": true, "reason": "..."}}`
"""


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _bvt_result_database() -> str:
    database = os.environ.get("BVT_RESULT_DATABASE", "").strip()
    if not database:
        raise RuntimeError(
            "BVT_GEN_RESULT is enabled but BVT_RESULT_DATABASE is not set; "
            "configure a dedicated empty scratch database, e.g. "
            "`mo_test_coverage_bot_pr123`"
        )

    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", database):
        raise RuntimeError(
            "BVT_RESULT_DATABASE must be a simple database name containing only "
            "letters, numbers, and underscores"
        )

    lowered = database.lower()
    if lowered in _protected_databases():
        raise RuntimeError(
            f"BVT_RESULT_DATABASE `{database}` is protected; refuse to run BVT "
            "result generation against an existing production/system database"
        )
    if not (lowered == "mo_test_coverage_bot" or lowered.startswith("mo_test_coverage_bot_")):
        raise RuntimeError(
            "BVT_RESULT_DATABASE must be a dedicated bot scratch database whose "
            "name starts with `mo_test_coverage_bot`"
        )
    return database


def _bvt_result_user_denylist() -> set[str]:
    raw = os.environ.get("BVT_RESULT_USER_DENYLIST", "root,test_coverage,test_team")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _strip_sql_comments_and_strings(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    lines = []
    for line in sql.splitlines():
        lines.append(re.sub(r"--.*$", " ", line))
    sql = "\n".join(lines)

    out: list[str] = []
    quote = ""
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == "\\" and i + 1 < len(sql):
                out.extend("  ")
                i += 2
                continue
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.extend("  ")
                    i += 2
                    continue
                quote = ""
            out.append(" ")
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _sql_identifier_pattern() -> str:
    return r"(?:`([^`]+)`|([A-Za-z_][A-Za-z0-9_$]*))"


def _sql_identifier_atom_pattern() -> str:
    return r"(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_$]*)"


def _collect_bvt_qualified_table_targets(clean_sql: str) -> set[str]:
    ident = _sql_identifier_atom_pattern()
    qualified = rf"({ident}\s*\.\s*{ident})"
    patterns = [
        rf"\bcreate\s+(?:temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?{qualified}",
        rf"\bdrop\s+table\s+(?:if\s+exists\s+)?{qualified}",
        rf"\balter\s+table\s+{qualified}",
        rf"\btruncate\s+table\s+{qualified}",
        rf"\binsert\s+into\s+{qualified}",
        rf"\bupdate\s+{qualified}",
        rf"\bdelete\s+from\s+{qualified}",
        rf"\bfrom\s+{qualified}",
        rf"\bjoin\s+{qualified}",
    ]
    targets: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, clean_sql, flags=re.IGNORECASE):
            target = re.sub(r"\s+", "", match.group(1))
            targets.add(target.replace("`", ""))
    return targets


def _collect_bvt_table_targets(clean_sql: str) -> set[str]:
    ident = _sql_identifier_pattern()
    targets: set[str] = set()
    patterns = [
        rf"\bcreate\s+(?:temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?{ident}",
        rf"\bdrop\s+table\s+(?:if\s+exists\s+)?{ident}",
        rf"\balter\s+table\s+{ident}",
        rf"\btruncate\s+table\s+{ident}",
        rf"\binsert\s+into\s+{ident}",
        rf"\bupdate\s+{ident}",
        rf"\bdelete\s+from\s+{ident}",
        rf"\bfrom\s+{ident}",
        rf"\bjoin\s+{ident}",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, clean_sql, flags=re.IGNORECASE):
            target = (match.group(1) or match.group(2) or "").strip()
            if target:
                targets.add(target)
    return targets


def _validate_bvt_sql_safety(sql: str, test_name: str) -> None:
    clean = _strip_sql_comments_and_strings(sql)

    db_stmt = re.search(
        r"\b(?:use|create\s+database|drop\s+database|alter\s+database)\b",
        clean,
        flags=re.IGNORECASE,
    )
    if db_stmt:
        raise ValueError(
            f"SQL contains forbidden database-level statement: `{db_stmt.group(0)}`"
        )

    qualified_targets = sorted(_collect_bvt_qualified_table_targets(clean))
    if qualified_targets:
        raise ValueError(
            "BVT SQL must not use database-qualified table names; unsafe target(s): "
            + ", ".join(qualified_targets[:10])
        )

    expected_prefix = f"bvt_{test_name}_"
    unsafe_targets = sorted(
        table for table in _collect_bvt_table_targets(clean)
        if not table.lower().startswith(expected_prefix)
    )
    if unsafe_targets:
        raise ValueError(
            "BVT table names must use the dedicated prefix "
            f"`{expected_prefix}`; unsafe table target(s): "
            + ", ".join(unsafe_targets[:10])
        )


def _prepare_mo_tester(workdir: str) -> str:
    configured = os.environ.get("MO_TESTER_DIR", "").strip()
    if configured:
        tester_dir = os.path.abspath(configured)
        if not os.path.exists(os.path.join(tester_dir, "run.sh")):
            raise RuntimeError(f"MO_TESTER_DIR does not contain run.sh: {tester_dir}")
        return tester_dir

    tester_dir = os.path.join(workdir, "mo-tester")
    if os.path.exists(tester_dir):
        c.run(["rm", "-rf", tester_dir])

    repo = os.environ.get("MO_TESTER_REPO", "https://github.com/matrixorigin/mo-tester.git")
    ref = os.environ.get("MO_TESTER_REF", "main")
    c.run(["git", "clone", "--depth", "1", "--branch", ref, repo, tester_dir])
    return tester_dir


def _write_mo_tester_config(tester_dir: str, database: str) -> None:
    host = os.environ.get("BVT_MO_HOST", "").strip()
    port = os.environ.get("BVT_MO_PORT", "3306").strip()
    user = os.environ.get("BVT_MO_USER", "root").strip()
    password = os.environ.get("BVT_MO_PASSWORD", "")

    if not host:
        raise RuntimeError("BVT_GEN_RESULT is enabled but BVT_MO_HOST is not set")
    if not port:
        raise RuntimeError("BVT_GEN_RESULT is enabled but BVT_MO_PORT is not set")
    if not user:
        raise RuntimeError("BVT_GEN_RESULT is enabled but BVT_MO_USER is not set")
    if user.lower() in _bvt_result_user_denylist():
        raise RuntimeError(
            f"BVT_MO_USER `{user}` is blocked for BVT result generation; "
            "use a dedicated low-privilege user such as `mo_bvt_bot`"
        )
    if not password:
        raise RuntimeError("BVT_GEN_RESULT is enabled but BVT_MO_PASSWORD is not set")

    content = f"""# Generated by mo-test-coverage-bot at workflow runtime.
jdbc:
  driver: "com.mysql.cj.jdbc.Driver"
  server:
  - addr: {_yaml_string(f"{host}:{port}")}
  database:
    default: {_yaml_string(database)}
  paremeter:
    characterSetResults: "utf8"
    continueBatchOnError: "false"
    useServerPrepStmts: "true"
    alwaysSendSetIsolation: "false"
    useLocalSessionState: "true"
    allowLoadLocalInfile: "true"
    zeroDateTimeBehavior: "CONVERT_TO_NULL"
    failoverReadOnly: "false"
    initialTimeout: 60
    autoReconnect: "true"
    maxReconnects: 4
    serverTimezone: "Asia/Shanghai"
    socketTimeout: 120000

user:
  name: {_yaml_string(user)}
  password: {_yaml_string(password)}
  sysuser: {_yaml_string(user)}
  syspass: {_yaml_string(password)}

debug:
  serverIP: {_yaml_string(host)}
  port: 6060
"""
    with open(os.path.join(tester_dir, "mo.yml"), "w", encoding="utf-8") as f:
        f.write(content)


def _bvt_result_hook(sql_path: str, result_path: str):
    def hook(clone_dir: str) -> None:
        database = _bvt_result_database()
        tester_dir = _prepare_mo_tester(os.path.dirname(clone_dir))
        _write_mo_tester_config(tester_dir, database)

        case_path = os.path.join(clone_dir, sql_path)
        resource_path = os.path.join(clone_dir, "test/distributed/resources")
        result_full = os.path.join(clone_dir, result_path)

        # mo-tester creates and drops a database named after the SQL file
        # basename, so never run genrs directly on the final case filename.
        # Use a guarded scratch basename and copy only the generated .result
        # back to the matrixone tree.
        with tempfile.TemporaryDirectory(prefix="bvt-genrs-") as tmpdir:
            scratch_case = os.path.join(tmpdir, f"{database}.sql")
            scratch_result = os.path.join(tmpdir, f"{database}.result")
            shutil.copyfile(case_path, scratch_case)

            cmd = ["bash", "run.sh", "-n", "-g", "-m", "genrs", "-p", scratch_case]
            if os.path.isdir(resource_path):
                cmd.extend(["-s", resource_path])

            print(
                f"auto-test-pr: generating BVT .result with mo-tester for {sql_path} "
                f"using scratch database {database}",
                file=sys.stderr,
            )
            c.run(cmd, cwd=tester_dir)

            if not os.path.exists(scratch_result):
                raise RuntimeError(
                    f"mo-tester did not generate expected result file: {result_path}"
                )
            os.makedirs(os.path.dirname(result_full), exist_ok=True)
            shutil.copyfile(scratch_result, result_full)

        if os.path.getsize(result_full) == 0:
            raise RuntimeError(f"mo-tester generated an empty result file: {result_path}")
    return hook


def gen_bvt(pr: c.PRContext, skills: str, cross_token: str) -> str | None:
    protected_databases = ", ".join(sorted(_protected_databases()))
    system_prompt = BVT_SYSTEM_TMPL.format(
        skills=skills,
        protected_databases=protected_databases,
    )
    user_prompt = (
        f"## 源 PR #{pr.number}（{pr.repo}）\n"
        f"**标题：** {pr.title}\n\n"
        f"## 改动文件\n{chr(10).join(pr.files)}\n\n"
        f"## Diff\n```\n{pr.diff}\n```\n"
    )

    print("auto-test-pr: generating BVT cases", file=sys.stderr)
    raw = c.call_llm(system_prompt, user_prompt, max_tokens=6000)
    try:
        spec = c.extract_json_block(raw)
    except (ValueError, json.JSONDecodeError) as e:
        return f"❌ BVT PR 生成失败（LLM 解析错误）：{e}"

    if spec.get("skip"):
        return f"➖ BVT：{spec.get('reason', '不需要')}"

    name = spec.get("test_name", "")
    if not TEST_NAME_RE.match(name):
        return f"❌ BVT PR 生成失败：test_name `{name}` 不合法"

    module = spec.get("module", "").strip("/")
    if not module:
        return "❌ BVT PR 生成失败：未指定 module 目录"

    sql_content = spec.get("sql_content", "")
    if not sql_content or len(sql_content) < 20:
        return "❌ BVT PR 生成失败：LLM 未输出有效 sql_content"
    try:
        _validate_bvt_sql_safety(sql_content, name)
    except ValueError as e:
        return f"❌ BVT PR 生成失败：生成的 SQL 可能触碰已有数据库，已阻止执行：{e}"

    base_path = f"test/distributed/cases/{module}/{name}"
    sql_path = f"{base_path}.sql"
    result_path = f"{base_path}.result"
    files = [c.GeneratedFile(path=sql_path, content=sql_content)]
    generate_result = _env_true("BVT_GEN_RESULT")

    branch = f"auto/bvt-{name}-pr{pr.number}"
    pr_title = f"test(bvt): {spec.get('summary', name)} (from PR #{pr.number})"
    added_files = f"`{sql_path}`"
    if generate_result:
        added_files = f"`{sql_path}`, `{result_path}`"
        result_note = (
            f"`.result` was generated by mo-tester in `genrs` mode against "
            f"the configured MO/MySQL endpoint.\n"
        )
    else:
        result_note = (
            f"### Reviewer checklist\n"
            f"- [ ] Run `cd mo-tester && ./run.sh -p ../{sql_path} -m genrs -g` "
            f"against a local MO instance to produce `{result_path}`\n"
            f"- [ ] Sanity-check the generated `.result` matches intended semantics\n"
            f"- [ ] Commit both `.sql` and `.result` before merging\n"
        )
    pr_body = (
        f"Auto-generated BVT cases for matrixone PR "
        f"[#{pr.number}](https://github.com/{pr.repo}/pull/{pr.number}).\n\n"
        f"**Summary:** {spec.get('summary', '')}\n\n"
        f"**Added:** {added_files}\n\n"
        f"{result_note}\n"
        f"---\n*由 auto-test-pr bot 自动生成*"
    )

    try:
        pr_url = c.open_cross_repo_pr(
            target_repo=BVT_TARGET_REPO,
            base_branch=BVT_TARGET_BASE,
            head_branch=branch,
            head_repo=BVT_HEAD_REPO,
            files=files,
            title=pr_title,
            body=pr_body,
            token=cross_token,
            path_allowlist=("test/distributed/cases/",),
            before_commit=_bvt_result_hook(sql_path, result_path) if generate_result else None,
        )
        return f"✅ BVT PR 已提交：{pr_url}"
    except c.DuplicateGeneratedTest as e:
        return duplicate_skip_message("BVT", e)
    except Exception as e:
        return f"❌ BVT PR 提交失败：{e}"


# ---------------------------------------------------------------------------
# Step 5: 通用 nightly-regression PR 生成（大数据/PITR/Snapshot）
# ---------------------------------------------------------------------------

NIGHTLY_TARGET_REPO = os.environ.get("NIGHTLY_TARGET_REPO", NIGHTLY_DEFAULT_REPO)

BIGDATA_SYSTEM_TMPL = """你是 MatrixOne 大数据测试专家。根据 PR diff 生成大规模数据查询用例，提交到 mo-nightly-regression 仓库的 `big_data` 分支。

## 知识库（必读）

{skills}

`big-data-test.md` 描述了：
- 目录结构（`tools/mo-regression-test/cases/big_data_test/04_QUERIES/`）
- 已 load 的表（默认用 `table_basic_for_load_100M`）
- 每列的数据分布和"适合做什么谓词"
- col1 / col5 / col21 上经采样验证存在的字面量
- 文件格式（`-- @<case_name>` 标签）
- **禁止生成 `.result`**——golden 由 reviewer 在真实环境跑出来

严格按 `big-data-test.md` 的约定生成；表名、列名、字面量都要来自那里给出的可选集合。

## 输出（只输出一个 JSON 代码块）

```json
{{
  "test_name": "snake_case_name",
  "summary": "一句话说明这组 case 在测什么",
  "module": "04_QUERIES",
  "sql_content": "-- @case1\\nselect ... from big_data_test.table_basic_for_load_100M where ...;\\n-- @case2\\nselect ...;\\n"
}}
```

## 约束
- test_name 只能用小写字母、数字、下划线（3-60 字符）；文件名会被固化为 `NN_<test_name>_100M.sql`（NN 是目录下下一个可用编号）
- module 只允许 `04_QUERIES` / `05_ALTER_TABLE` / `06_CLONE` / `07_WINDOW`；新增查询统一放 `04_QUERIES`
- sql_content 必须：
  * 每条 case 用 `-- @<name>` 开头，`<name>` 是 snake_case 且在文件内唯一
  * 表名全部 schema 前缀：`big_data_test.<table>`
  * 使用知识库里列出的表和字面量，不要凭空造新表 / 新字面量
  * 不输出 `.result` 内容，不输出期望结果
- 不需要大数据覆盖时返回 `{{"skip": true, "reason": "..."}}`
"""

PITR_SYSTEM_TMPL = """你是 MatrixOne PITR 测试专家。根据 PR diff 生成 Point-In-Time Recovery 测试场景，用于 mo-nightly-regression 仓库。

## MO 知识库

{skills}

## PITR 测试说明

- PITR 测试验证备份、时间点恢复的正确性
- 配置文件位于 `pitr-test/` 目录（mo-nightly-regression, main 分支）
- 脚本需要：创建数据 → 备份 → 修改/删除数据 → 恢复到时间点 → 验证数据一致性

## 输出（只输出一个 JSON 代码块）

```json
{{
  "test_name": "snake_case_name",
  "summary": "一句话说明",
  "files": [
    {{"path": "pitr-test/<name>.yaml", "content": "<完整配置>"}},
    {{"path": "pitr-test/scripts/<name>.sh", "content": "<验证脚本>"}}
  ]
}}
```

## 约束
- test_name 只能用小写字母、数字、下划线（3-60字符）
- 脚本必须 fail-fast（set -euo pipefail）
- 如果不需要 PITR 测试，返回 `{{"skip": true, "reason": "..."}}`
"""

SNAPSHOT_SYSTEM_TMPL = """你是 MatrixOne Snapshot 测试专家。根据 PR diff 生成 Snapshot 备份恢复测试场景，用于 mo-nightly-regression 仓库。

## MO 知识库

{skills}

## Snapshot 测试说明

- Snapshot 测试验证快照备份和跨账号恢复
- 配置文件位于 `snapshot-test/` 目录（mo-nightly-regression, main 分支）
- 脚本需要：创建 snapshot → 恢复 → 验证数据

## 输出（只输出一个 JSON 代码块）

```json
{{
  "test_name": "snake_case_name",
  "summary": "一句话说明",
  "files": [
    {{"path": "snapshot-test/<name>.yaml", "content": "<完整配置>"}},
    {{"path": "snapshot-test/scripts/<name>.sh", "content": "<验证脚本>"}}
  ]
}}
```

## 约束
- test_name 只能用小写字母、数字、下划线（3-60字符）
- 脚本必须 fail-fast（set -euo pipefail）
- 如果不需要 Snapshot 测试，返回 `{{"skip": true, "reason": "..."}}`
"""


def _gen_nightly_pr(
    label: str,
    system_tmpl: str,
    pr: c.PRContext,
    skills: str,
    cross_token: str,
    target_branch: str,
    pr_prefix: str,
    path_allowlist: tuple[str, ...],
) -> str | None:
    system_prompt = system_tmpl.format(skills=skills)
    user_prompt = (
        f"## 源 PR #{pr.number}（{pr.repo}）\n"
        f"**标题：** {pr.title}\n\n"
        f"## 改动文件\n{chr(10).join(pr.files)}\n\n"
        f"## Diff\n```\n{pr.diff}\n```\n"
    )

    print(f"auto-test-pr: generating {label} config", file=sys.stderr)
    raw = c.call_llm(system_prompt, user_prompt, max_tokens=4000)
    try:
        spec = c.extract_json_block(raw)
    except (ValueError, json.JSONDecodeError) as e:
        return f"❌ {label} PR 生成失败（LLM 解析错误）：{e}"

    if spec.get("skip"):
        return f"➖ {label}：{spec.get('reason', '不需要')}"

    name = spec.get("test_name", "")
    if not TEST_NAME_RE.match(name):
        return f"❌ {label} PR 生成失败：test_name `{name}` 不合法"

    files: list[c.GeneratedFile] = []
    for f in spec.get("files", []):
        path, content = f.get("path"), f.get("content")
        if path and content is not None:
            mode = "100755" if path.endswith(".sh") else "100644"
            files.append(c.GeneratedFile(path=path, content=content, mode=mode))

    if not files:
        return f"❌ {label} PR 生成失败：LLM 未输出有效文件"

    branch = f"auto/{pr_prefix}-{name}-pr{pr.number}"
    pr_title = f"test({pr_prefix}): {spec.get('summary', name)} (from matrixone PR #{pr.number})"
    pr_body = (
        f"Auto-generated {label} test for matrixone PR "
        f"[#{pr.number}](https://github.com/{pr.repo}/pull/{pr.number}).\n\n"
        f"**说明：** {spec.get('summary', '')}\n\n"
        f"---\n*由 auto-test-pr bot 自动生成*"
    )

    try:
        pr_url = c.open_cross_repo_pr(
            target_repo=NIGHTLY_TARGET_REPO,
            base_branch=target_branch,
            head_branch=branch,
            files=files,
            title=pr_title,
            body=pr_body,
            token=cross_token,
            path_allowlist=path_allowlist,
        )
        return f"✅ {label} PR 已提交：{pr_url}"
    except c.DuplicateGeneratedTest as e:
        return duplicate_skip_message(label, e)
    except Exception as e:
        return f"❌ {label} PR 提交失败：{e}"


BIGDATA_MODULE_RE = re.compile(r"^0[4-7]_[A-Z_]+$")


def gen_bigdata(pr, skills, cross_token):
    """Big-data generator: emits a single .sql file (no .result) under
    tools/mo-regression-test/cases/big_data_test/<module>/."""
    system_prompt = BIGDATA_SYSTEM_TMPL.format(skills=skills)
    user_prompt = (
        f"## 源 PR #{pr.number}（{pr.repo}）\n"
        f"**标题：** {pr.title}\n\n"
        f"## 改动文件\n{chr(10).join(pr.files)}\n\n"
        f"## Diff\n```\n{pr.diff}\n```\n"
    )

    print("auto-test-pr: generating 大数据 case", file=sys.stderr)
    raw = c.call_llm(system_prompt, user_prompt, max_tokens=4000)
    try:
        spec = c.extract_json_block(raw)
    except (ValueError, json.JSONDecodeError) as e:
        return f"❌ 大数据 PR 生成失败（LLM 解析错误）：{e}"

    if spec.get("skip"):
        return f"➖ 大数据：{spec.get('reason', '不需要')}"

    name = spec.get("test_name", "")
    if not TEST_NAME_RE.match(name):
        return f"❌ 大数据 PR 生成失败：test_name `{name}` 不合法"

    module = spec.get("module", "04_QUERIES")
    if not BIGDATA_MODULE_RE.match(module):
        return f"❌ 大数据 PR 生成失败：module `{module}` 不合法（期望 04_QUERIES / 05_ALTER_TABLE / 06_CLONE / 07_WINDOW）"

    sql = spec.get("sql_content", "")
    if "-- @" not in sql or len(sql) < 20:
        return "❌ 大数据 PR 生成失败：sql_content 缺少 `-- @name` 注解或过短"

    # Prefix with `zz_` so the file sorts at the end of the target dir and
    # doesn't collide with the existing NN_ numbering. Reviewer renames
    # during merge.
    filename = f"zz_{name}_100M.sql"
    target_path = f"tools/mo-regression-test/cases/big_data_test/{module}/{filename}"
    files = [c.GeneratedFile(path=target_path, content=sql)]

    branch = f"auto/bigdata-{name}-pr{pr.number}"
    pr_title = f"test(big_data): {spec.get('summary', name)} (from matrixone PR #{pr.number})"
    pr_body = (
        f"Auto-generated big-data query cases for matrixone PR "
        f"[#{pr.number}](https://github.com/{pr.repo}/pull/{pr.number}).\n\n"
        f"**Summary:** {spec.get('summary', '')}\n\n"
        f"**Added:** `{target_path}`\n\n"
        f"### Reviewer checklist\n"
        f"- [ ] Rename `zz_` prefix to next free NN in `{module}/` (current max + 1)\n"
        f"- [ ] Run each case against a 100M-row `big_data_test` to produce "
        f"`golden/big_data_test/{module}/{filename.replace('.sql', '.result')}`\n"
        f"- [ ] Verify literals still match real data distribution\n\n"
        f"---\n*由 auto-test-pr bot 自动生成；golden 由 reviewer 人工补充*"
    )

    try:
        pr_url = c.open_cross_repo_pr(
            target_repo=NIGHTLY_TARGET_REPO,
            base_branch=os.environ.get("BIGDATA_TARGET_BASE", "big_data"),
            head_branch=branch,
            files=files,
            title=pr_title,
            body=pr_body,
            token=cross_token,
            path_allowlist=("tools/mo-regression-test/cases/big_data_test/",),
        )
        return f"✅ 大数据 PR 已提交：{pr_url}"
    except c.DuplicateGeneratedTest as e:
        return duplicate_skip_message("大数据", e)
    except Exception as e:
        return f"❌ 大数据 PR 提交失败：{e}"


def gen_pitr(pr, skills, cross_token):
    return _gen_nightly_pr("PITR", PITR_SYSTEM_TMPL, pr, skills, cross_token,
                           target_branch=os.environ.get("PITR_TARGET_BASE", "main"),
                           pr_prefix="pitr",
                           path_allowlist=("pitr-test/",))


def gen_snapshot(pr, skills, cross_token):
    return _gen_nightly_pr("Snapshot", SNAPSHOT_SYSTEM_TMPL, pr, skills, cross_token,
                           target_branch=os.environ.get("SNAPSHOT_TARGET_BASE", "main"),
                           pr_prefix="snapshot",
                           path_allowlist=("snapshot-test/",))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    pr_number = os.environ["PR_NUMBER"]
    repo = os.environ.get("SOURCE_REPO") or os.environ["GITHUB_REPOSITORY"]
    bot_repo = os.environ.get("BOT_REPO") or os.environ.get("GITHUB_REPOSITORY") or repo
    cross_token = os.environ.get("CROSS_REPO_TOKEN", "")
    bvt_token = os.environ.get("BVT_CROSS_TOKEN") or cross_token

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url_suffix = (
        f"\n\n---\n*由 AI Test Analyzer 自动生成 · "
        f"[workflow run](https://github.com/{bot_repo}/actions/runs/{run_id})*"
    ) if run_id else ""

    pr = c.fetch_pr(pr_number, repo)
    if not pr.diff.strip():
        c.post_pr_comment(pr_number, repo,
                          f"## PR #{pr_number} 测试覆盖分析\n\n⚠️ 无法获取 PR diff。{run_url_suffix}")
        return 0

    skills = c.load_skills(pr.files)

    # --- Step 1: 分析 ---
    system_prompt = ANALYZE_SYSTEM_TMPL.format(skills=skills)
    user_prompt = (
        f"## PR #{pr_number}\n"
        f"**标题：** {pr.title}\n"
        f"**描述：** {pr.body}\n\n"
        f"## 改动文件\n{chr(10).join(pr.files)}\n\n"
        f"## Diff\n```\n{pr.diff}\n```\n"
    )

    print(f"auto-test-pr: PR #{pr_number}, files={len(pr.files)}, diff={len(pr.diff)} chars",
          file=sys.stderr)
    analysis_raw = c.call_llm(system_prompt, user_prompt, max_tokens=2000)
    needs = extract_needs(analysis_raw)
    analysis_md = strip_json_block(analysis_raw)

    # --- Step 2: 自动生成 cross-repo PR ---
    action_results: list[str] = []

    for flag, fn in [
        ("needs_chaos",     lambda: gen_chaos(pr, skills, cross_token)),
        ("needs_stability", lambda: gen_stability(pr, skills, cross_token)),
        ("needs_bvt",       lambda: gen_bvt(pr, skills, bvt_token)),
        ("needs_bigdata",   lambda: gen_bigdata(pr, skills, cross_token)),
        ("needs_pitr",      lambda: gen_pitr(pr, skills, cross_token)),
        ("needs_snapshot",  lambda: gen_snapshot(pr, skills, cross_token)),
    ]:
        if needs.get(flag):
            result = fn()
            if result:
                action_results.append(result)

    # --- Step 3: 拼装最终评论 ---
    comment_parts = [analysis_md]

    if action_results:
        comment_parts.append("\n### 🤖 自动生成的测试 PR\n")
        comment_parts.extend(action_results)

    comment_parts.append(run_url_suffix)
    c.post_pr_comment(pr_number, repo, "\n".join(comment_parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
