# Agent 框架设计文档

Database HolmesGPT 的自研轻量 Agent Runtime，不依赖 LangChain 等第三方框架。

## 一句话概括

**LLM 通过调用工具获取信息 → 压缩结果防止爆上下文 → 循环推理直到得出最终结论。**

## 核心架构

```
用户输入
  │
  ▼
┌─────────────────────────────────────────────┐
│              ReActLoop (react_loop.py)       │
│          "Think → Act → Observe" 主循环      │
│                                              │
│   while not done:                            │
│       response = llm.chat(messages, tools)   │
│       if response 要调工具:                   │
│           执行工具 → 压缩结果 → 加入上下文     │
│       else:                                  │
│           return 最终答案                     │
└──────┬──────────┬───────────┬───────────────┘
       │          │           │
  ┌────▼────┐ ┌───▼────┐ ┌───▼────────┐
  │LLMClient│ │ToolExec│ │ContextMgr  │
  │         │ │utor    │ │            │
  └─────────┘ └───┬────┘ └────────────┘
                  │
          ┌───────▼────────┐
          │ ToolRegistry    │
          │ (所有工具的目录) │
          └───────┬────────┘
                  │
          ┌───────▼────────────┐
          │ ObservationCompres-│
          │ sor (结果压缩)      │
          └────────────────────┘
```

## 五大模块详解

### 1. LLMClient — 大模型调用封装

**文件**: `agent/llm_client.py`

**核心类**: `LLMClient`、`LLMResponse`

**做什么**：把调用大模型的细节封装起来，让上游代码（ReActLoop）只需要一行 `await llm.chat(messages, tools)` 就能拿到结构化结果。

**输入**：
- `messages` — 完整的对话历史（list[dict]，OpenAI 格式）
- `tools` — 所有可用工具的定义（list[dict]，OpenAI function calling 格式）

**输出**（`LLMResponse`）：
- `content` — 纯文本回复（最终答案时使用）
- `tool_calls` — 工具调用列表（需要执行工具时使用）
- `has_tool_calls` — 是否为工具调用（Agent 靠这个属性判断走哪个分支）
- `is_final` — 是否为最终回复

**关键设计**：
- 基于 `openai.AsyncOpenAI`，兼容所有 OpenAI-API-compatible 的服务（DeepSeek、OpenAI、本地 vLLM/Ollama）
- 内置指数退避重试（1s → 2s → 4s），网络抖动时自动恢复
- `temperature=0.0`（诊断场景需要确定性，不需要创意）

**为什么有两个返回形态**：LLM 的 function calling 机制有两种响应：
1. 模型决定调用工具 → 返回 tool_calls（content 通常为 None 或思考过程）
2. 模型认为信息充足 → 返回纯文本结论（tool_calls 为 None）

`LLMResponse` 把这两种统一成一个对象，上游用 `response.has_tool_calls` 一行判断即可。

**切换模型的成本**：只需要改三个参数 `model`、`api_key`、`base_url`，代码其余部分完全不动。

---

### 2. Tool System — 工具系统三件套

**文件**: `tools/base.py`、`tools/registry.py`、`tools/executor.py`

**核心类**: `BaseTool`、`ToolRegistry`、`ToolExecutor`

这三个文件各自职责清晰，互不粘连。

#### 2.1 BaseTool — 工具定义

```python
class ExplainQuery(BaseTool):
    name = "explain_query"
    description = "执行 EXPLAIN 分析 SQL 执行计划"
    parameters = {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "要分析的 SQL"}},
        "required": ["sql"]
    }
    risk_level = RiskLevel.SAFE

    async def run(self, sql: str) -> dict:
        # 实际查 MySQL
        return {...}
```

每个工具只需要告诉框架三件事：
- **我是谁** — `name`、`description`、`parameters`（JSON Schema，直接喂给 LLM）
- **我安不安全** — `risk_level`（Safe / Approval / Dangerous）
- **我怎么执行** — `run()` 方法（框架不关心内部实现）

