# OPC CEO

English

`opc-ceo` is a local-first OPC CEO workspace and decision loop. It turns a fixed six-tab operating workbook into controlled local artifacts, supports staged import review and apply, generates a daily CEO briefing with explicit dispositions, and reconstructs workspace health from on-disk artifacts without exposing sensitive source content.

The repository contains:

- A Python CLI: `opc-workspace`
- A bundled Codex skill: `skills/opc-ceo-office`
- A fixed workbook contract and schema resources
- Integration, boundary, and coverage-gated tests
- Benchmark and evaluation harnesses

## Core Capabilities

- Initialize and validate a secure local workspace
- Stage workbook imports from exported `.xlsx` files
- Review diffs, conflicts, quarantined rows, anomalies, and tombstones
- Seal and apply approved imports into canonical records
- Draft, render, and apply a daily CEO briefing
- Report workspace status from retained artifacts:
  - imports
  - briefings
  - quarantine
  - recovery
  - cleanup
  - audit
- Verify connector receipts before first use
- Package and install the bundled `opc-ceo-office` skill

## CLI

The package exposes one CLI entry point:

```bash
opc-workspace
```

Main commands:

```bash
opc-workspace init --approve
opc-workspace validate
opc-workspace status
opc-workspace backup --output /path/to/backup/opc

opc-workspace import stage --source /path/to/source.xlsx --metadata /path/to/metadata.json
opc-workspace import review --run <run_id>
opc-workspace import resolve --run <run_id> --resolution /path/to/resolution.json
opc-workspace import apply --run <run_id> --confirm <run_id:seal_sha256>

opc-workspace briefing draft --language zh-CN
opc-workspace briefing render --run <run_id> --dispositions /path/to/dispositions.json
opc-workspace briefing apply --run <run_id> --confirm <run_id:seal_sha256>

opc-workspace connector-receipt verify --receipt /path/to/receipt.json
opc-workspace install
```

Use `--format json` when integrating with tools or agents.

## Workflow

1. Verify the connector receipt.
2. Initialize the workspace after explicit approval.
3. Export the fixed Google Sheet as `.xlsx`.
4. Stage the import with bounded metadata.
5. Review and resolve the staged diff.
6. Apply only with the exact approval token.
7. Draft the daily briefing from canonical local records.
8. Collect dispositions, render, and apply the briefing.
9. Use `opc-workspace status --format json` to inspect health and recovery state.

## Development

Requirements:

- Python `3.12`
- `uv`

Install dependencies:

```bash
uv sync --dev
```

Run tests:

```bash
uv run pytest
```

Run the full coverage gate:

```bash
uv run pytest --cov=opc_ceo --cov-branch --cov-fail-under=100
```

Lint and format:

```bash
uv run ruff format --check .
uv run ruff check .
```

Type-check:

```bash
uv run mypy src tests evals spikes
```

Check generated contract resources:

```bash
uv run python -m opc_ceo.contracts generate --check
```

Run the status benchmark:

```bash
uv run python -m opc_ceo.benchmark --phase status --records 1000 --warmup 3 --repeat 20 --stat p95
```

## Repository Layout

```text
src/opc_ceo/         Python implementation
tests/               Integration, boundary, and unit tests
skills/              Bundled Codex skill
evals/               Evaluation harness and cases
evidence/            Captured benchmark and eval outputs
docs/superpowers/    Design spec and implementation plan
spikes/              Focused probes and harness scripts
```

## Important Constraints

- The workbook shape is fixed by the bundled contract. Arbitrary spreadsheet layouts are out of scope.
- Sensitive raw workbook content should stay out of model context.
- Import and briefing apply steps require exact approval tokens.
- `status` is artifact-driven and privacy-bounded. It exposes aggregate health, hashed references, and bounded diagnostics rather than business content.

## Bundled Skill

The bundled skill is at:

- `skills/opc-ceo-office/SKILL.md`

It describes the intended agent workflow for:

- setup
- refresh and import
- daily briefing
- status and recovery

Install it through:

```bash
uv run opc-workspace install
```

## Project Status

Current package version in `pyproject.toml`:

- `0.2.0`

The repository includes the Stage 1 planning artifacts:

- `OPC_CEO_stage1_minimum_assistant_configuration.md`
- `OPC_CEO_stage1_agent_skill_plan.md`
- `OPC_CEO_stage1_implementation_plan.md`

---

简体中文

