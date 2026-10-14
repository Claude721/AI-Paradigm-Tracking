"""
公共 LLM 工具 — 为多智能体提供统一的客户端构建和 JSON 解析能力

MAIN/SUB 各自显式配置模型与密钥；不继承全局或另一角色，不猜测默认模型。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal
from types import SimpleNamespace
from urllib.parse import urlparse

from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

AgentRole = Literal["sub", "main"]

@dataclass
class ResolvedModel:
    """解析后的模型配置，每个字段都标注了来源"""

    role: str
    provider: str
    provider_from: str  # Agent 专属配置或未配置，不再有全局/default 来源。
    model: str
    model_from: str
    api_key: str
    api_key_from: str
    base_url: str
    base_url_from: str
    request_options: str = ""

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def summary_lines(self) -> list[str]:
        """返回人类可读的解析摘要"""
        role_name = "子Agent(初筛)" if self.role == "sub" else "主Agent(深度分析)"
        key_display = "已配置" if self.api_key and self.api_key != "placeholder" else "未配置"
        if self.provider == "ollama":
            key_display = "免密(本地)"

        lines = [
            f"  {role_name}:",
            f"    Provider : {self.provider:15s} ← {self.provider_from}",
            f"    Model    : {self.model:15s} ← {self.model_from}",
            f"    API Key  : {key_display:15s} ← {self.api_key_from}",
            f"    Base URL : {self.base_url[:40]:15s} ← {self.base_url_from}",
        ]
        policy = request_policy(self)
        lines.append(f"    输出参数 : {policy['output_limit']}；请求策略来自角色配置/提供方协议，不依赖模型名或地址")
        return lines


def resolve_model(role: AgentRole) -> ResolvedModel:
    """只读取本角色字段；缺项保留为空，Doctor/build_client 明确阻断。"""
    if role not in {"sub", "main"}:
        raise ValueError("未知模型角色")
    prefix = "SUB_AGENT" if role == "sub" else "MAIN_AGENT"
    provider = getattr(config, prefix + "_PROVIDER").strip()
    model = getattr(config, prefix + "_MODEL").strip()
    base_url = getattr(config, prefix + "_BASE_URL").strip()
    api_key = getattr(config, prefix + "_LLM_API_KEY").strip()
    def source(value):
        return "Agent专属配置" if value else "未配置"
    key_from = source(api_key)
    if provider == "ollama" and not api_key:
        api_key, key_from = "ollama", "本地免密"

    return ResolvedModel(
        role=role,
        provider=provider,
        provider_from=source(provider),
        model=model,
        model_from=source(model),
        api_key=api_key,
        api_key_from=key_from,
        base_url=base_url,
        base_url_from=source(base_url),
        request_options=getattr(config, prefix + "_REQUEST_OPTIONS", ""),
    )


def resolve_all() -> tuple[ResolvedModel, ResolvedModel]:
    """解析全部 Agent 的最终模型配置"""
    return resolve_model("sub"), resolve_model("main")


def request_policy(resolved: ResolvedModel, *, model: str = "") -> dict:
    """Configuration-driven Chat Completions dialect, also a cache identity.

    Provider is an explicit protocol choice, NOT a price/trust assertion.
    Model names and endpoint hosts never select business code paths. A new
    compatible service needs configuration, not a new model allowlist.
    """
    dashscope = resolved.provider.casefold() == "dashscope"
    policy = {"version": 2,
              "output_limit": "max_completion_tokens" if dashscope else "max_tokens",
              "extra_body": {"enable_thinking": config.LLM_ENABLE_THINKING} if dashscope else {},
              "omit_parameters": []}
    raw = getattr(resolved, "request_options", "")
    if raw:
        try:
            options = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError("角色 REQUEST_OPTIONS 必须为有效 JSON 对象") from None
        if not isinstance(options, dict) or set(options) - {"output_limit", "extra_body", "omit_parameters"}:
            raise ValueError("角色 REQUEST_OPTIONS 包含未知字段")
        policy.update(options)
    if not isinstance(policy["output_limit"], str) or policy["output_limit"] not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("角色 REQUEST_OPTIONS.output_limit 无效")
    body = policy["extra_body"]
    if not isinstance(body, dict) or any(
        not isinstance(key, str) or key in {"model", "messages", "max_tokens", "max_completion_tokens",
        "stream", "tools", "tool_choice", "api_key", "authorization", "response_format"}
        or any(word in key.casefold() for word in ("secret", "password", "token", "api_key", "authorization"))
        for key in body
    ):
        raise ValueError("角色 REQUEST_OPTIONS.extra_body 无效或包含保留字段/凭据")
    if "enable_thinking" in body and type(body["enable_thinking"]) is not bool:
        raise ValueError("enable_thinking 必须为布尔值")
    omissions = policy["omit_parameters"]
    if not isinstance(omissions, list) or any(not isinstance(value, str) or value not in {"temperature", "response_format"} for value in omissions):
        raise ValueError("omit_parameters 只允许 temperature / response_format")
    policy["omit_parameters"] = sorted(set(omissions))
    return policy


class ModelPolicyClient:
    """Apply the same explicit request contract in Smoke and all model stages.

    Delegate SDK lifecycle/base_url/retry attributes so monetary admission can
    validate the actual transport. The request cap bounds reasoning + answer,
    even when thinking is explicitly enabled; it is not a silent budget raise.
    """
    def __init__(self, client: AsyncOpenAI, resolved: ResolvedModel):
        self._client = client
        self._resolved = resolved
        self._policy = request_policy(resolved)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def create(self, **kwargs):
        payload = dict(kwargs)
        if payload.get("model") != self._resolved.model:
            raise ValueError("请求模型与本角色配置不一致；请修改角色配置，不在阶段内切换模型")
        if self._policy:
            body = dict(payload.get("extra_body") or {})
            configured = self._policy["extra_body"]
            if any(key in body and body[key] != value for key, value in configured.items()):
                raise ValueError("请求参数与本角色 REQUEST_OPTIONS 配置冲突")
            body.update(configured)
            if body:
                payload["extra_body"] = body
            values = [payload.pop(key) for key in ("max_tokens", "max_completion_tokens") if key in payload]
            if values:
                if any(value != values[0] for value in values):
                    raise ValueError("请求包含冲突的输出上限")
                if type(values[0]) is not int or values[0] < 1:
                    raise ValueError("输出上限必须为正整数")
                payload[self._policy["output_limit"]] = values[0]
            for key in self._policy["omit_parameters"]:
                payload.pop(key, None)
        return await self._client.chat.completions.create(**payload)


def build_client(
    role: AgentRole = "main",
    *,
    timeout_seconds: int | float | None = None,
) -> tuple[ModelPolicyClient, str]:
    """构建带显式供应商契约的客户端，返回 (client, model_name)"""
    resolved = resolve_model(role)
    prefix = "SUB_AGENT" if role == "sub" else "MAIN_AGENT"
    missing = [prefix + "_" + field for field, value in (
        ("PROVIDER", resolved.provider), ("MODEL", resolved.model),
        ("BASE_URL", resolved.base_url), ("LLM_API_KEY", resolved.api_key))
        if not value or value == "placeholder"]
    if missing:
        raise ValueError("本角色模型配置缺失：" + "、".join(missing))
    endpoint = urlparse(resolved.base_url)
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
        or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError("本角色 BASE_URL 必须是无凭据、查询或片段的 API 根地址")
    request_policy(resolved)  # Validate before allocating a network client.
    logger.info(f"初始化 LLM 客户端: {resolved.label} (role={role})")
    client = AsyncOpenAI(
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        timeout=float(timeout_seconds or config.LLM_REQUEST_TIMEOUT_SECONDS),
        # 业务层已经按阶段记录并执行一次显式重试；关闭 SDK 隐式重试，
        # 避免一个请求在 Actions 中无审计地等待数倍超时。
        max_retries=0,
    )
    return ModelPolicyClient(client, resolved), resolved.model


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


def completed_content(response) -> str:
    """A syntactically valid prefix is not a completed model judgment.

    Validate after cost admission has received usage, before business parsing
    or success accounting. No response body is included in the exception.
    Missing attribute is tolerated for existing injected/offline clients; SDK
    choice objects carry the attribute and must explicitly finish with stop.
    """
    choice = response.choices[0]
    reason = getattr(choice, "finish_reason", None)
    if hasattr(choice, "finish_reason") and reason != "stop":
        label = reason if reason in {"length", "content_filter", "tool_calls", "function_call"} else "unknown"
        raise ValueError("模型输出未完整结束；finish_reason=" + label)
    content = choice.message.content
    if content is not None and not isinstance(content, str):
        raise TypeError("模型回答必须为文本")
    return content or ""


def parse_json_response(content: str) -> dict:
    """解析旧版项目评分响应，并校验 1-10 分。"""
    data = parse_json_object(content)
    score = int(data["score"])
    if not 1 <= score <= 10:
        raise ValueError(f"score 超出范围: {score}")

    return data
