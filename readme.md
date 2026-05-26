下面是把前面两次讨论合并后的完整方案：**Database-HolmesGPT：面向数据库故障诊断与优化建议的 Agent Demo**。

你可以把它做成一个简历项目，目标不是复刻完整 HolmesGPT，而是做一个“数据库领域版 Mini-HolmesGPT”。

---

# Database-HolmesGPT 实现方案

## 1. 项目定位

**Database-HolmesGPT** 是一个面向数据库性能问题的智能排障 Agent。

它通过自研轻量 Agent Runtime，结合数据库只读工具、ReAct 多轮推理、诊断 workflow、上下文裁剪、证据存储和 HITL 审批机制，实现对慢 SQL、索引问题、锁等待、大表治理等场景的自动诊断与优化建议生成。

一句话介绍：

```text
Database-HolmesGPT 是一个面向 MySQL 慢查询与数据库性能问题的 Agentic Troubleshooting Assistant，
能够自动读取数据库运行状态、分析慢 SQL、生成根因报告，并给出可审查的优化建议。
```

项目重点不是“让大模型直接操作数据库”，而是：

```text
自动排查
自动收集证据
自动分析根因
自动生成优化建议
人工审批高风险动作
```

---

## 2. 为什么选择数据库方向

你选择数据库是合理的，因为你已经学习过数据库，并且“系统出现慢 SQL，怎么排查？”本身就是一个非常真实的开发 / 运维问题。

相比 Kubernetes、Prometheus、Loki 这类平台，数据库方向有几个优势：

```text
环境容易搭建
问题容易模拟
排障链路清晰
结果容易验证
适合简历展示
面试官容易理解
```

最终可以把它包装成：

```text
一个数据库领域的 HolmesGPT-like Agent
```

也就是：

```text
HolmesGPT 关注云原生 / SRE 故障排查
Database-HolmesGPT 关注数据库性能故障排查
```

---

## 3. 总体架构

整体架构可以设计成这样：

```text
User / CLI / Web UI
        ↓
Agent Runtime
        ↓
ReAct Agent / Workflow Engine
        ↓
Tool Router
        ↓
MySQL Toolset
        ↓
MySQL / Slow Query Log / Performance Schema / Information Schema
        ↓
Observation Compressor / Cache / Evidence Store
        ↓
Root Cause Report / Optimization Plan / HITL Approval
```

代码目录可以这样设计：

```text
database_holmes_gpt/
  agent/
    llm_client.py
    react_loop.py
    context.py
    memory.py
    prompts.py

  tools/
    base.py
    registry.py
    executor.py
    mysql/
      slow_query.py
      schema.py
      index.py
      explain.py
      processlist.py
      locks.py
      table_stats.py

  workflow/
    base.py
    slow_query_diagnosis.py
    lock_diagnosis.py
    table_growth_diagnosis.py

  safety/
    tool_risk.py
    approval.py

  observe/
    compressor.py
    cache.py
    evidence_store.py

  runtime/
    cli.py
    api.py

  examples/
    docker-compose.yml
    seed.sql
    bad_queries.sql
    demo_cases.md
```

---

## 4. Agent Runtime 设计

你可以先自研一个轻量 Agent 框架，不需要一开始用 LangChain、AutoGen 之类的框架。

核心能力包括：

```text
LLM 调用
上下文管理
工具定义
工具注册
工具执行
ReAct 多轮循环
Workflow 编排
HITL 审批
Runtime 入口
```

最小 ReAct Loop 可以是：

```python
messages = [
    system_prompt,
    user_question,
]

while not done:
    response = llm.chat(messages, tools=available_tools)

    if response.has_tool_call:
        tool_result = tool_executor.run(response.tool_call)
        compressed_result = compressor.compress(tool_result)

        evidence_store.save(tool_result)
        messages.append(response.tool_call)
        messages.append(compressed_result)
    else:
        return response.final_answer
```

这和 HolmesGPT 的最小内核非常类似：

```text
用户问题 / 告警
→ system prompt
→ LLM 判断是否需要工具
→ 调用工具
→ 工具返回 observation
→ 记录到上下文
→ 继续推理
→ 输出诊断报告
```

---

## 5. 数据库 Toolset 设计

项目的核心不是 LLM 本身，而是数据库工具集。