`parameters` 格式遵循 JSON Schema draft-07，和 OpenAI function calling 完全对齐，不需要二次转换。

#### 2.2 ToolRegistry — 工具目录

一个 `dict[str, BaseTool]` 的包装，提供：
- `register(tool)` / `register_many(tools)` — 注册工具
- `get(name)` — 按名称查找（O(1)）
- `to_openai_schema()` — 一键导出为 OpenAI 格式，直接当 Chat API 的 `tools` 参数

所有工具必须在 ReActLoop 启动前注册完成。运行时只读访问，不需要加锁。

#### 2.3 ToolExecutor — 工具执行

接收 LLM 返回的 `ToolCall` 列表，找到对应工具并执行。核心逻辑：

```
execute_one(tool_call):
    1. 去 Registry 找工具 → 找不到返回错误
    2. 检查 risk_level:
       DANGEROUS → 直接拒绝，返回 mock 错误消息
       APPROVAL  → MVP 阶段放行（预留审批拦截点）
       SAFE      → 直接执行
    3. try: 执行 tool.run(**args) → 返回 ToolResult(success=True)
       except: 捕获异常 → 返回 ToolResult(success=False, error=...)
```

**为什么要捕获异常而不是让它炸**：工具执行是 LLM 推理链的一部分。如果一次工具调用抛异常导致整个循环终止，之前收集的证据全丢了。捕获异常并作为 tool_result 返回给 LLM，LLM 可以看到错误信息并调整策略（比如换个参数重试）。

#### 2.4 数据结构

**ToolCall** — LLM 请求的工具调用（id + name + arguments）
**ToolResult** — 工具执行结果（tool_call_id + success + data/error）

这两个是纯数据类，在 LLMClient → ReActLoop → ToolExecutor 之间传递，不包含任何逻辑。

**为什么 ToolResult 要带 tool_call_id**：OpenAI API 要求 tool 消息必须用 `tool_call_id` 关联回 assistant 消息中对应的 tool_call。不关联会返回 400 错误。ToolResult 携带这个 ID，ContextManager 才能正确构造消息。

---

### 3. ContextManager — 上下文管理

**文件**: `agent/context.py`

**核心类**: `ContextManager`、`Message`

**职责**：维护对话消息列表 + 控制 token 预算，防止爆上下文窗口。

#### 3.1 Message 数据结构

一条消息就是一个 `Message` 对象，字段直接映射 OpenAI Chat Completions 格式：

| role | content | tool_calls | tool_call_id | name | 含义 |
|------|---------|-----------|-------------|------|------|
| system | ✅ | ❌ | ❌ | ❌ | 系统提示词 |
| user | ✅ | ❌ | ❌ | ❌ | 用户提问 |
| assistant | 可选 | 可选 | ❌ | ❌ | 模型回复/工具调用 |
| tool | ✅ | ❌ | ✅ | ✅ | 工具执行结果 |

`to_openai_dict()` 方法只输出非 None 字段，避免多余 null 干扰 API。

#### 3.2 添加消息的四个方法

```
add_user_message("orders 表为什么变慢了？")
add_assistant_message(content=None, tool_calls=[...])
add_tool_result(tool_call_id="call_xxx", tool_name="explain_query", content="{...}")
```

注意 `add_tool_result` 接收的 `content` 是**压缩后的文本**，原始 dict 不会进入这个方法。压缩在 ReActLoop 里由 Compressor 完成后再传进来。

#### 3.3 裁剪机制（核心）

**两层控制**：

```
第一层：轮数裁剪
  每轮 = 1 条 user + 1 条 assistant + N 条 tool
  超过 max_rounds (默认10) → 丢弃最早轮次

第二层：Token 裁剪
  粗略估算：total_chars / 2.5 ≈ token 数
  超过 max_tokens (默认 12000) → 逐轮丢弃最早轮次
```

**关键规则**——不拆散同一轮内的 assistant + tool 消息对。如果拆散了，OpenAI API 会报"tool message without matching tool_call"错误。所以裁剪粒度是"整轮"，不是"单条消息"。

