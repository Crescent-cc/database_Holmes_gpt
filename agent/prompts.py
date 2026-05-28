"""
提示词模板 —— Agent 系统提示词和故障调查相关的 prompt 片段。

PromptBuilder 负责把一次 Investigation 的上下文、工具集摘要和任务偏好
组装成 system prompt。这样 agent 主循环只关心消息流，不直接拼 prompt。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.models import DiagnosisRequest

# ---------------------------------------------------------------------------
# 主系统提示词
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个 RAG 应用故障调查专家（RAG Holmes / RAG Observer）。

## 你的能力
- 诊断 RAG 应用故障：404/500、接口超时、检索为空、召回不相关、回答幻觉、模型或向量库调用失败
- 使用工具获取日志、trace、指标、Spring route、代码片段、RAG trace 和支撑数据源信息
- 基于证据进行根因分析，给出工程师可执行的修复建议，尽量定位到具体文件和代码行

## 工作原则
1. **先调查，再推断**：先通过工具收集证据（日志、trace、route、代码、RAG trace 等），再给出结论
2. **证据驱动**：每个结论都要有工具返回的数据支撑，不要凭空猜测
3. **分步骤排查**：一次只调 1-2 个工具，根据返回结果再决定下一步
4. **压缩感知**：返回给你的工具结果可能经过压缩，如有疑问请用更精确的条件重新查询

## 输出规范
- 最终输出用中文
- 包含"根因分析"、"证据链"和"修复建议"三部分
- 如果证据不足，明确指出还需要什么信息
- 如果定位到代码，给出文件路径、行号和修改方向

## 安全红线
- DANGEROUS 级别的工具你无法调用（会被拒绝），不要尝试
- 优先建议只读排查和人工确认后的变更，不要建议自动修改生产代码、删除数据或执行破坏性操作
"""


@dataclass
class PromptBuilder:
    """按调查请求动态组装系统提示词。"""

    base_prompt: str = SYSTEM_PROMPT

    def build(
        self,
        request: DiagnosisRequest,
        toolsets: list[dict[str, Any]] | None = None,
    ) -> str:
        parts = [self.base_prompt.strip()]

        if request.workflow:
            parts.append(f"\n## 当前诊断流程\n优先按照 `{request.workflow}` 场景组织调查步骤。")

        if toolsets:
            parts.append("\n## 可用工具集")
            for toolset in toolsets:
                if not toolset.get("enabled", True):
                    continue
                tools = ", ".join(toolset.get("tools") or []) or "暂无工具"
                parts.append(
                    f"- {toolset.get('name')}: {toolset.get('description')}。"
                    f"工具：{tools}"
                )

        if request.metadata:
            metadata_lines = [
                f"- {key}: {value}"
                for key, value in request.metadata.items()
            ]
            parts.append("\n## 请求元信息\n" + "\n".join(metadata_lines))

        return "\n".join(parts)

# ---------------------------------------------------------------------------
# 场景类提示词片段（后续按 Workflow 组装）
# ---------------------------------------------------------------------------

HTTP_404_TASK = "请诊断当前 RAG 接口 404 问题，确认请求路径、网关路由、Spring mapping 和调用方代码是否一致。"

RAG_RETRIEVAL_MISS_TASK = "请诊断当前 RAG 检索为空或召回不相关的问题，检查 query rewrite、向量检索、TopK、阈值、metadata filter 和知识库状态。"

RAG_BAD_ANSWER_TASK = "请诊断当前 RAG 回答幻觉、引用错误或答案不相关的问题，检查检索 chunk、prompt 上下文和回答 grounding。"