第一版建议只支持 MySQL，工具全部默认只读。

### 5.1 慢查询工具

```text
list_slow_queries()
get_slow_query_by_fingerprint()
rank_slow_queries()
```

作用：

```text
读取慢 SQL
按 fingerprint 聚合
按平均耗时、总耗时、执行次数排序
提取 Top N 问题 SQL
```

不要把所有慢查询日志直接塞给 LLM，而是先结构化压缩。

---

### 5.2 表结构工具

```text
get_table_schema(table_name)
get_table_indexes(table_name)
get_table_status(table_name)
```

作用：

```text
查看字段
查看主键
查看普通索引
查看联合索引
查看表行数
查看表大小
查看索引大小
```

---

### 5.3 执行计划工具

```text
explain_query(sql)
explain_analyze_query(sql)
```

作用：

```text
判断是否全表扫描
判断是否使用索引
查看扫描行数
查看 Extra 信息
判断是否 filesort / temporary table
```

进入上下文时，不需要塞完整 EXPLAIN，只保留关键字段：

```text
table=orders
type=ALL
key=NULL
rows=1200000
Extra=Using where; Using filesort
```

---

### 5.4 锁等待工具

```text
show_processlist()
get_innodb_trx()
get_data_locks()
get_lock_waits()
show_engine_innodb_status()
```

作用：

```text
分析锁等待
分析长事务
发现 blocking session
判断是否有死锁
判断是否建议人工 kill session
```

---

### 5.5 数据库状态工具

```text
show_global_status()
show_variables()
get_connection_stats()
get_buffer_pool_stats()
get_temp_table_stats()
```

作用：

```text
判断连接数异常
判断是否 Too many connections
判断临时表落盘
判断 buffer pool 命中率
判断数据库资源压力
```

---

### 5.6 大表治理工具

```text
list_large_tables()
get_table_growth_trend()
get_index_size_stats()
analyze_access_pattern()
```

作用：

```text
发现大表
分析数据增长
判断是否需要归档
判断是否需要分区
判断是否需要长期评估分库分表
```

---

## 6. 支持的典型诊断场景

不要只做一个“慢 SQL 诊断”。可以设计成 3 到 5 个典型场景。

---

## 场景一：慢 SQL 诊断

用户输入：

```text
接口 /api/orders 最近变慢，帮我排查。
```

Agent 调查过程：

```text
1. 查询慢 SQL Top N
2. 找到 orders 相关慢查询
3. 查看 SQL fingerprint
4. 查看 orders 表结构
5. 查看索引
6. 执行 EXPLAIN
7. 判断根因
8. 输出优化建议
```

可能结论：

```text
orders 表查询存在全表扫描，WHERE 条件包含 user_id 和 status，
ORDER BY created_at DESC，但当前缺少匹配的联合索引。
建议新增联合索引：
CREATE INDEX idx_user_status_created_at ON orders(user_id, status, created_at);
```

---

## 场景二：索引健康检查

用户输入：

```text
帮我检查一下 orders 表有没有索引问题。
```

Agent 可以分析：

```text
是否缺少联合索引
是否存在重复索引
是否存在低选择性索引
是否存在未使用索引
是否存在索引字段顺序不合理
```

输出：

```text
idx_user_id 和 idx_user_id_status 存在前缀重复。
orders 查询高频使用 user_id + status + created_at，
建议保留联合索引，评估删除冗余单列索引。
```

---

## 场景三：锁等待 / 死锁分析

用户输入：

```text
为什么订单更新接口卡住了？
```

Agent 调查过程：

```text
1. 查询 processlist
2. 查询 innodb_trx
3. 查询 data_locks
4. 构建锁等待链路
5. 找出 blocking transaction
6. 生成处理建议
```

输出：

```text
事务 123 当前持有 orders 表某行锁，事务 456、457 正在等待。
阻塞事务已运行 280 秒，SQL 为 UPDATE orders SET ...
建议先联系业务确认该事务是否可终止。
如确认无风险，可人工审批后 kill session。
```

这里可以体现 HITL：

```text
kill session 属于高风险动作，需要人工确认。
```

---

## 场景四：连接数 / 资源异常分析

用户输入：

```text
数据库连接数打满了，帮我看看原因。
```

Agent 调查：