**System prompt 永不丢弃**——无论怎么裁剪，system prompt 始终保留。这是 Agent 的工作守则，丢了行为不可控。

#### 3.4 Token 估算为什么是 `/ 2.5` 而不是精确计算

精确计算需要调 tokenizer，每个模型的 tokenizer 不同。`/ 2.5` 是一个中英文混合场景下的经验值，误差在 ±15% 左右。对于"防止超出 128K 上下文窗口"这个目标来说足够了 —— 保守估计即可，不需要精确。

---

### 4. ObservationCompressor — 观测压缩器

**文件**: `observe/compressor.py`

**核心类**: `ObservationCompressor`

**为什么需要这个模块**：MySQL 一条 `SHOW FULL PROCESSLIST` 可能返回上千行，直接全量放进 LLM 上下文会：
- Token 消耗过快，成本爆增
- 超出模型上下文窗口
- LLM 注意力被大量无关数据稀释，诊断质量下降

**工作方式**——策略模式：

```
compress(tool_name, raw_result):
    1. 按 tool_name 匹配压缩策略（fnmatch 通配符）
    2. 调用匹配到的策略函数
    3. 返回压缩后的字符串
```

**内置策略**：

| 匹配模式 | 策略 | 对应用到的工具 |
|---------|------|-------------|
| `explain_*` | 只保留 type/key/rows/filtered/Extra 五个字段 | EXPLAIN、EXPLAIN ANALYZE |
| `*slow_query*` | 保留 Top 5 条 + 总数 | 慢查询列表 |
| `*processlist*` | 保留 Top 5 条 + 总数 | 进程列表 |
| `*list_*` / `*get_*` 等 | 保留 Top 5 条 + 总数 | 表结构、索引等 |
| `*`（兜底） | JSON 序列化后按 max_chars 截断 | 其他所有工具 |

**自定义策略**：`compressor.register("my_tool", my_strategy_fn)`，一行注册。

**策略匹配优先级**：按注册顺序，先注册的先匹配。所以具体模式（`explain_*`）要排在通用模式（`*`）之前。当前代码在 `_register_builtin_strategies` 里已保证了顺序。

#### 四种策略的具体逻辑

**`_compress_explain`**（EXPLAIN 专用）：
```json
// 压缩前（完整结果）
{"id": 1, "select_type": "SIMPLE", "table": "orders", "partitions": null,
 "type": "ALL", "possible_keys": null, "key": null, "key_len": null,
 "ref": null, "rows": 1200000, "filtered": 100.0, "Extra": "Using where; Using filesort"}

// 压缩后（只保留诊断关键字段）
{"table": "orders", "type": "ALL", "key": null, "rows": 1200000,
 "filtered": 100.0, "Extra": "Using where; Using filesort"}
```

去掉的字段（id、select_type、partitions、possible_keys 等）在性能诊断中基本不提供额外信息。

**`_compress_list`**（列表类通用）：
```json
{"total_count": 150, "shown_count": 5, "top_results": [...]}
```
LLM 看到 total_count 和 top 5 就足以判断趋势，不需要全量数据。

**`_compress_default`**（兜底策略）：

超过 `max_chars`（默认 800）时，保留头尾各一半，中间插入截断提示。这样 LLM 既能看到前几条关键数据，也能感知到数据不完整（如果需要可以调更精确的工具再查）。

---

### 5. ReActLoop — 推理主循环

**文件**: `agent/react_loop.py`

**核心类**: `ReActLoop`

**在整个框架中的角色**：把一个 LLM 变成一个能自主使用工具的 Agent。它的任务是把上面四个模块串起来跑一个 while 循环，直到 LLM 给出最终结论。

#### 完整执行流程

