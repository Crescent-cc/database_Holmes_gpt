# Database HolmesGPT

面向 MySQL 性能故障诊断的 Agentic Troubleshooting Assistant。

## 项目概述

Database HolmesGPT 通过自研轻量 Agent Runtime，结合 MySQL 只读工具集、ReAct 多轮推理、诊断 Workflow、上下文裁剪、证据存储和 HITL 审批机制，实现对慢 SQL、索引问题、锁等待、大表治理等场景的自动诊断与优化建议生成。

**核心能力：** 自动排查 → 自动收集证据 → 根因分析 → 优化建议生成 → 高风险操作人工审批。

## 架构

```
User / CLI
    ↓
DatabaseHolmesInvestigator
    ↓
PromptBuilder + ContextManager
    ↓
ReActLoop
    ├─ LLMClient
    ├─ ToolExecutor → ApprovalPolicy → ToolRegistry → Toolset(MySQL)
    └─ ObservationCompressor + EvidenceStore
    ↓
DiagnosisResult(answer + evidence_ids + tool traces)
```

这次架构按 HolmesGPT 的 agent 思路重构，但只按需引入：

- **Investigator**：统一一次诊断的入口，调用方不再直接拼 ReActLoop 依赖。
- **Toolset**：把工具按数据源/诊断域分组，当前是 MySQL，后续可以扩展 PostgreSQL、Redis、云监控等。
- **EvidenceStore**：完整工具结果不进入上下文，只保存为 evidence；LLM 只接收压缩摘要。
- **ApprovalPolicy**：安全决策从 ToolExecutor 中抽出来，便于后续接 CLI/API/HITL 审批。
- **DiagnosisResult**：最终结果包含答案、证据 ID、工具调用轨迹和迭代次数，方便报告与审计。

## 项目结构

```
database_holmes_gpt/
  agent/               # Agent 核心
    investigator.py    # 一次数据库调查的编排入口
    models.py          # DiagnosisRequest / DiagnosisResult / ToolCallTrace
    llm_client.py      # LLM 调用封装
    react_loop.py      # ReAct 多轮推理循环
    context.py         # 上下文管理
    memory.py          # 对话记忆
    prompts.py         # 提示词模板
  tools/               # 工具系统
    base.py            # 工具基类
    toolset.py         # 工具集边界（MySQL / PostgreSQL / 监控等）
    registry.py        # 工具注册中心
    executor.py        # 工具执行器
    mysql/             # MySQL 工具集
      slow_query.py    # 慢查询分析
      schema.py        # 表结构查询
      index.py         # 索引分析
      explain.py       # 执行计划
      processlist.py   # 进程列表
      locks.py         # 锁等待分析
      table_stats.py   # 表统计信息
  workflow/            # 诊断工作流
    base.py
    slow_query_diagnosis.py
    lock_diagnosis.py
    table_growth_diagnosis.py
  safety/              # 安全审批
    tool_risk.py       # 工具风险分级
    approval.py        # 人工审批流程
  observe/             # 观测与上下文管理
    compressor.py      # 观测压缩器
    cache.py           # 结果缓存
    evidence_store.py  # 证据存储
  runtime/             # 入口
    cli.py             # 命令行接口
    api.py             # API 接口（可选）
  examples/            # 示例与测试数据
    docker-compose.yml
    seed.sql
    bad_queries.sql
    demo_cases.md
```

## 快速开始

### 环境要求

- Python 3.10+
- Docker & Docker Compose
- MySQL 8.0+

### 启动测试环境

```bash
cd examples
docker-compose up -d
```

### 安装依赖

```bash
pip install -r requirements.txt
```

### 运行 CLI

```bash
python -m runtime.cli
```

## 核心模块

### Agent Runtime

自研轻量 Agent 框架，不依赖 LangChain 等第三方框架。推荐入口是
`DatabaseHolmesInvestigator`：

```python
registry = ToolRegistry()
registry.register_toolset(build_mysql_toolset([...]))

executor = ToolExecutor(registry)
investigator = DatabaseHolmesInvestigator(llm_client, executor)

result = await investigator.investigate("orders 表为什么突然变慢？")
print(result.answer)
print(result.evidence_ids)
```

核心 ReAct Loop：

```python
messages = [system_prompt, user_question]

while not done:
    response = llm.chat(messages, tools=available_tools)
    if response.has_tool_call:
        tool_result = tool_executor.run(response.tool_call)
        evidence_store.save(tool_result)
        compressed_result = compressor.compress(tool_result)
        messages.append(response.tool_call)
        messages.append(compressed_result)
    else:
        return response.final_answer
```