```text
show processlist
show status like Threads_connected
show variables like max_connections
统计 sleep connection
统计来源 IP
统计当前活跃 SQL
```

输出：

```text
当前连接数接近 max_connections，其中 75% 为 Sleep 状态，
主要来自 app-server-03，疑似连接池泄漏或连接未及时释放。
建议检查应用连接池配置和连接释放逻辑。
```

---

## 场景五：大表治理 / 是否需要分表

用户输入：

```text
orders 表越来越慢，是否需要分表？
```

Agent 调查：

```text
1. 查询 orders 表行数和大小
2. 查询索引大小
3. 查询慢 SQL 模式
4. 分析查询条件是否集中在 user_id / created_at
5. 判断当前瓶颈是索引问题、历史数据问题，还是容量问题
6. 输出治理建议
```

输出应该是分层的：

```text
当前不建议直接分库分表。

原因：
1. 慢查询主要由缺少联合索引导致。
2. 当前查询集中在 user_id + status + created_at。
3. 新增联合索引可以先解决主要慢查询。
4. 分库分表会引入路由、迁移、跨分片查询、事务一致性等复杂度。

短期建议：
新增联合索引，优化查询。

中期建议：
对历史订单做归档，减少热表数据量。

长期建议：
如果 orders 表持续增长到亿级，并且访问天然按 user_id 隔离，
可以评估按 user_id 分片。
```

这个场景可以体现你对分库分表的工程边界理解。

---

## 7. 自动优化的能力边界

你可以说项目支持“自动优化建议”，但不要说“自动执行分库分表”。

推荐把能力分成 4 个等级。

### Level 1：自动诊断

```text
发现问题是什么。
```

例如：

```text
orders 查询慢的主要原因是缺少联合索引，导致扫描 120 万行并产生 filesort。
```

---

### Level 2：自动生成优化建议

```text
建议怎么改。
```

例如：

```sql
CREATE INDEX idx_user_status_created_at
ON orders(user_id, status, created_at);
```

---

### Level 3：自动生成变更计划

```text
生成可审查的 migration / runbook。
```

例如：

```text
变更目标：新增联合索引
影响表：orders
风险：大表建索引可能造成锁等待和 IO 压力
建议执行窗口：低峰期
回滚方案：DROP INDEX idx_user_status_created_at ON orders
验证方式：对比 EXPLAIN 和查询耗时
```

---

### Level 4：人工确认后执行

```text
危险动作需要 HITL。
```

例如：

```text
是否允许执行该 DDL？
[y/N]
```

简历 demo 建议最多做到 Level 3，Level 4 可以模拟。

---

## 8. Tool Risk 和 HITL 设计

你可以给工具分风险等级。

### Safe Tools

只读工具，可以自动执行：

```text
list_slow_queries
get_table_schema
get_table_indexes
explain_query
show_processlist
get_table_status
```

### Approval Tools

需要人工确认：

```text
generate_index_sql
generate_migration_plan
kill_session
run_analyze_table
```

### Dangerous Tools

默认禁止或只做 mock：

```text
execute_ddl
drop_index
partition_table
shard_table
delete_data
```

执行前流程：

```text
Agent 生成动作建议
→ Risk Classifier 判断风险等级
→ 如果是 safe，直接执行
→ 如果是 approval，需要用户确认
→ 如果是 dangerous，默认拒绝或 mock
```

这可以成为项目亮点：

```text
实现 tool-level risk classification 与 human approval gate，避免 Agent 直接执行高风险数据库操作。
```

---

## 9. 上下文爆炸问题解决方案

数据库 Agent 很容易遇到上下文爆炸。

例如：

```text
慢查询日志很多
表结构很长
索引很多
EXPLAIN ANALYZE 很长
processlist 很乱
performance_schema 数据巨大
```

解决方案是：

```text
Raw Evidence Store + Observation Compressor + Context Budget
```

---

### 9.1 Evidence Store

完整工具结果不直接进入 LLM 上下文，而是保存到 evidence store。

```text
Tool Result
   ↓
Raw Evidence Store 保存完整结果
   ↓
Observation Compressor 提取摘要
   ↓
摘要进入 LLM context
```

例如：