```
run(user_input="orders 表为什么变慢了？"):
    │
    ├─ 1. context.add_user_message(user_input)    # 放入用户问题
    │
    └─ 2. 进入 while 循环（最多 MAX_ITERATIONS=15 轮）
         │
         ├─ 2a. 检查迭代次数                  # 防止死循环
         │
         ├─ 2b. messages = context.get_messages()  # 自动裁剪
         │     tools = registry.to_openai_schema()  # 工具列表
         │     response = await llm.chat(messages, tools)
         │
         ├─ 2c. 如果 response.has_tool_calls:
         │       │
         │       ├─ 解析 tool_calls JSON → ToolCall 列表
         │       ├─ context.add_assistant_message(tool_calls=...)
         │       ├─ 逐个 await executor.execute_one(tc)
         │       │     ↓
         │       │   ToolResult(success, data/error)
         │       ├─ compressor.compress(tool_name, result)
         │       │     ↓
         │       │   压缩后的文本字符串
         │       └─ context.add_tool_result(id, name, compressed_text)
         │              ↓
         │         回到 2b，继续循环
         │
         └─ 2d. 如果 response.is_final:
                return response.content           # 输出最终诊断结论
```

#### 终止条件

1. **正常终止**：LLM 返回纯文本（`is_final = True`），说明它认为信息足够给出结论
2. **强制终止**：达到 `MAX_ITERATIONS=15` 轮，给 LLM 最后一次机会基于已有信息总结（不带 tools，强制只输出文本）
3. **异常退出**：理论上不会走到，作为安全兜底返回错误提示

#### 为什么 MAX_ITERATIONS 是 15

一次典型的慢查询诊断需要 5-8 轮工具调用：
- 列出慢查询 → 选目标 → 看表结构 → 看索引 → EXPLAIN → 结论

15 轮给了足够的余量，同时防止模型陷入"调工具 → 不满意 → 重新调 → 还是不满意"的死循环。

#### 错误处理策略

- LLM 返回的 JSON 可能非法 → 用 `try: json.loads()` 兜底，失败时 arguments={}
- 工具执行失败 → 不抛异常，把错误信息作为 tool_result 返回给 LLM，让它看见并调整
- 网络错误 → LLMClient 内部重试，不影响上层
- 达到最大轮次 → 强制 LLM 输出最佳结论

#### 各模块的分工

```
工具 → 拿证据
规则 → 稳定分类（后续实现）
Workflow → 流程控制（后续实现）
LLM → 综合解释、生成报告和优化方案
HITL → 控制高风险动作（后续实现）
```

---

## 整体数据流（一帧看完）

以"orders 表为什么变慢了？"为例：

```
[1] ContextManager.add_user_message("orders 表为什么变慢了？")

[2] LLMClient.chat(messages, tools=registry.to_openai_schema())
    → LLMResponse(tool_calls=[
        {"name": "list_slow_queries", "arguments": "{}"},
        {"name": "get_table_schema", "arguments": "{\"table\": \"orders\"}"}
      ])

[3] ContextManager.add_assistant_message(tool_calls=[...])

[4] ToolExecutor.execute_many([
        ToolCall(id="call_1", name="list_slow_queries", arguments={}),
        ToolCall(id="call_2", name="get_table_schema", arguments={"table": "orders"})
    ])
    → [ToolResult(data={...150条慢查询...}), ToolResult(data={...orders表结构...})]

[5] ObservationCompressor.compress("list_slow_queries", result)
    → '{"total_count": 150, "top_results": [...]}'   # 只保留 Top 5

    ObservationCompressor.compress("get_table_schema", result)
    → '{"total_count": 12, "top_results": [...]}'     # 12 列 → Top 5

[6] ContextManager.add_tool_result("call_1", "list_slow_queries", compressed_1)
    ContextManager.add_tool_result("call_2", "get_table_schema", compressed_2)

[7] LLMClient.chat(messages_with_results, tools)
    → LLMResponse(tool_calls=[{"name": "explain_query", "arguments": "{...}"}])

...（重复步骤 3-6）...

[N] LLMClient.chat(messages, tools)
    → LLMResponse(content="orders 查询全表扫描 120 万行 + filesort，
        建议 CREATE INDEX idx_user_status_created_at ON orders(...)")
    → is_final=True → 循环结束
```