### 工具系统

所有工具默认只读，按风险等级分级（见安全机制）。

#### 慢查询

| 工具 | 说明 |
|------|------|
| `list_slow_queries()` | 读取慢查询列表 |
| `get_slow_query_by_fingerprint()` | 按 fingerprint 获取详情 |
| `rank_slow_queries()` | 按耗时/执行次数排序 |

#### 表结构

| 工具 | 说明 |
|------|------|
| `get_table_schema(table)` | 查看字段、主键 |
| `get_table_indexes(table)` | 查看全部索引 |
| `get_table_status(table)` | 查看行数、表大小、索引大小 |

#### 执行计划

| 工具 | 说明 |
|------|------|
| `explain_query(sql)` | EXPLAIN |
| `explain_analyze_query(sql)` | EXPLAIN ANALYZE |

压缩后只保留关键字段：`type, key, rows, filtered, Extra`。

#### 锁等待

| 工具 | 说明 |
|------|------|
| `show_processlist()` | 当前连接和查询 |
| `get_innodb_trx()` | 活跃事务 |
| `get_data_locks()` | 锁信息 |
| `get_lock_waits()` | 锁等待链 |
| `show_engine_innodb_status()` | InnoDB 引擎状态 |

#### 数据库状态

| 工具 | 说明 |
|------|------|
| `show_global_status()` | 全局状态变量 |
| `show_variables()` | 配置变量 |
| `get_connection_stats()` | 连接数统计 |
| `get_buffer_pool_stats()` | Buffer Pool 命中率 |
| `get_temp_table_stats()` | 临时表落盘统计 |

#### 大表治理

| 工具 | 说明 |
|------|------|
| `list_large_tables()` | 列出大表 |
| `get_table_growth_trend()` | 增长趋势 |
| `get_index_size_stats()` | 索引大小 |
| `analyze_access_pattern()` | 访问模式分析 |

### 诊断 Workflow

支持三种执行模式：

| 模式 | 说明 |
|------|------|
| Agent | LLM 自主决定工具调用顺序 |
| Workflow | 按固定流程排查 |
| Hybrid | Workflow 控制主流程，LLM 负责局部分析 |

#### SlowQueryDiagnosisWorkflow

```
collect_slow_queries → rank_by_latency → select_target_query
→ inspect_schema → inspect_indexes → run_explain
→ classify_problem → generate_report → generate_optimization_plan
```

#### LockDiagnosisWorkflow

```
show_processlist → collect_transactions → collect_locks
→ build_wait_chain → find_blocker → assess_risk → generate_action_plan
```

#### TableGrowthDiagnosisWorkflow

```
get_table_size → get_row_count → get_index_size
→ analyze_query_patterns → check_time_range_usage
→ classify_solution → generate_short_mid_long_term_plan
```

### 上下文管理

三级机制控制上下文爆炸：**Evidence Store → Observation Compressor → Context Budget**。

**Evidence Store**：完整工具结果不直接进入 LLM 上下文，只传入压缩摘要。

```json
{
  "evidence_id": "ev_001",
  "tool": "explain_query",
  "raw_result": { "table": "orders", "type": "ALL", "key": null, "rows": 1200000, "Extra": "Using where; Using filesort" }
}
```

**压缩策略**：

| 数据类型 | 压缩方式 |
|----------|----------|
| 慢查询 | 按 fingerprint 聚合，保留 Top N |
| 表结构 | 只保留相关字段和索引 |
| EXPLAIN | 只保留 type, key, rows, filtered, Extra |
| Processlist | 按状态聚合，只保留长时间查询 |

**Context Budget 参数**：

```
slow_query_top_n = 5
processlist_top_n = 20
max_observation_tokens = 800
max_total_context_tokens = 12000
```

### 安全机制

工具按风险等级分为三级：

| 级别 | 行为 | 示例 |
|------|------|------|
| **Safe** | 自动执行 | `list_slow_queries`, `explain_query`, `get_table_schema` |
| **Approval** | 需人工确认 | `generate_index_sql`, `kill_session` |
| **Dangerous** | 默认拒绝 | `execute_ddl`, `drop_index`, `partition_table` |

执行流程：

```
Agent 生成动作 → Risk Classifier 分级 → Safe: 直接执行 / Approval: 弹出确认 / Dangerous: 拒绝 + mock
```

### 诊断规则

部分判断使用硬编码规则而非 LLM，提高稳定性：