```json
{
  "evidence_id": "ev_001",
  "tool": "explain_query",
  "raw_result": {
    "table": "orders",
    "type": "ALL",
    "key": null,
    "rows": 1200000,
    "Extra": "Using where; Using filesort"
  }
}
```

进入上下文的只有：

```text
Evidence ev_001: EXPLAIN 显示 orders 表 type=ALL，未使用索引，预计扫描 1200000 行，Extra 包含 Using filesort。
```

---

### 9.2 Observation Compressor

不同工具用不同压缩策略。

慢查询压缩：

```text
按 fingerprint 聚合
只保留 Top N
保留 avg_time、max_time、count、sample_sql
```

表结构压缩：

```text
只保留相关字段
只保留主键和相关索引
隐藏无关字段
```

EXPLAIN 压缩：

```text
只保留 type、key、rows、filtered、Extra
```

Processlist 压缩：

```text
按状态聚合
只保留长时间运行 SQL
只保留阻塞链路相关连接
```

---

### 9.3 Context Budget

给每轮上下文设置预算：

```text
最大 observation token 数
每个工具最大返回行数
每个 workflow 最大 evidence 数
超过预算自动摘要
```

例如：

```text
slow_query_top_n = 5
processlist_top_n = 20
max_observation_tokens = 800
max_total_context_tokens = 12000
```

这样项目就不会变成“把数据库所有信息丢给大模型”。

---

## 10. Workflow 设计

除了 ReAct 自由探索，你还可以实现确定性的诊断 workflow。

### 慢 SQL Workflow

```text
SlowQueryDiagnosisWorkflow

1. collect_slow_queries
2. rank_by_latency
3. select_target_query
4. inspect_schema
5. inspect_indexes
6. run_explain
7. classify_problem
8. generate_report
9. generate_optimization_plan
```

---

### 锁等待 Workflow

```text
LockDiagnosisWorkflow

1. show_processlist
2. collect_transactions
3. collect_locks
4. build_wait_chain
5. find_blocker
6. assess_risk
7. generate_action_plan
```

---

### 大表治理 Workflow

```text
TableGrowthDiagnosisWorkflow

1. get_table_size
2. get_row_count
3. get_index_size
4. analyze_query_patterns
5. check_time_range_usage
6. classify_solution
7. generate_short_mid_long_term_plan
```

你可以支持两种模式：

```text
agent 模式：LLM 自己决定下一步工具
workflow 模式：按固定流程排查
hybrid 模式：workflow 控制大步骤，LLM 决定局部分析
```

这比单纯 ReAct 更像真实工程。

---

## 11. 诊断规则与 LLM 的分工

不要让所有判断都交给 LLM。可以把一部分数据库规则写死。

例如：

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

系统分工：

```text
工具负责拿证据
规则负责稳定分类
Workflow 负责流程控制
LLM 负责综合解释、生成报告和优化方案
HITL 负责控制高风险动作
```

这样比纯 LLM Agent 更稳定。

---

## 12. 最小可行版本 MVP

建议第一版只做这些：

```text
1. 自研 ReAct Agent Runtime
2. Tool Registry / Tool Executor
3. MySQL 只读工具
4. 慢查询诊断 Workflow
5. EXPLAIN 分析
6. 索引建议生成
7. Evidence Store
8. Observation Compressor
9. HITL 模拟审批
10. CLI 演示
```

不要一开始做：

```text
多数据库支持
Kubernetes
Prometheus
完整 Web UI
自动执行 DDL
真实分库分表
复杂长期记忆
多 Agent 协作
向量数据库 RAG
```

---

## 13. 推荐开发路线

### 第一步：跑通 Agent Runtime

先不接数据库，写假工具。

```text
LLM 调用
messages 管理
tool schema
tool executor
ReAct loop
final answer
```

---

### 第二步：接入 MySQL 只读工具

实现：

```text
get_table_schema
get_table_indexes
explain_query
list_slow_queries
show_processlist
```

---

### 第三步：构造 demo 数据

用 Docker Compose 启动 MySQL，造几个典型问题：

```text
缺少联合索引
深分页
锁等待
大表扫描
连接数异常
```

---

### 第四步：实现慢 SQL 诊断闭环

让 Agent 能完成：

```text
发现慢 SQL
查看表结构
查看索引
执行 EXPLAIN
判断根因
生成报告
```

---

### 第五步：加入上下文裁剪

