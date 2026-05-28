"""
上下文管理 —— 组装对话消息、控制 Token 预算、裁剪历史。

三级防爆机制：
  1. Evidence Store    → 完整工具结果不入 LLM 上下文，只传压缩摘要
  2. Observation Compressor → 工具结果压缩（见 observe/compressor.py）
  3. Context Budget     → 总 token 超限时裁剪旧消息（本模块负责）

当前模块只负责第 3 层预算裁剪；Evidence Store 和 Observation Compressor
分别由 observe/evidence_store.py 与 observe/compressor.py 实现，并在
ReActLoop 中统一编排。

裁剪策略：
  - System prompt 永远保留（不可丢弃）
  - 超出 max_tokens 时，从最早的消息轮次开始整轮丢弃
  - "一轮"定义为：1 条 user → 1 条 assistant（可能带 tool_calls）→ N 条 tool_result
  - 不拆散同一轮内的 assistant + tool_result 对（否则 LLM API 会报错）
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 消息数据结构
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """单条对话消息，兼容 OpenAI Chat Completions 格式。

    字段说明：
      role         — system / user / assistant / tool
      content      — 消息文本（tool 消息中是压缩后的工具结果）
      tool_calls   — assistant 消息携带的工具调用列表（OpenAI 格式）
      tool_call_id — tool 消息对应的 tool_call.id，用于关联
      name         — tool 消息对应的函数名
    """
    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai_dict(self) -> dict:
        """转为 OpenAI API 接受的 dict 格式。

        只包含非 None 的字段，避免多余 null 值干扰 API。
        """
        msg: dict[str, object] = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.tool_calls is not None:
            msg["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            msg["name"] = self.name
        return msg


# ---------------------------------------------------------------------------
# 上下文管理器
# ---------------------------------------------------------------------------

class ContextManager:
    """上下文管理器 —— 维护当前会话的消息列表，控制 Token 预算。

    职责：
      1. 追加各类消息（user / assistant / tool_result）
      2. 在接近 token 上限时自动裁剪旧消息
      3. 保证消息序列始终有效（assistant+tool 成对出现）

    使用方式：
        ctx = ContextManager(system_prompt="You are a MySQL DBA expert.")
        ctx.add_user_message("orders 表为什么变慢了？")
        ...
        messages = ctx.get_messages()  # 传给 LLMClient.chat()
    """

    # Token 估算系数：中英文混合场景，1 token ≈ 2.5 个字符
    CHARS_PER_TOKEN = 2.5

    def __init__(
        self,
        system_prompt: str,
        max_tokens: int = 12000,
        max_rounds: int = 10,
    ):
        """
        Args:
            system_prompt: 系统提示词（不会被裁剪）
            max_tokens: 上下文最大 token 数阈值，超出时触发裁剪
            max_rounds: 最多保留的对话轮数，优先控制
        """
        self.system_msg = Message(role="system", content=system_prompt)
        self.messages: list[Message] = []
        self.max_tokens = max_tokens
        self.max_rounds = max_rounds

    # ---- 添加消息 ----

    def add_user_message(self, content: str) -> None:
        """添加一条用户消息。"""
        self.messages.append(Message(role="user", content=content))

    def add_assistant_message(
        self,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        """添加一条 assistant 消息。

        Args:
            content: 文本内容（tool_call 场景下通常为 None）
            tool_calls: OpenAI 格式的工具调用列表，如
                [{"id": "call_xxx", "type": "function",
                  "function": {"name": "explain_query", "arguments": "..."}}]
        """
        self.messages.append(Message(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
        ))

    def add_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> None:
        """添加一条 tool 消息（压缩后的工具返回结果）。

        Args:
            tool_call_id: 必须是 assistant 消息中对应 tool_call 的 id
            tool_name: 工具名称
            content: 压缩后的文本结果
        """
        self.messages.append(Message(
            role="tool",
            tool_call_id=tool_call_id,
            name=tool_name,
            content=content,
        ))

    # ---- 获取消息 ----

    def get_messages(self) -> list[dict]:
        """获取完整的消息列表（含 system prompt），裁剪后返回。

        每次调用都会先检查是否需要裁剪，因此无需手动调用 prune。
        """
        self._prune()
        return [self.system_msg.to_openai_dict()] + [
            m.to_openai_dict() for m in self.messages
        ]

    # ---- 裁剪逻辑 ----

    def _prune(self) -> None:
        """裁剪超出预算的消息。

        两层控制：
          1. 轮数裁剪 —— 超过 max_rounds 时丢弃最早轮次
          2. Token 裁剪 —— 超 max_tokens 时逐轮丢弃最早轮次
        """
        rounds = self._group_into_rounds()

        # 第一层：按轮数裁剪
        if len(rounds) > self.max_rounds:
            rounds = rounds[-self.max_rounds:]

        # 第二层：按 token 数裁剪（只要超预算就丢最早一轮）
        while self._estimate_tokens(rounds) > self.max_tokens and len(rounds) > 1:
            rounds = rounds[1:]

        # 展平回 messages 列表
        self.messages = [msg for round_msgs in rounds for msg in round_msgs]

    def _group_into_rounds(self) -> list[list[Message]]:
        """将消息按"轮次"分组。

        每轮从一条 user 消息开始，包含该 user 之后的 assistant（及 tool）消息。
        这样裁剪时不会拆散 assistant+tool 对，保证消息序列对 API 有效。
        """
        rounds: list[list[Message]] = []
        current: list[Message] = []

        for msg in self.messages:
            if msg.role == "user" and current:
                rounds.append(current)
                current = []
            current.append(msg)

        if current:
            rounds.append(current)

        return rounds

    def _estimate_tokens(self, rounds: list[list[Message]] | None = None) -> int:
        """根据字符数粗略估算当前消息的 token 总量。

        这是一个近似值，不需要 100% 精确 —— 只要不超出模型上下文窗口即可。
        """
        if rounds is None:
            rounds = self._group_into_rounds()
        total_chars = len(self.system_msg.content or "")
        for round_msgs in rounds:
            for msg in round_msgs:
                total_chars += len(msg.content or "")
                total_chars += len(str(msg.tool_calls or ""))
        return int(total_chars / self.CHARS_PER_TOKEN)

    # ---- 工具方法 ----

    def reset(self) -> None:
        """清空消息历史（system prompt 保留）。"""
        self.messages.clear()

    @property
    def round_count(self) -> int:
        """当前对话轮数。"""
        return len(self._group_into_rounds())

    @property
    def estimated_tokens(self) -> int:
        """当前估算的 token 数。"""
        return self._estimate_tokens()
