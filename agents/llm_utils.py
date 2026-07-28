"""
公共 LLM 工具 — 为多智能体提供统一的客户端构建和 JSON 解析能力

模型解析优先级（每个字段独立解析）：
  Agent 专属值 → 全局 LLM_* 值 → Provider 内置默认值
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

AgentRole = Literal["sub", "main"]

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-plus",
        "api_key": "",
    },
    "volcengine": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-pro-32k",
        "api_key": "",
    },
}


def _ollama_defaults() -> dict[str, str]:
    """ollama 的默认值优先从 OLLAMA_* 环境变量读取"""
    return {
        "base_url": config.OLLAMA_BASE_URL or "http://localhost:11434/v1",
        "model": config.OLLAMA_MODEL or "qwen3:14b",
        "api_key": "ollama",
    }


@dataclass
class ResolvedModel:
    """解析后的模型配置，每个字段都标注了来源"""

    role: str
    provider: str
    provider_from: str  # "agent" | "global" | "default"
    model: str
    model_from: str
    api_key: str
    api_key_from: str
    base_url: str
    base_url_from: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def summary_lines(self) -> list[str]:
        """返回人类可读的解析摘要"""
        role_name = "子Agent(初筛)" if self.role == "sub" else "主Agent(深度分析)"
        key_display = "已配置" if self.api_key and self.api_key != "placeholder" else "未配置"
        if self.provider == "ollama":
            key_display = "免密(本地)"

        return [
            f"  {role_name}:",
            f"    Provider : {self.provider:15s} ← {self.provider_from}",
            f"    Model    : {self.model:15s} ← {self.model_from}",
            f"    API Key  : {key_display:15s} ← {self.api_key_from}",
            f"    Base URL : {self.base_url[:40]:15s} ← {self.base_url_from}",
        ]


def _pick(agent_val: str, global_val: str, default_val: str) -> tuple[str, str]:
    """三级优先级选择，返回 (值, 来源标签)"""
    if agent_val:
        return agent_val, "Agent专属配置"
    if global_val:
        return global_val, "全局LLM配置"
    if default_val:
        return default_val, "Provider默认值"
    return "", "未配置"


def resolve_model(role: AgentRole) -> ResolvedModel:
    """解析指定角色的最终模型配置，返回完整的来源追踪"""
    if role == "sub":
        a_provider = config.SUB_AGENT_PROVIDER
        a_model = config.SUB_AGENT_MODEL
        a_key = config.SUB_AGENT_API_KEY
        a_url = config.SUB_AGENT_BASE_URL
    else:
        a_provider = config.MAIN_AGENT_PROVIDER
        a_model = config.MAIN_AGENT_MODEL
        a_key = config.MAIN_AGENT_API_KEY
        a_url = config.MAIN_AGENT_BASE_URL

    # Step 1: 解析 provider
    provider, provider_from = _pick(a_provider, config.LLM_PROVIDER, "ollama")
    defaults = (
        _ollama_defaults() if provider == "ollama"
        else PROVIDER_DEFAULTS.get(provider, {})
    )

    # Step 2: 解析其余字段
    model, model_from = _pick(a_model, config.LLM_MODEL, defaults.get("model", ""))
    api_key, key_from = _pick(a_key, config.LLM_API_KEY, defaults.get("api_key", ""))
    base_url, url_from = _pick(a_url, config.LLM_BASE_URL, defaults.get("base_url", ""))

    if not api_key and provider != "ollama":
        logger.warning(
            f"[{role}] API Key 未配置！请运行 python main.py --setup"
        )
        api_key = "placeholder"
        key_from = "未配置(将失败)"

    return ResolvedModel(
        role=role,
        provider=provider,
        provider_from=provider_from,
        model=model,
        model_from=model_from,
        api_key=api_key,
        api_key_from=key_from,
        base_url=base_url,
        base_url_from=url_from,
    )


def resolve_all() -> tuple[ResolvedModel, ResolvedModel]:
    """解析全部 Agent 的最终模型配置"""
    return resolve_model("sub"), resolve_model("main")


def build_client(role: AgentRole = "main") -> tuple[AsyncOpenAI, str]:
    """构建 AsyncOpenAI 客户端，返回 (client, model_name)"""
    resolved = resolve_model(role)
    logger.info(f"初始化 LLM 客户端: {resolved.label} (role={role})")
    client = AsyncOpenAI(
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        timeout=float(config.LLM_REQUEST_TIMEOUT_SECONDS),
        # 业务层已经按阶段记录并执行一次显式重试；关闭 SDK 隐式重试，
        # 避免一个请求在 Actions 中无审计地等待数倍超时。
        max_retries=0,
    )
    return client, resolved.model


def parse_json_object(content: str) -> dict:
    """从 LLM 响应中提取 JSON 对象，不强制要求业务字段。"""
    content = content.strip()

    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL
    )
    if fence_match:
        content = fence_match.group(1).strip()

    brace_match = re.search(r"\{.*\}", content, re.DOTALL)
    if brace_match:
        content = brace_match.group(0)

    # 针对大模型常见的 JSON 格式错误做简单修复
    # 1. 修复忘记转义的双引号或字符串内部换行导致的问题（非常粗略，依赖大模型自身输出质量）
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        logger.debug(f"标准 JSON 解析失败，尝试修复: {e}")
        # 尝试修复末尾多余的逗号
        content = re.sub(r",\s*\}", "}", content)
        content = re.sub(r",\s*\]", "]", content)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e2:
            logger.warning(
                "JSON 修复后解析仍失败；响应长度=%s，不把模型正文写入日志",
                len(content),
            )
            raise e2

    if not isinstance(data, dict):
        raise TypeError("LLM 响应必须是 JSON 对象")
    return data


def parse_json_response(content: str) -> dict:
    """解析旧版项目评分响应，并校验 1-10 分。"""
    data = parse_json_object(content)
    score = int(data["score"])
    if not 1 <= score <= 10:
        raise ValueError(f"score 超出范围: {score}")

    return data