实现：

```text
Evidence Store
Observation Compressor
Top N slow query
EXPLAIN 关键字段提取
```

---

### 第六步：加入 Workflow

实现：

```text
SlowQueryDiagnosisWorkflow
LockDiagnosisWorkflow
TableGrowthDiagnosisWorkflow
```

---

### 第七步：加入 HITL

实现：

```text
高风险操作拦截
DDL 只生成不执行
kill session 需要确认
分库分表只给方案不执行
```

---

### 第八步：完善 README 和演示

README 要重点展示：

```text
架构图
Agent Loop
工具系统
诊断流程
上下文裁剪机制
HITL 机制
三个 demo case
```

---

## 14. 推荐 Demo Case

最终项目最好准备 3 个演示。

### Demo 1：慢 SQL + 缺少联合索引

用户输入：

```text
为什么订单列表接口最近很慢？
```

Agent 输出：

```text
发现 orders 查询平均耗时 2.8s。
EXPLAIN 显示扫描 120 万行，未使用索引，并出现 Using filesort。
当前查询条件为 user_id + status，排序字段为 created_at。
建议新增联合索引 idx_user_status_created_at。
```

---

### Demo 2：锁等待

用户输入：

```text
订单更新接口一直卡住，帮我排查。
```

Agent 输出：

```text
发现事务 123 持有锁，事务 456 和 457 正在等待。
阻塞事务运行 280 秒。
建议先确认业务状态，如确认异常，可人工审批后终止阻塞连接。
```

---

### Demo 3：是否需要分表

用户输入：

```text
orders 表越来越慢，是否需要分表？
```

Agent 输出：

```text
当前不建议直接分表。
主要瓶颈是慢查询缺少联合索引。
短期建议新增索引。
中期建议历史数据归档。
长期在数据量继续增长且访问模式稳定按 user_id 隔离时，再评估按 user_id 分片。
```

这个 case 可以很好体现你对“自动优化边界”的理解。

---

## 15. 简历描述

可以这样写：

```text
Database-HolmesGPT：面向 MySQL 性能故障诊断的 Agentic Troubleshooting Assistant

- 自研轻量 Agent Runtime，支持 LLM 调用、ReAct 多轮推理、工具注册、上下文管理、Workflow 编排与 HITL 审批。
- 实现 MySQL Toolset，支持慢查询读取、表结构分析、索引分析、EXPLAIN 诊断、锁等待分析、连接状态检查等只读排障工具。
- 构建慢 SQL 诊断 Workflow，自动识别全表扫描、索引缺失、filesort、深分页、锁等待等典型问题，并生成根因报告。
- 设计 Evidence Store 与 Observation Compressor，对大规模工具输出进行裁剪、摘要和缓存，避免上下文污染。
- 引入 tool risk level 与 approval gate，默认只读，高风险 SQL 操作仅生成可审查方案或在人工确认后执行。
- 支持根据诊断结果生成索引优化建议、DDL 草案、回滚方案和验证步骤。
```

---

## 16. 项目边界总结

你这个项目应该做：

```text
自动诊断数据库性能问题
自动收集数据库证据
自动分析慢 SQL 根因
自动生成索引和 SQL 优化建议
自动生成可审查的变更计划
支持人工审批高风险动作
支持上下文裁剪和证据存储
```

不建议做：

```text
自动执行分库分表
自动修改线上数据库
自动删除索引
自动 kill 生产连接
自动迁移大表数据
```

分库分表可以作为高级建议出现：

```text
短期：索引优化
中期：归档 / 分区
长期：评估分库分表
```

而不是自动执行动作。

---

# 最终结论

你的项目路线是流畅的，而且非常适合简历。

最推荐的版本是：

```text
Database-HolmesGPT = 自研 Agent Runtime + MySQL Toolset + 慢 SQL / 锁等待 / 大表治理诊断 + 上下文裁剪 + HITL 审批
```

核心卖点不是“我让大模型连上数据库”，而是：

```text
我把 HolmesGPT 的 Agentic Troubleshooting 思路迁移到了数据库性能诊断场景，
实现了一个具备工具调用、证据收集、上下文压缩、Workflow 编排和人工审批能力的数据库排障 Agent。
```

这个项目既能体现 Agent 工程能力，也能体现数据库知识。
