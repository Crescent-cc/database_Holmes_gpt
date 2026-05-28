# Agent 架构设计文档

RAG Holmes / RAG Observer 是一个面向 RAG 应用故障调查的轻量 Agent Runtime。当前项目借鉴 HolmesGPT 的调查式 agent 架构，但只按需引入适合本项目的部分：统一调查入口、工具集边界、证据存储、观测压缩、安全审批策略和 ReAct 主循环。

本项目现在仍处于 MVP 骨架阶段：Agent Runtime 已经成型，后续重点是围绕 RAG 应用常见故障扩展 workflow、toolset、memory 裁剪和代码定位能力。

## 一句话概括

**用户提交故障描述 → Investigator 组装调查上下文 → ReActLoop 驱动 LLM 调工具 → 工具结果完整入 EvidenceStore、摘要进上下文 → LLM 生成带证据轨迹的诊断和修复建议。**

## 当前架构

```text
User / 调用方（CLI/API 后续可接入 agent 模式）
    |
    v
Investigator
    |
    +-- PromptBuilder
    +-- ContextManager
    |
    v
ReActLoop
    |
    +-- LLMClient
    |
    +-- ToolExecutor
    |     |
    |     +-- ApprovalPolicy
    |     +-- ToolRegistry
    |           |
    |           +-- Toolset(logs/traces/java/repo/rag/db)
    |                 |
    |                 +-- BaseTool implementations
    |
    +-- ObservationCompressor
    +-- EvidenceStore
    |
    v
DiagnosisResult(answer + evidence_ids + tool traces)
```

这套分层的目标是让每个模块只承担一个稳定职责：

- `DatabaseHolmesInvestigator`：当前已有的一次调查编排入口，后续可逐步改名为更通用的 Investigator。
- `ReActLoop`：只负责 Think → Act → Observe 循环。
- `Toolset`：按数据源或诊断域组织工具。
- `EvidenceStore`：保存完整原始证据。
- `ObservationCompressor`：把工具结果压缩成 LLM 可读摘要。
- `ApprovalPolicy`：统一处理工具执行前的安全决策。
- `DiagnosisResult`：把最终答案、证据 ID、工具轨迹和运行元信息结构化返回。

## 目录现状

```text
agent/
  __init__.py          # agent 包导出，Investigator 使用懒加载
  investigator.py      # DatabaseHolmesInvestigator，调查编排入口
  models.py            # DiagnosisRequest / DiagnosisResult / ToolCallTrace
  llm_client.py        # OpenAI-compatible LLM 调用封装
  react_loop.py        # ReAct 主循环，支持结构化结果和证据写入
  context.py           # 对话上下文管理和预算裁剪
  prompts.py           # SYSTEM_PROMPT 和 PromptBuilder
  memory.py            # 占位，后续做调查记忆和跨会话记忆

tools/
  base.py              # BaseTool / RiskLevel / ToolCall / ToolResult
  toolset.py           # Toolset 抽象
  registry.py          # ToolRegistry，支持 register_toolset
  executor.py          # ToolExecutor，执行前走 ApprovalPolicy
  mysql/__init__.py    # 旧 MySQL toolset 工厂，后续可沉淀到 db toolset

observe/
  compressor.py        # ObservationCompressor，内置多种压缩策略
  evidence_store.py    # EvidenceRecord / EvidenceStore / InMemoryEvidenceStore
  cache.py             # 占位，后续做工具结果 TTL 缓存

safety/
  approval.py          # ApprovalPolicy / ApprovalResult / ApprovalDecision
  tool_risk.py         # 占位，后续做动态风险评分

workflow/
  base.py              # 占位，后续做固定诊断流程

runtime/
  cli.py               # 最小聊天 REPL，后续扩展为 agent 诊断入口
```

## 核心数据模型

### DiagnosisRequest

文件：`agent/models.py`

`DiagnosisRequest` 表示一次故障调查请求。

```python
DiagnosisRequest(
    question="RAG 接口返回 404，帮我排查原因。",
    source="cli",
    metadata={"service": "interview-guide", "env": "staging", "time_window": "last_30m"},
    workflow="http_404",
)
```

字段含义：

- `question`：用户的故障描述。
- `source`：请求来源，默认 `cli`，后续 API 或 Web UI 可以传不同来源。
- `metadata`：服务名、环境、时间窗口、trace_id、request_id 等额外上下文。
- `workflow`：可选场景名，后续用于 Hybrid Workflow。

### DiagnosisResult

`DiagnosisResult` 是一次调查的结构化输出。

```python
DiagnosisResult(
    answer="最终诊断结论和修复建议",
    request=request,
    tool_calls=[...],
    evidence_ids=["ev_xxx"],
    iterations=4,
    metadata={"forced_conclusion": False, "failed": False},
)
```

它解决两个问题：

- 调用方不用从纯文本里解析证据和工具轨迹。
- 后续生成报告、审计、测试时可以直接读取结构化字段。

