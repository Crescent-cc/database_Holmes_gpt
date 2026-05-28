# Agent 架构设计文档

Database HolmesGPT 是一个面向数据库性能诊断的轻量 Agent Runtime。当前项目借鉴 HolmesGPT 的调查式 agent 架构，但只按需引入适合本项目的部分：统一调查入口、工具集边界、证据存储、观测压缩、安全审批策略和 ReAct 主循环。

本项目现在仍处于 MVP 骨架阶段：Agent Runtime 已经成型，MySQL 具体诊断工具还在扩展入口处预留。

## 一句话概括

**用户提交诊断问题 → Investigator 组装调查上下文 → ReActLoop 驱动 LLM 调工具 → 工具结果完整入 EvidenceStore、摘要进上下文 → LLM 生成带证据轨迹的诊断结果。**

## 当前架构

```text
User / 调用方（CLI/API 后续可接入 agent 模式）
    |
    v
DatabaseHolmesInvestigator
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
    |           +-- Toolset(mysql)
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

- `DatabaseHolmesInvestigator`：一次诊断的编排入口。
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
  investigator.py      # DatabaseHolmesInvestigator，诊断编排入口
  models.py            # DiagnosisRequest / DiagnosisResult / ToolCallTrace
  llm_client.py        # OpenAI-compatible LLM 调用封装
  react_loop.py        # ReAct 主循环，支持结构化结果和证据写入
  context.py           # 对话上下文管理和预算裁剪
  prompts.py           # SYSTEM_PROMPT 和 PromptBuilder
  memory.py            # 占位，后续做跨会话记忆

tools/
  base.py              # BaseTool / RiskLevel / ToolCall / ToolResult
  toolset.py           # Toolset 抽象
  registry.py          # ToolRegistry，支持 register_toolset
  executor.py          # ToolExecutor，执行前走 ApprovalPolicy
  mysql/__init__.py    # build_mysql_toolset 工厂，具体工具待加入

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

`DiagnosisRequest` 表示一次诊断请求。

```python
DiagnosisRequest(
    question="orders 表为什么突然变慢？",
    source="cli",
    metadata={"database": "shop", "env": "staging"},
    workflow="slow_query",
)
```

字段含义：

- `question`：用户的诊断问题。
- `source`：请求来源，默认 `cli`，后续 API 或 Web UI 可以传不同来源。
- `metadata`：数据库名、环境、业务线、时间窗口等额外上下文。
- `workflow`：可选场景名，后续用于 Hybrid Workflow。

### DiagnosisResult

`DiagnosisResult` 是一次调查的结构化输出。

```python
DiagnosisResult(
    answer="最终诊断结论",
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

`DatabaseHolmesInvestigator` 是当前推荐入口。调用方不需要直接创建 `ContextManager`、`ReActLoop`、`ObservationCompressor` 等一串对象。

```python
from agent.investigator import DatabaseHolmesInvestigator
from agent.models import DiagnosisRequest
from agent.llm_client import LLMClient
from tools import ToolExecutor, ToolRegistry
from tools.mysql import build_mysql_toolset

registry = ToolRegistry()
registry.register_toolset(build_mysql_toolset([...]))

executor = ToolExecutor(registry)
llm = LLMClient(
    model="deepseek-v4-flash",
    base_url="https://api.deepseek.com",
)

investigator = DatabaseHolmesInvestigator(llm, executor)

result = await investigator.investigate(DiagnosisRequest(
    question="orders 表为什么突然变慢？",
    metadata={"database": "shop"},
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

## ReActLoop 主循环

文件：`agent/react_loop.py`

`ReActLoop` 是 Agent 的核心执行循环。它现在有两个入口：

- `run(user_input: str) -> str`：兼容旧调用，只返回最终文本答案。
- `run_result(request: DiagnosisRequest | str) -> DiagnosisResult`：新入口，返回结构化结果。

主流程：

```text
1. 把用户问题加入 ContextManager
2. 循环执行，最多 max_iterations 轮
3. 获取裁剪后的 messages 和工具 schema
4. 调用 LLMClient.chat(messages, tools)
5. 如果 LLM 返回 tool_calls:
   5.1 解析为 ToolCall
   5.2 记录 assistant tool_calls 消息
   5.3 ToolExecutor 执行工具
   5.4 EvidenceStore 保存完整 ToolResult
   5.5 ObservationCompressor 压缩 ToolResult
   5.6 压缩摘要作为 tool message 回填 ContextManager
   5.7 记录 ToolCallTrace
6. 如果 LLM 返回纯文本:
   6.1 构造 DiagnosisResult
   6.2 结束
7. 如果超过最大轮数:
   7.1 不再传 tools
   7.2 强制 LLM 基于已有证据给出当前最佳结论
```

关键行为：

- `max_iterations` 默认来自 `MAX_ITERATIONS = 15`，实例上可覆盖。
- LLM 生成非法 JSON 参数时会降级为空参数，避免整轮崩溃。
- 工具异常不会抛出到主循环，而是包装成失败的 `ToolResult` 返回给 LLM。
- 工具完整结果保存到 EvidenceStore，LLM 上下文只进入压缩摘要。

## LLMClient

文件：`agent/llm_client.py`

`LLMClient` 封装 OpenAI-compatible Chat Completions 调用，支持 DeepSeek、OpenAI、本地 vLLM、Ollama 兼容端点等。

输入：

- `messages`：OpenAI chat messages。
- `tools`：OpenAI function calling 格式工具定义。

输出：

- `LLMResponse.content`：最终文本或 assistant 内容。
- `LLMResponse.tool_calls`：工具调用列表。
- `LLMResponse.has_tool_calls`：本轮是否需要执行工具。
- `LLMResponse.is_final`：本轮是否为最终答复。

当前设计保持 LLM 通信层单一职责：它不执行工具，不压缩结果，也不写证据。

注意：本地开发环境需要安装 `openai` 包后才能实际调用 LLM。为了让轻量模块可单独导入，`agent.__init__` 对 `DatabaseHolmesInvestigator` 使用懒加载。

## PromptBuilder

文件：`agent/prompts.py`

`PromptBuilder` 基于三类信息构造 system prompt：

- 基础 `SYSTEM_PROMPT`：MySQL 性能诊断专家角色、安全红线、输出规范。
- `DiagnosisRequest.workflow`：可选诊断流程偏好。
- `ToolRegistry.toolset_descriptions`：当前启用的工具集和工具名称。
- `DiagnosisRequest.metadata`：数据库名、环境、时间窗口等请求元信息。

这样 prompt 拼装从 ReActLoop 中分离，后续可替换为 Jinja2 或多场景 prompt。

## ContextManager

文件：`agent/context.py`

`ContextManager` 管理 OpenAI chat messages，并做上下文预算控制。

消息类型：

| role | 用途 |
|------|------|
| `system` | 系统提示词，永不裁剪 |
| `user` | 用户问题或强制总结指令 |
| `assistant` | LLM 文本回复或 tool_calls |
| `tool` | 工具压缩结果，必须关联 tool_call_id |

裁剪策略：

- 按轮次裁剪，不拆散 assistant tool_calls 和后续 tool result。
- 先按 `max_rounds` 控制轮数，默认保留 10 轮。
- 再按 `max_tokens` 粗估预算裁剪，默认 12000。
- token 估算使用 `字符数 / 2.5`，不引入 tokenizer 依赖。

## 工具系统

### BaseTool

文件：`tools/base.py`

所有工具继承 `BaseTool`：

```python
class ExplainQuery(BaseTool):
    name = "explain_query"
    description = "执行 EXPLAIN 分析 SQL 执行计划"
    parameters = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "要分析的 SQL"}
        },
        "required": ["sql"],
    }
    risk_level = RiskLevel.SAFE

    async def run(self, sql: str) -> dict:
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
    name="mysql",
    description="MySQL 只读诊断工具集",
    tools=[ExplainQuery(), ShowProcesslist()],
    enabled=True,
)
```

当前 `tools/mysql/__init__.py` 提供：

```python
build_mysql_toolset(extra_tools=[...])
```

具体 MySQL 工具还未实现，但后续新增工具时只需加入 toolset，再通过 `ToolRegistry.register_toolset()` 注册。

### ToolRegistry

文件：`tools/registry.py`

`ToolRegistry` 负责：

- `register(tool)`：注册单个工具。
- `register_many(tools)`：批量注册工具。
- `register_toolset(toolset)`：注册工具集并导入其中启用工具。
- `get(name)`：按名称获取工具。
- `to_openai_schema()`：导出 OpenAI function calling schema。
- `toolset_descriptions`：给 PromptBuilder 使用的工具集摘要。

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

## 安全审批

文件：`safety/approval.py`

当前已有可插拔 `ApprovalPolicy`：

| RiskLevel | 默认策略 |
|-----------|----------|
| `SAFE` | 自动通过 |
| `APPROVAL` | MVP 阶段自动通过，并记录日志 |
| `DANGEROUS` | 自动拒绝 |

返回值是 `ApprovalResult`：

- `decision`：`APPROVED`、`DENIED`、`NEEDS_REVIEW`。
- `reason`：审批原因。
- `approved`：便捷布尔属性。

后续 CLI 审批、API 回调、企业审批系统都可以通过替换 `ApprovalPolicy` 接入，不需要改工具执行器。

## 证据存储

文件：`observe/evidence_store.py`

当前实现：

- `EvidenceRecord`：单条证据。
- `EvidenceStore`：协议接口。
- `InMemoryEvidenceStore`：内存版实现。

`EvidenceRecord` 字段：

- `id`：如 `ev_4d33293e0fab`。
- `tool_name`：产生证据的工具。
- `tool_call_id`：对应 LLM tool call。
- `data`：完整工具返回数据。
- `error`：失败信息。
- `success`：是否成功。
- `created_at`：UTC 时间。
- `metadata`：如迭代轮数。

设计原则：

- 完整结果保存到 EvidenceStore。
- 压缩摘要进入 LLM 上下文。
- 最终 `DiagnosisResult.evidence_ids` 只引用证据 ID。

后续可把 `InMemoryEvidenceStore` 替换为 SQLite、对象存储或审计库。

## 观测压缩

文件：`observe/compressor.py`

`ObservationCompressor` 防止工具结果撑爆上下文。它按 `tool_name` 使用 `fnmatch` 匹配压缩策略。

内置策略：

| 模式 | 策略 |
|------|------|
| `explain_*` | 只保留 `table/type/possible_keys/key/key_len/rows/filtered/Extra` |
| `*slow_query*` | 列表摘要，保留 Top N |
| `*processlist*` | 列表摘要，保留 Top N |
| `*list_*` / `*get_*` / `*show_*` / `*rank_*` | 列表摘要 |
| `*` | JSON 序列化后按 `max_chars` 截断 |

默认最大输出字符数是 800。压缩不是为了丢弃证据，而是为了控制 LLM 上下文；完整证据已经由 EvidenceStore 保存。

## 一次完整数据流

以“orders 表为什么突然变慢？”为例：

```text
1. 调用 investigator.investigate("orders 表为什么突然变慢？")

