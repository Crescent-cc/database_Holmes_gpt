# RAG Holmes / RAG Observer

面向 RAG 应用的观测、诊断和优化助手。

## 项目概述

RAG Holmes 通过自研轻量 Agent Runtime，围绕 RAG 应用故障排查场景逐步建设 ReAct 多轮推理、工具执行、上下文裁剪、证据存储和 HITL 审批机制。

当前项目已经具备 Agent Runtime 骨架、证据压缩链路和命令行 LLM 调试入口。后续重点不是全盘重写，而是在现有骨架上按 RAG 业务场景扩展 workflow、toolset、证据链和代码定位能力。

**目标能力：** 接收故障描述 → 确认数据源 → 按优先级调用工具取证 → 巩固证据链 → 定位根因 → 给出工程师可执行的修复建议。

典型输入：

```text
我发现 RAG 挂了，你排查并给出解决方案。
```

期望输出：

```text
根因：前端调用 /api/rag/chat，但后端实际暴露 /api/knowledge-base/chat，网关未配置 rewrite，因此请求返回 404。
证据：日志、trace、Spring route、前端 API 调用路径互相印证。
建议：修改 frontend/src/api/ragChat.ts 中的请求路径，或在后端增加兼容 mapping。
```

## 架构

```text
User / 调用方（CLI/API 后续可接入 agent 模式）
    ↓
Investigator
    ↓
PromptBuilder + ContextManager
    ↓
ReActLoop
    ├─ LLMClient
    ├─ ToolExecutor → ApprovalPolicy → ToolRegistry → Toolset(logs/traces/java/repo/rag/db)
    └─ ObservationCompressor + EvidenceStore
    ↓
DiagnosisResult(answer + evidence_ids + tool traces)
```

这次架构继续借鉴 HolmesGPT 的调查式 agent 思路，但聚焦 RAG 应用故障：

- **Investigator**：统一一次故障调查入口。
- **Toolset**：按数据源/诊断域分组，后续扩展 logs、traces、metrics、java、repo、rag、db 等工具集。
- **EvidenceStore**：完整工具结果不进入上下文，只保存为 evidence；LLM 只接收压缩摘要。
- **ObservationCompressor**：控制日志、trace、chunk、prompt、代码片段等大结果进入上下文的大小。
- **ApprovalPolicy**：控制高风险操作，默认以只读调查为主。
- **DiagnosisResult**：最终结果包含答案、证据 ID、工具调用轨迹和迭代次数，方便报告与审计。

## 项目结构

```text
database_holmes_gpt/
  agent/               # Agent 核心
    investigator.py    # 一次故障调查的编排入口
    models.py          # DiagnosisRequest / DiagnosisResult / ToolCallTrace
    llm_client.py      # LLM 调用封装
    react_loop.py      # ReAct 多轮推理循环
    context.py         # 上下文管理
    memory.py          # 对话/调查记忆占位
    prompts.py         # 提示词模板
  tools/               # 工具系统
    base.py            # 工具基类
    toolset.py         # 工具集边界
    registry.py        # 工具注册中心
    executor.py        # 工具执行器
    mysql/__init__.py  # 旧 MySQL toolset 工厂，后续可迁移/保留为 db 工具集
  workflow/            # 故障诊断工作流
    base.py
  safety/              # 安全审批
  observe/             # 观测、证据和上下文管理
  runtime/             # 入口
```

> 说明：当前部分代码类名仍保留 `DatabaseHolmesInvestigator`、`DiagnosisRequest` 等历史命名。为了避免无意义的大范围重命名，先只调整项目定位和提示词，后续在新增 RAG workflow/toolset 时逐步演进命名。

## 快速开始

### 环境要求

- Python 3.10+
- 可选：可访问目标 RAG 应用的日志、trace、代码仓库、数据库或向量库数据源。

### 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 配置模型 Key

```bash
printf 'DEEPSEEK_API_KEY=你的_key\n' > .env
```

### 运行 CLI

```bash
source .venv/bin/activate
python -m runtime.cli
```

当前 CLI 是最小聊天 REPL，用于验证 API Key、模型连通性和基础上下文管理。默认模型为 `deepseek-v4-flash`，默认地址为 `https://api.deepseek.com`；可通过 `--model`、`--base-url`、`--system` 覆盖。

## 核心模块

### Agent Runtime

自研轻量 Agent 框架，不依赖 LangChain 等第三方框架。推荐入口仍是当前已有的 `DatabaseHolmesInvestigator`：