### ToolCallTrace

每次工具执行都会留下一个 `ToolCallTrace`：

- `tool_call_id`：LLM function calling 返回的调用 ID。
- `tool_name`：工具名称。
- `success`：工具是否执行成功。
- `evidence_id`：完整结果在 EvidenceStore 中的 ID。
- `error`：失败时的错误信息。

## 调查入口：DatabaseHolmesInvestigator

文件：`agent/investigator.py`

`DatabaseHolmesInvestigator` 是当前推荐入口。类名暂时保留历史命名，避免无意义的大范围改动。调用方不需要直接创建 `ContextManager`、`ReActLoop`、`ObservationCompressor` 等一串对象。

```python
from agent.investigator import DatabaseHolmesInvestigator
from agent.models import DiagnosisRequest
from agent.llm_client import LLMClient
from tools import ToolExecutor, ToolRegistry

registry = ToolRegistry()
# 后续注册 logs / traces / java / repo / rag / db 等 toolset

executor = ToolExecutor(registry)
llm = LLMClient(
    model="deepseek-v4-flash",
    base_url="https://api.deepseek.com",
)

investigator = DatabaseHolmesInvestigator(llm, executor)

result = await investigator.investigate(DiagnosisRequest(
    question="RAG 接口返回 404，帮我排查原因。",
    metadata={"service": "interview-guide", "env": "staging"},
    workflow="http_404",
))

print(result.answer)
print(result.evidence_ids)
```

它内部做了这些事：

1. 用 `PromptBuilder` 根据请求和 toolsets 组装 system prompt。
2. 创建新的 `ContextManager`，保证每次调查上下文隔离。
3. 创建 `ReActLoop`，注入 LLM、工具执行器、压缩器和证据存储。
4. 执行 `loop.run_result(request)`。
5. 返回 `DiagnosisResult`。

## PromptBuilder

文件：`agent/prompts.py`

`PromptBuilder` 基于三类信息构造 system prompt：

- 基础 `SYSTEM_PROMPT`：RAG 应用故障调查专家角色、安全红线、输出规范。
- `DiagnosisRequest.workflow`：可选诊断流程偏好。
- `ToolRegistry.toolset_descriptions`：当前启用的工具集和工具名称。
- `DiagnosisRequest.metadata`：服务名、环境、时间窗口、trace_id 等请求元信息。

这样 prompt 拼装从 ReActLoop 中分离，后续可替换为 Jinja2 或多场景 prompt。

## ContextManager

文件：`agent/context.py`

`ContextManager` 管理 OpenAI chat messages，并做上下文预算控制。

当前策略：

- 按轮次裁剪，不拆散 assistant tool_calls 和后续 tool result。
- 先按 `max_rounds` 控制轮数，默认保留 10 轮。
- 再按 `max_tokens` 粗估预算裁剪，默认 12000。
- token 估算使用 `字符数 / 2.5`，不引入 tokenizer 依赖。

后续升级方向：

```text
Conversation Context：当前 LLM messages
Evidence Memory：完整工具证据
Investigation Memory：调查状态、假设、已确认事实、待验证问题
```

裁剪不应只按时间顺序删除消息，而应优先保留：

- 用户原始问题。
- 服务、环境、时间窗口、request_id、trace_id。
- 已确认事实和关键 evidence_id。
- 当前根因假设和置信度。
- 已排除方向。
- 代码定位结果。
- 下一步调查计划。

## 工具系统

### BaseTool

文件：`tools/base.py`

所有工具继承 `BaseTool`：

```python
class SearchRepo(BaseTool):
    name = "search_repo"
    description = "在代码仓库中搜索关键字或路径"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "要搜索的关键字或正则"}
        },
        "required": ["pattern"],
    }
    risk_level = RiskLevel.SAFE

    async def run(self, pattern: str) -> dict:
        return {...}
```

工具需要声明：

- `name`：LLM 调用的函数名。
- `description`：给 LLM 判断何时使用。
- `parameters`：JSON Schema，直接转换为 OpenAI function schema。
- `risk_level`：`SAFE`、`APPROVAL`、`DANGEROUS`。
- `run()`：实际执行逻辑。

### Toolset

文件：`tools/toolset.py`

`Toolset` 是本次按 HolmesGPT 架构引入的重要边界。它按数据源或诊断域组织工具。

```python
Toolset(
    name="repo",
    description="代码搜索和行号定位工具集",
    tools=[SearchRepo(), ReadFileLines()],
    enabled=True,
)
```

RAG Holmes 优先规划这些 toolset：

- `logs`：查询应用日志、网关日志、错误堆栈。
- `traces`：查询 Tempo/Jaeger/OpenTelemetry trace。
- `metrics`：查询 Prometheus 指标。
- `java`：检查 Spring route、Actuator mappings、配置和异常类。
- `repo`：搜索代码、读取文件片段、定位具体行号。
- `rag`：查询 RAG trace、检索结果、chunk、prompt、模型调用结果。
- `db`：查询 MySQL/PostgreSQL/pgvector/Redis 等支撑数据源。
- `gateway`：检查 Nginx/Ingress/Gateway 路由和 rewrite。