2. Investigator:
   - 构造 DiagnosisRequest
   - PromptBuilder 组装 system prompt
   - 创建 ContextManager 和 ReActLoop

3. ReActLoop:
   - context.add_user_message(question)
   - llm.chat(messages, tools)

4. LLM 返回 tool_calls:
   - list_slow_queries
   - get_table_schema

5. ToolExecutor:
   - 检查工具是否存在
   - ApprovalPolicy 审批
   - 执行工具
   - 返回 ToolResult

6. ReActLoop:
   - EvidenceStore 保存完整 ToolResult
   - ObservationCompressor 压缩结果
   - ContextManager 添加 tool message
   - 记录 ToolCallTrace

7. 重复调用 LLM 和工具，直到信息足够

8. LLM 返回最终文本

9. ReActLoop 返回 DiagnosisResult:
   - answer
   - evidence_ids
   - tool_calls
   - iterations
```

## 当前实现状态

已实现：

- ReAct 主循环。
- OpenAI-compatible LLM 调用封装。
- 上下文管理和预算裁剪。
- 工具基类、工具注册、工具执行。
- Toolset 抽象和 MySQL toolset 工厂。
- 可插拔审批策略。
- 内存版 EvidenceStore。
- 观测压缩器。
- 结构化 DiagnosisRequest / DiagnosisResult。
- Investigator 统一入口。
- Runtime CLI 最小聊天 REPL，可验证模型连通性和上下文裁剪。

占位或待实现：

- MySQL 具体工具：慢查询、表结构、索引、EXPLAIN、锁等待、数据库状态、大表治理。
- Workflow 固定诊断流程。
- Runtime CLI 的 agent/tool 模式。
- 工具结果缓存。
- 跨会话记忆。
- 动态风险评分和真正 HITL。
- 持久化 EvidenceStore。

## 后续扩展建议

优先顺序建议：

1. 补齐 MySQL 只读工具集：`explain_query`、`get_table_schema`、`get_table_indexes`、`show_processlist`。
2. 把 `runtime/cli.py` 从聊天 REPL 扩展为 `DatabaseHolmesInvestigator` 调试入口。
3. 为慢查询诊断加一个 Workflow，把高频排查路径固定下来。
4. 把 `ApprovalPolicy` 接到 CLI 确认流程。
5. 把 `InMemoryEvidenceStore` 替换或扩展为 SQLite。
6. 增加单元测试：ReActLoop、ToolExecutor、PromptBuilder、EvidenceStore。

## 设计边界

本项目负责：

- 数据库诊断 Agent Runtime。
- 工具调用编排。
- 上下文控制。
- 证据留存。
- 安全审批边界。
- 最终诊断报告生成。

本项目不在当前阶段负责：

- 自动执行 DDL。
- 自动 kill 生产连接。
- 自动删除索引。
- 自动迁移或分库分表。
- 多 Agent 协同。
- Web UI。

## 和 HolmesGPT 的关系

本项目不是复制 HolmesGPT，而是借鉴其适合诊断系统的架构思想：

- 用 Investigator 表达一次调查。
- 用 Toolset 表达数据源能力边界。
- 用 EvidenceStore 保留调查证据。
- 用压缩摘要控制 LLM 上下文。
- 用安全策略限制高风险动作。

当前项目只聚焦 MySQL 性能诊断，因此没有引入 HolmesGPT 的全部数据源、插件系统或复杂运行时。这样能保持代码小、职责清楚，也方便后续按真实需求扩展。