---

## 安全机制（三级风险）

工具执行前按 `risk_level` 分级处理：

| 级别 | 策略 | 示例 |
|------|------|------|
| **Safe** | 直接执行 | `EXPLAIN`、`SHOW CREATE TABLE`、`SELECT` |
| **Approval** | MVP 阶段直接执行（预留审批拦截点） | `GENERATE INDEX DDL`、`KILL SESSION` |
| **Dangerous** | 直接拒绝，返回 mock 错误 | `DROP INDEX`、`ALTER TABLE`、`TRUNCATE` |

后续 HITL 模块会实现：
- Approval 级别 → CLI 弹出确认提示 `[y/N]`
- Dangerous 级别 → 永远拒绝自动执行，必须走人工流程

---

## 框架的边界

**框架负责**：
- 管理 LLM 对话和工具调用的循环
- 维护上下文窗口不超限
- 压缩工具返回结果
- 按风险等级控制工具执行

**框架不负责**：
- 工具的具体实现（数据库连接、SQL 执行由工具类自己处理）
- 诊断逻辑的正确性（那是 LLM 和工具的事）
- 持久化存储（后续 Evidence Store 模块负责）
- 多 Agent 协作

---

## 目录结构（有代码的用 ★ 标注）

```
./
├── agent/
│   ├── llm_client.py    ★ LLM 调用封装（260行）
│   ├── react_loop.py    ★ ReAct 推理循环（150行）
│   ├── context.py       ★ 上下文管理（170行）
│   ├── prompts.py       ★ 提示词模板（50行）
│   └── memory.py          对话记忆（占位）
├── tools/
│   ├── base.py          ★ 工具基类 + 数据结构（110行）
│   ├── registry.py      ★ 工具注册中心（70行）
│   ├── executor.py      ★ 工具执行器（100行）
│   └── mysql/             MySQL 工具集（占位）
├── observe/
│   ├── compressor.py    ★ 观测压缩器（190行）
│   ├── cache.py           结果缓存（占位）
│   └── evidence_store.py  证据存储（占位）
├── workflow/
│   └── base.py            诊断 Workflow（占位）
├── safety/
│   ├── tool_risk.py       工具风险分级（占位）
│   └── approval.py        人工审批（占位）
└── runtime/
    └── cli.py             命令行入口（占位）
```

- ★ = 已完整实现，约 1100 行 Python
- 其余为占位文件，各含 docstring 说明后续要做什么

---

## 关键设计决策

**1. 为什么用了异步（async/await）**

LLM 调用和数据库查询都是 I/O 密集操作，async 能避免阻塞。更重要的是，后续多个工具可以 `asyncio.gather` 并发执行（当工具之间没有依赖时），降低端到端延迟。

**2. 为什么不直接用 LangChain**

LangChain 的 AgentExecutor 是黑盒，出了问题难调试。自研框架约 1100 行代码，每个环节都可见可控。而且 MySQL 诊断场景的 ReAct 循环固定，不需要 LangChain 的通用性。

**3. 为什么压缩器用 fnmatch 模式匹配而不是注册时指定策略**

因为同一个压缩策略可能适用于多个工具（如 `_compress_list` 适用于所有返回列表的工具）。模式匹配比逐个工具指定策略更简洁，也更容易添加新工具。

**4. 为什么上下文裁剪是估算而不是精确 token 计数**

精确计数需要加载对应模型的 tokenizer（几 MB 到几十 MB），引入不必要的依赖。`字符数 / 2.5` 的估算在 ±15% 误差内，对"预裁剪"这个目标来说足够。

**5. 为什么工具执行在 ReActLoop 里而非 LLMClient 里**

LLMClient 只负责"和模型说话"。工具执行涉及风险分级、结果压缩、上下文追加——这些是 Agent 的业务逻辑，不应该塞在通信层。职责单一，测试也更容易。
