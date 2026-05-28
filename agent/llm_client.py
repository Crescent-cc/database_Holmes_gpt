"""
LLM 调用封装 —— 统一的模型调用接口。

设计要点：
  - 基于 OpenAI Python SDK（兼容所有 OpenAI-API-compatible 的模型服务）
  - 自动解析 function calling 的响应为结构化 LLMResponse
  - 指数退避重试（网络瞬时故障）
  - 与具体模型解耦 —— 切换模型只需要改 model / base_url 参数

支持的模型服务（只要兼容 OpenAI API 即可）：
  - DeepSeek:   base_url="https://api.deepseek.com"
  - OpenAI:     base_url="https://api.openai.com/v1"
  - 本地 vLLM:   base_url="http://localhost:8000/v1"
  - 本地 Ollama: base_url="http://localhost:11434/v1"
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 标准化响应
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    """LLM 返回的标准化响应，统一文本回复和工具调用两种模式。

    字段说明：
      content    — 纯文本回复（tool_call 场景下通常为 None 或思考过程）
      tool_calls — OpenAI 格式的工具调用列表
    """
    content: str | None = None
    tool_calls: list[dict] | None = None

    @property
    def has_tool_calls(self) -> bool:
        """本次响应是否需要调用工具。"""
        return self.tool_calls is not None and len(self.tool_calls) > 0

    @property
    def is_final(self) -> bool:
        """是否为最终回复（不含工具调用）。"""
        return not self.has_tool_calls


# ---------------------------------------------------------------------------
# LLM 客户端
# ---------------------------------------------------------------------------

class LLMClient:
    """LLM 客户端 —— 封装模型调用的创建、发送和响应解析。

    使用示例：
        client = LLMClient(
            model="deepseek-v4-flash",
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
        )
        response = await client.chat(messages, tools)
        if response.has_tool_calls:
            ...  # 执行工具
        else:
            print(response.content)  # 获取最终答案
    """

    def __init__(
        self,
        model: str = "deepseek-v4-flash",
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        temperature: float = 0.0,
        max_retries: int = 2,
    ):
        """
        Args:
            model: 模型名称标识
            api_key: API 密钥
            base_url: API 端点地址（含协议和路径）
            temperature: 采样温度，诊断场景推荐 0.0（确定性输出）
            max_retries: 网络错误时最大重试次数
        """
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        resolved_api_key = api_key or self._resolve_api_key(base_url)

        self.client = AsyncOpenAI(
            api_key=resolved_api_key,
            base_url=base_url,
        )

    @staticmethod
    def _resolve_api_key(base_url: str) -> str:
        """从环境变量解析 API Key，避免在代码中保留密钥占位。"""
        LLMClient._load_local_env()
        env_names = ["LLM_API_KEY"]
        if "deepseek" in base_url:
            env_names.insert(0, "DEEPSEEK_API_KEY")
        if "openai" in base_url:
            env_names.insert(0, "OPENAI_API_KEY")

        for name in env_names:
            value = os.getenv(name)
            if value:
                return value

        raise ValueError(
            "LLM API key is required. Pass api_key explicitly or set one of: "
            + ", ".join(env_names)
        )

    @staticmethod
    def _load_local_env(env_path: str | Path = ".env") -> None:
        """加载当前项目 .env 中尚未存在的环境变量。

        只支持 KEY=VALUE 的简单格式，避免为了读取本地密钥引入额外依赖。
        """
        path = Path(env_path)
        if not path.exists():
            return

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """发送消息到 LLM 并返回标准化响应。

        Args:
            messages: OpenAI 格式的对话历史
            tools: OpenAI function calling 格式的工具定义，传 None 则不传 tools
                字段（此时 LLM 只能返回纯文本）

        Returns:
            LLMResponse: 解析后的标准化响应

        Raises:
            openai.APIError: 在重试耗尽后仍然失败时抛出
        """
        for attempt in range(self.max_retries + 1):
            try:
                return await self._call_api(messages, tools)
            except Exception as exc:
                logger.warning(
                    "LLM call attempt %d/%d failed: %s",
                    attempt + 1, self.max_retries + 1, exc,
                )
                if attempt < self.max_retries:
                    # 指数退避：1s → 2s → 4s ...
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise

    async def _call_api(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> LLMResponse:
        """执行单次 API 调用并解析结果。"""
        # 构建请求参数
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools

        # 调用 OpenAI-compatible API
        completion = await self.client.chat.completions.create(**kwargs)
        choice = completion.choices[0]
        message = choice.message

        # --- 解析 tool_calls ---
        if message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]
            logger.debug(
                "LLM requested %d tool call(s): %s",
                len(tool_calls),
                [tc["function"]["name"] for tc in tool_calls],
            )
            return LLMResponse(
                content=message.content,
                tool_calls=tool_calls,
            )

        # --- 纯文本回复（最终答案） ---
        logger.debug("LLM returned final text response (%d chars)", len(message.content or ""))
        return LLMResponse(content=message.content)