```python
if explain["type"] == "ALL" and explain["rows"] > 100000:
    suspect = "full_table_scan"

if "Using filesort" in explain["Extra"]:
    suspect = "sort_without_index"

if query.offset > 10000:
    suspect = "deep_pagination"

if table.rows > 50_000_000 and query.filters_by("created_at"):
    suspect = "partition_or_archive_candidate"

if table.rows > 100_000_000 and query.filters_by("user_id"):
    suspect = "sharding_candidate"
```

**分工原则**：

```
工具 → 拿证据
规则 → 稳定分类
Workflow → 流程控制
LLM → 综合解释、生成报告和优化方案
HITL → 控制高风险动作
```

### 自动优化能力边界

| Level | 能力 | 说明 |
|-------|------|------|
| 1 | 自动诊断 | 发现根因（全表扫描、索引缺失等） |
| 2 | 自动生成建议 | 输出优化 SQL（CREATE INDEX 等） |
| 3 | 自动生成变更计划 | 含风险评估、回滚方案、验证步骤 |
| 4 | 人工确认后执行 | HITL 审批 |

Demo 展示 Level 1-3，Level 4 模拟。

---

## 诊断场景

### 场景一：慢 SQL 诊断

**输入**：`/api/orders` 最近变慢。

**调查流程**：慢查询 Top N → 定位 orders 相关 SQL → 查看表结构和索引 → EXPLAIN → 根因判断。

**输出**：

```
orders 查询全表扫描，WHERE 含 user_id + status，ORDER BY created_at DESC，
缺少联合索引。建议：
CREATE INDEX idx_user_status_created_at ON orders(user_id, status, created_at);
```

### 场景二：索引健康检查

**输入**：检查 orders 表索引问题。

**分析维度**：冗余索引、低选择性索引、未使用索引、联合索引缺失。

**输出**：

```
idx_user_id 与 idx_user_id_status 存在前缀重复。
高频查询使用 user_id + status + created_at，建议保留联合索引并删除冗余单列索引。
```

### 场景三：锁等待分析

**输入**：订单更新接口卡住。

**调查流程**：processlist → innodb_trx → data_locks → lock_waits → 构建等待链。

**输出**：

```
事务 123 持有 orders 表行锁，事务 456、457 等待中，阻塞事务已运行 280 秒。
建议确认业务状态后人工审批 kill session。[HITL]
```

### 场景四：连接数异常

**输入**：数据库连接数打满。

**分析**：Threads_connected / max_connections、Sleep 占比、来源 IP。

**输出**：

```
连接数接近 max_connections，75% 为 Sleep，主要来自 app-server-03。
疑似连接池泄漏，建议检查应用连接池配置。
```

### 场景五：大表治理评估

**输入**：orders 表是否需要分表？

**分析**：表行数/大小、索引大小、查询模式、访问隔离性。

**输出**（分层建议）：

```
短期：新增联合索引优化查询
中期：历史订单归档，减少热表数据量
长期：亿级 + 访问按 user_id 隔离时，评估按 user_id 分片

当前不建议直接分库分表，瓶颈在索引而非容量。
```

---

## 开发路线

### MVP

- [√] 自研 ReAct Agent Runtime
- [ ] Tool Registry / Executor
- [ ] MySQL 只读工具集
- [ ] 慢查询诊断 Workflow
- [ ] EXPLAIN 分析
- [ ] 索引建议生成
- [ ] Evidence Store + Observation Compressor
- [ ] HITL 模拟审批
- [ ] CLI 演示

### 后续

- [ ] 多数据库支持 (PostgreSQL)
- [ ] Web UI
- [ ] 向量数据库 RAG
- [ ] 多 Agent 协作

### 不做

- 自动执行 DDL / 分库分表 / 删除索引
- 自动 kill 生产连接 / 自动迁移大表

---

## Demo Case

**Demo 1 — 慢 SQL + 缺少联合索引：**

> 输入：订单列表接口为什么变慢？
> 输出：全表扫描 120 万行 + filesort，建议 `CREATE INDEX idx_user_status_created_at ON orders(...)`

**Demo 2 — 锁等待：**

> 输入：订单更新接口卡住
> 输出：事务 123 持锁 280s，事务 456/457 等待，建议人工终止 [HITL]

**Demo 3 — 分表评估：**

> 输入：orders 是否需要分表？
> 输出：短期索引优化 → 中期归档 → 长期评估按 user_id 分片，当前不建议分库分表

---

## 项目边界

**本项目做：** 自动诊断数据库性能问题、自动收集证据和根因分析、生成优化建议和变更计划、上下文裁剪和证据存储、高风险动作人工审批。

**本项目不做：** 自动执行 DDL / 分库分表、自动修改线上数据库、自动删除索引或 kill 生产连接。