`opc-ceo` 是一个本地优先的 OPC CEO 工作空间与决策闭环。它把固定的六标签运营工作簿转成受控的本地工件，支持分阶段导入、审阅与应用，生成每日 CEO Briefing，并且可以在不暴露敏感源数据的前提下，从磁盘工件重建工作空间健康状态。

这个仓库包含：

- 一个 Python CLI：`opc-workspace`
- 一个随仓库打包的 Codex Skill：`skills/opc-ceo-office`
- 固定的工作簿契约与 schema 资源
- 带覆盖率门槛的集成、边界与单元测试
- benchmark 与 eval harness

## 核心能力

- 初始化并校验安全的本地工作空间
- 从导出的 `.xlsx` 文件分阶段导入工作簿
- 审阅 diff、冲突、隔离行、异常和 tombstone
- 对批准后的导入进行 seal 和 apply，写入 canonical records
- 起草、渲染并应用每日 CEO Briefing
- 从保留工件中重建工作空间状态：
  - imports
  - briefings
  - quarantine
  - recovery
  - cleanup
  - audit
- 首次使用前校验 connector receipt
- 打包并安装 `opc-ceo-office` Skill

## CLI

这个包暴露一个 CLI 入口：

```bash
opc-workspace
```

主要命令：

```bash
opc-workspace init --approve
opc-workspace validate
opc-workspace status
opc-workspace backup --output /path/to/backup/opc

opc-workspace import stage --source /path/to/source.xlsx --metadata /path/to/metadata.json
opc-workspace import review --run <run_id>
opc-workspace import resolve --run <run_id> --resolution /path/to/resolution.json
opc-workspace import apply --run <run_id> --confirm <run_id:seal_sha256>

opc-workspace briefing draft --language zh-CN
opc-workspace briefing render --run <run_id> --dispositions /path/to/dispositions.json
opc-workspace briefing apply --run <run_id> --confirm <run_id:seal_sha256>

opc-workspace connector-receipt verify --receipt /path/to/receipt.json
opc-workspace install
```

当你要和工具或 agent 集成时，使用 `--format json`。

## 典型流程

1. 先校验 connector receipt。
2. 在明确批准后初始化 workspace。
3. 将固定的 Google Sheet 导出为 `.xlsx`。
4. 用有界 metadata 执行 `import stage`。
5. 审阅并解析 staged diff。
6. 只有拿到精确 approval token 才能 apply。
7. 从本地 canonical records 生成 daily briefing。
8. 收集 disposition，render，然后 apply briefing。
9. 用 `opc-workspace status --format json` 检查健康状态与 recovery 状态。

## 开发

环境要求：

- Python `3.12`
- `uv`

安装依赖：

```bash
uv sync --dev
```

运行测试：

```bash
uv run pytest
```

运行完整覆盖率门槛：

```bash
uv run pytest --cov=opc_ceo --cov-branch --cov-fail-under=100
```

Lint 和格式检查：

```bash
uv run ruff format --check .
uv run ruff check .
```

类型检查：

```bash
uv run mypy src tests evals spikes
```

检查生成的 contract 资源是否漂移：

```bash
uv run python -m opc_ceo.contracts generate --check
```

运行 status benchmark：

```bash
uv run python -m opc_ceo.benchmark --phase status --records 1000 --warmup 3 --repeat 20 --stat p95
```

## 仓库结构

```text
src/opc_ceo/         Python 实现
tests/               集成、边界和单元测试
skills/              随仓库打包的 Codex Skill
evals/               评测 harness 与案例
evidence/            benchmark 与 eval 输出
docs/superpowers/    设计说明与实施计划
spikes/              定向探针与 harness 脚本
```

## 关键约束

- 工作簿形状由内置 contract 固定，不支持任意 spreadsheet 布局。
- 敏感的原始工作簿内容不应进入模型上下文。
- import 与 briefing 的 apply 都要求精确 approval token。
- `status` 基于工件重建，且受隐私约束。它暴露的是聚合健康信息、哈希引用和有界诊断，而不是业务内容。

## Bundled Skill

内置 Skill 位于：

- `skills/opc-ceo-office/SKILL.md`

它定义了面向 agent 的工作流：

- setup
- refresh and import
- daily briefing
- status and recovery

安装方式：

```bash
uv run opc-workspace install
```

## 项目状态

`pyproject.toml` 中当前包版本：

- `0.2.0`

仓库中还保留了 Stage 1 规划工件：

- `OPC_CEO_stage1_minimum_assistant_configuration.md`
- `OPC_CEO_stage1_agent_skill_plan.md`
- `OPC_CEO_stage1_implementation_plan.md`
