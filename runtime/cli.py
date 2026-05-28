"""
命令行入口。

当前先提供最小可用的 LLM 聊天 REPL，方便验证 API Key、模型连通性和
基础上下文管理。后续再把 RAG 故障 workflow 和 toolset 接入成 agent 模式。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agent.context import ContextManager
from agent.llm_client import LLMClient


DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_SYSTEM_PROMPT = (
    "你是 RAG Holmes / RAG Observer 的命令行调试助手。"
    "请用中文回答，保持简洁，遇到不确定的信息要明确说明。"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m runtime.cli",
        description="RAG Holmes 命令行聊天入口",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"模型名称，默认 {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--base-url",
        default="https://api.deepseek.com",
        help="OpenAI-compatible API 地址，默认 DeepSeek",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="采样温度，默认 0.0",
    )
    parser.add_argument(
        "--system",
        default=DEFAULT_SYSTEM_PROMPT,
        help="系统提示词",
    )
    parser.add_argument(
        "--once",
        help="只发送一条消息并退出，适合测试",
    )
    return parser


async def ask_once(client: LLMClient, context: ContextManager, question: str) -> str:
    context.add_user_message(question)
    response = await client.chat(context.get_messages(), tools=None)
    answer = response.content or "(模型未返回内容)"
    context.add_assistant_message(answer)
    return answer


async def run_repl(args: argparse.Namespace) -> int:
    try:
        client = LLMClient(
            model=args.model,
            base_url=args.base_url,
            temperature=args.temperature,
        )
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    system_prompt = (
        f"{args.system}\n"
        f"当前运行时模型配置为 `{args.model}`，API 地址为 `{args.base_url}`。"
        "如果用户询问模型身份，只能基于该运行时配置回答，不要猜测为 OpenAI GPT。"
    )
    context = ContextManager(system_prompt=system_prompt)

    if args.once:
        try:
            answer = await ask_once(client, context, args.once)
        except Exception as exc:
            print(f"调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(answer)
        return 0

    print("RAG Holmes CLI")
    print("输入问题开始聊天；命令：/reset 清空上下文，/exit 退出。")

    while True:
        try:
            question = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not question:
            continue
        if question in {"/exit", "/quit", "exit", "quit"}:
            return 0
        if question == "/reset":
            context.reset()
            print("已清空上下文。")
            continue

        try:
            answer = await ask_once(client, context, question)
        except Exception as exc:
            print(f"调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        print(f"\nAI> {answer}")


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run_repl(args))


if __name__ == "__main__":
    raise SystemExit(main())