```python
registry = ToolRegistry()
# 后续注册 RAG / logs / traces / repo / java 等 toolset

executor = ToolExecutor(registry)
investigator = DatabaseHolmesInvestigator(llm_client, executor)

result = await investigator.investigate("RAG 接口返回 404，帮我排查原因。")
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

### Toolset 规划

所有工具默认以只读调查为主，按风险等级分级。

| Toolset | 目标 |
|---------|------|
| `logs` | 查询应用日志、网关日志、错误堆栈 |
| `traces` | 查询 Tempo/Jaeger/OpenTelemetry trace，定位失败 span |
| `metrics` | 查询 Prometheus 指标，确认错误率、延迟、资源异常 |
| `java` | 检查 Spring route、Actuator mappings、异常类和配置 |
| `repo` | 搜索代码、读取文件片段、定位具体行号 |
| `rag` | 查询 RAG trace、检索结果、chunk、prompt、模型调用结果 |
| `db` | 查询 MySQL/PostgreSQL/pgvector/Redis 等支撑数据源 |
| `gateway` | 检查 Nginx/Ingress/Gateway 路由和 rewrite |

### Workflow 规划

支持三种执行模式：

| 模式 | 说明 |
|------|------|
| Agent | LLM 自主决定工具调用顺序 |
| Workflow | 按固定流程排查 |
| Hybrid | Workflow 控制主流程，LLM 负责局部分析 |

优先开发的 RAG 故障 workflow：

```text
Http404DiagnosisWorkflow
parse_incident → query_logs → query_trace → inspect_gateway_route
→ inspect_spring_mappings → search_frontend_api_call
→ search_backend_controller → compare_routes → locate_code_line
→ generate_fix_report
```

```text
RagRetrievalMissWorkflow
inspect_rag_trace → inspect_rewrite_query → inspect_vector_search
→ inspect_topk_chunks → inspect_similarity_score → inspect_metadata_filter
→ inspect_knowledge_base_status → generate_retrieval_fix_report
```

```text
RagBadAnswerWorkflow
inspect_question → inspect_retrieved_chunks → inspect_prompt_context
→ evaluate_grounding → check_citation → locate_prompt_or_chunk_issue
→ generate_optimization_plan
```

### 上下文和记忆

后续 memory 裁剪应分成三层：

```text
Conversation Context：当前 LLM messages
Evidence Memory：完整工具证据
Investigation Memory：调查状态、假设、已确认事实、待验证问题
```

进入 LLM 上下文的内容应该是压缩后的调查状态，而不是所有原始日志或 trace：

- 原始问题
- 当前故障场景
- 已确认事实
- 关键证据摘要和 `evidence_id`
- 当前假设和置信度
- 已排除方向
- 下一步计划
- 最终输出要求

完整结果继续保存到 EvidenceStore，压缩摘要进入 LLM 上下文。

## 安全机制

工具按风险等级分为三级：

| 级别 | 行为 | 示例 |
|------|------|------|
| **Safe** | 自动执行 | 查询日志、读取 trace、搜索代码、读取只读配置 |
| **Approval** | 需人工确认 | 生成变更命令、建议重启服务、建议回滚 |
| **Dangerous** | 默认拒绝 | 删除数据、修改生产配置、执行破坏性命令 |

执行流程：

```text
Agent 生成动作 → Risk Classifier 分级 → Safe: 直接执行 / Approval: 弹出确认 / Dangerous: 拒绝
```

## 开发路线

### MVP

- [x] 自研 ReAct Agent Runtime
- [x] Tool Registry / Executor
- [x] Toolset 抽象
- [x] Evidence Store + Observation Compressor
- [x] CLI 最小聊天 REPL
- [ ] RAG 故障场景 prompt 和 workflow
- [ ] `repo` 代码搜索与行号定位工具
- [ ] `java` Spring route / mapping 检查工具
- [ ] `logs` / `traces` 只读工具集
- [ ] `Http404DiagnosisWorkflow`
- [ ] memory / context 裁剪升级

### 后续

- [ ] RAG trace 工具集
- [ ] 向量库 / pgvector / Redis 诊断工具
- [ ] RAG 召回质量评估
- [ ] 证据链可视化
- [ ] Web UI

### 不做

- 自动修改生产代码
- 自动执行破坏性命令
- 自动删除数据或修改线上配置
- 在证据不足时强行给确定根因

## 项目边界

**本项目做：** 面向 RAG 应用故障的自动排查、证据收集、根因分析、代码级定位建议、上下文裁剪和证据存储、高风险动作人工审批。

**本项目不做：** 自动修复线上服务、自动修改生产配置、自动删除数据、自动执行破坏性操作。