### ToolExecutor

文件：`tools/executor.py`

`ToolExecutor` 接收 LLM 返回的 `ToolCall`，执行流程：

```text
1. 从 ToolRegistry 查找工具
2. 找不到则返回失败 ToolResult
3. 调用 ApprovalPolicy.evaluate(tool_call, risk_level)
4. 未获准则返回失败 ToolResult
5. 执行 tool.run(**arguments)
6. 成功返回 ToolResult(data=...)
7. 异常返回 ToolResult(error=...)
```

它不负责压缩、不写上下文、不直接写证据。这些都由 ReActLoop 编排。

## 证据存储和观测压缩

设计原则：

- 完整结果保存到 EvidenceStore。
- 压缩摘要进入 LLM 上下文。
- 最终 `DiagnosisResult.evidence_ids` 只引用证据 ID。

RAG 故障场景中，日志、trace、chunk、prompt、代码片段都可能很大，所以压缩策略后续需要从 MySQL 专用规则扩展为更通用的场景规则：

| 数据类型 | 压缩方式 |
|----------|----------|
| 日志 | 聚合错误类型、保留关键异常栈和 request_id |
| Trace | 保留失败 span、耗时最长 span、上下游服务 |
| RAG chunk | 保留 chunk_id、score、metadata、关键文本片段 |
| Prompt | 保留 system/user/context 分区和长度统计 |
| 代码 | 保留匹配行、附近上下文、文件路径和行号 |

## Workflow 规划

优先落地一个最小闭环：`Http404DiagnosisWorkflow`。

```text
parse_incident
→ query_logs
→ query_trace
→ inspect_gateway_route
→ inspect_spring_mappings
→ search_frontend_api_call
→ search_backend_controller
→ compare_routes
→ locate_code_line
→ generate_fix_report
```

然后扩展：

- `RagRetrievalMissWorkflow`：检索不到或召回为空。
- `RagBadAnswerWorkflow`：回答幻觉、引用错误、答案不相关。
- `RagLatencyWorkflow`：RAG 接口超时或延迟异常。
- `KnowledgeBaseIngestionWorkflow`：文档上传、解析、分块、向量化失败。
- `EmbeddingFailureWorkflow`：embedding provider 或向量写入异常。

## 当前实现状态

已实现：

- ReAct 主循环。
- OpenAI-compatible LLM 调用封装。
- 上下文管理和预算裁剪。
- 工具基类、工具注册、工具执行。
- Toolset 抽象。
- 可插拔审批策略。
- 内存版 EvidenceStore。
- 观测压缩器。
- 结构化 DiagnosisRequest / DiagnosisResult。
- Investigator 统一入口。
- Runtime CLI 最小聊天 REPL，可验证模型连通性和上下文裁剪。

占位或待实现：

- RAG 故障 workflow。
- logs / traces / java / repo / rag toolset。
- 代码行定位工具。
- 更面向调查状态的 memory 裁剪。
- 工具结果缓存。
- 跨会话记忆。
- 动态风险评分和真正 HITL。
- 持久化 EvidenceStore。

## 后续扩展建议

优先顺序建议：

1. 增加 `repo` 工具集：`search_repo`、`read_file_lines`、`find_frontend_api_call`。
2. 增加 `java` 工具集：`find_spring_controller_by_path`、`find_request_mapping`。
3. 增加 `Http404DiagnosisWorkflow`，打穿“日志/trace → route → 代码行 → 修复建议”的闭环。
4. 把 `runtime/cli.py` 从聊天 REPL 扩展为 Investigator 调试入口。
5. 升级 memory 裁剪，引入 InvestigationState / HypothesisMemory。
6. 把 `InMemoryEvidenceStore` 替换或扩展为 SQLite。

## 设计边界

本项目负责：

- RAG 应用故障调查 Agent Runtime。
- 工具调用编排。
- 上下文控制。
- 证据留存。
- 安全审批边界。
- 代码级定位建议。
- 最终诊断报告生成。

本项目不在当前阶段负责：

- 自动修改生产代码。
- 自动执行破坏性命令。
- 自动删除数据或修改线上配置。
- 多 Agent 协同。
- Web UI。

## 和 HolmesGPT 的关系

本项目不是复制 HolmesGPT，而是借鉴其适合故障调查系统的架构思想：

- 用 Investigator 表达一次调查。
- 用 Toolset 表达数据源能力边界。
- 用 EvidenceStore 保留调查证据。
- 用压缩摘要控制 LLM 上下文。
- 用安全策略限制高风险动作。

当前项目聚焦 Java/RAG 应用故障诊断，因此不会一次性引入 HolmesGPT 的全部数据源、插件系统或复杂运行时。这样能保持代码小、职责清楚，也方便后续按真实 RAG 场景扩展。
