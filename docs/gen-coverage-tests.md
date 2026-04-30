# `/gen-coverage-tests` 使用与实现说明

`/gen-coverage-tests` 是 MatrixOne PR 测试覆盖补充入口。维护者在 `matrixorigin/matrixone` 的 PR 评论区输入该命令后，bot 会分析 PR diff 和 `docs/ai-skills` 知识库，判断 6 类测试是否缺失，并为缺口自动生成跨仓 PR。

`/auto-test-pr` 仍保留为兼容别名，但新的文档、群内说明和桥接工作流都建议使用 `/gen-coverage-tests`。

## 触发方式

在 matrixone PR 评论区输入：

```text
/gen-coverage-tests
```

matrixone 侧 bridge workflow 只允许 `MEMBER`、`OWNER`、`COLLABORATOR` 评论触发。bridge 会把 PR 号、源仓库和评论 ID 通过 `repository_dispatch` 转发到本仓库的 `Test Coverage Bot` workflow。

手动调试时也可以在本仓库 Actions 页面触发 `workflow_dispatch`：

```text
event_type: gen-coverage-tests
pr_number: <matrixone PR number>
repo: matrixorigin/matrixone
```

## 生成范围

| 测试类型 | 生成内容 | 目标仓库 |
|---------|----------|----------|
| BVT | `test/distributed/cases/**.sql`，可选 `.result` | `Ariznawlll/matrixone` |
| 稳定性 | `script/stability_cases/**.py` | `Ariznawlll/mo-nightly-regression` |
| Chaos | chaos YAML + verify 脚本 | `Ariznawlll/mo-nightly-regression` |
| 大数据 | big-data SQL 用例 | `Ariznawlll/mo-nightly-regression` |
| PITR | PITR 配置 | `Ariznawlll/mo-nightly-regression` |
| Snapshot | Snapshot 配置 | `Ariznawlll/mo-nightly-regression` |

非 BVT 的 nightly 用例先提交到 `Ariznawlll/mo-nightly-regression`，后续可以人工合入 `matrixorigin/mo-nightly-regression`。BVT 用例提交到 `Ariznawlll/matrixone`。

## BVT `.result` 生成

开启 `BVT_GEN_RESULT=true` 后，bot 会调用 mo-tester 的 `genrs` 模式连接公网 MySQL/MO 生成 `.result` 文件，并随 BVT PR 一起提交。

必需配置：

| 配置 | 说明 |
|------|------|
| `BVT_GEN_RESULT=true` | 开启 `.result` 生成 |
| `BVT_MO_HOST` / `BVT_MO_PORT` | MySQL/MO 连接地址 |
| `BVT_MO_USER` / `BVT_MO_PASSWORD` | 连接用户和密码；建议使用专用低权限用户 `mo_bvt_bot` |
| `BVT_RESULT_DATABASE` | 专用 scratch 库，必须是 `mo_test_coverage_bot` 或 `mo_test_coverage_bot_` 前缀 |

安全限制：

- 默认拒绝使用 `root`、`test_coverage`、`test_team` 等高权限或共享用户生成结果。
- 生成的 BVT SQL 允许 `CREATE TABLE` / `DROP TABLE` 清理自己的测试表。
- 生成的 BVT SQL 不允许 `USE`、`CREATE DATABASE/SCHEMA`、`DROP DATABASE/SCHEMA`、`ALTER DATABASE/SCHEMA`。
- 生成的 BVT SQL 不允许显式引用 `db.table`，避免误碰共享库。
- 测试表名必须使用独立前缀，避免和已有表冲突。
- `BVT_RESULT_DATABASE` 必须是独立 scratch 库，不应指向业务库、历史结果库或团队共享库。

## 去重策略

生成前会对目标仓库已有测试做相似度检查。BVT、稳定性、Chaos、大数据、PITR、Snapshot 都会走同一套去重逻辑。

`DEDUP_SIMILARITY_THRESHOLD` 默认是 `0.88`。如果新生成用例和已有文件高度相似，bot 会跳过新增，并在源 PR 评论里说明命中的已有文件和相似度。

## 配置入口

常用变量在本仓库 GitHub Actions Variables / Secrets 中配置：

| 配置 | 默认值 |
|------|--------|
| `LLM_MODEL` | `openai/gpt-4.1` |
| `BVT_TARGET_REPO` | `Ariznawlll/matrixone` |
| `NIGHTLY_TARGET_REPO` | `Ariznawlll/mo-nightly-regression` |
| `CHAOS_TARGET_REPO` | `Ariznawlll/mo-nightly-regression` |
| `STABILITY_TARGET_REPO` | `Ariznawlll/mo-nightly-regression` |
| `SOURCE_REPO_ALLOWLIST` | `matrixorigin/matrixone` |

核心 secret：

| Secret | 用途 |
|--------|------|
| `LLM_API_TOKEN` | 调用模型 |
| `SOURCE_REPO_TOKEN` | 读取 matrixone PR、评论和 reaction |
| `CROSS_REPO_TOKEN` | 向 nightly 目标仓库推分支并开 PR |
| `BVT_CROSS_TOKEN` | 可选，单独用于 BVT 目标仓库 |
| `BVT_MO_PASSWORD` | 生成 BVT `.result` 时连接数据库 |

## 实现流程

1. matrixone bridge 检查评论者权限和命令前缀。
2. bridge 发送 `repository_dispatch` 到本仓库。
3. `test-coverage-bot.yml` 校验 event type、PR 号和源仓库 allowlist。
4. workflow 拉取 matrixone 的 `docs/ai-skills`。
5. `scripts/auto_test_pr.py` 调用模型输出覆盖分析和结构化 `needs_*` 判断。
6. 对每个缺失类型生成对应测试文件。
7. 生成前做重复用例检查，重复则跳过。
8. 对需要落地的文件开跨仓 PR，并把结果汇总评论回源 PR。
