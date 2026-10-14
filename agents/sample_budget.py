"""Configuration-driven, fail-closed cost admission for authorized samples.

No model/endpoint/price/date allowlist. Each role supplies a dated price
contract. This journal is a per-test upper bound, not an account billing limit.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse


class SampleBudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class PriceContract:
    model: str
    base_url: str
    input_limit: int
    input_price_cny: str
    output_price_cny: str
    output_limit: str
    output_tolerance: int
    response_models: tuple[str, ...]
    verified_on: str
    pricing_url: str
    contract_url: str
    completion_includes_reasoning: bool

    @classmethod
    def parse(cls, value, *, model, base_url, verified_on, now=None):
        try:
            if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
                raise ValueError()
            contract = cls(**(value | {"response_models": tuple(value["response_models"])}))
            current = now or datetime.now(timezone.utc).date()
            if (contract.verified_on != verified_on.isoformat() or verified_on != current
                or contract.model != model or contract.base_url.rstrip("/") != base_url.rstrip("/")):
                raise ValueError()
            for address in (base_url, contract.pricing_url, contract.contract_url):
                parsed = urlparse(address)
                if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                    raise ValueError()
            if (type(contract.input_limit) is not int or not 1 <= contract.input_limit <= 10_000_000
                or contract.output_limit not in {"max_tokens", "max_completion_tokens"}
                or type(contract.output_tolerance) is not int or not 0 <= contract.output_tolerance <= 100
                or contract.completion_includes_reasoning is not True
                or not isinstance(value["response_models"], list) or model not in contract.response_models
                or any(not isinstance(item, str) or not item for item in contract.response_models)):
                raise ValueError()
            for price in (contract.input_price_cny, contract.output_price_cny):
                if not isinstance(price, str) or not Decimal(price).is_finite() or Decimal(price) <= 0:
                    raise ValueError()
            return contract
        except Exception:
            raise SampleBudgetError("本角色价格契约无效/过期/不匹配；核验官方价格后更新配置，不更换模型") from None


class SampleBudgetClient:
    """All roles serialize against ONE durable reservation/settlement ledger.

    Reserve the whole declared input capacity, not a tokenizer approximation.
    Unknown charge/cancellation stops every role; no automatic retry or refund.
    """
    def __init__(self, client, *, path: Path, budget_cny, verified_on: date,
                 model, base_url, price_contract, now: date | None = None):
        try:
            budget = Decimal(str(budget_cny))
        except Exception as exc:
            raise SampleBudgetError("费用上限无效") from exc
        if not budget.is_finite() or not 0 < budget <= 20:
            raise SampleBudgetError("单路线验收费用必须在 0 到 20 元内")
        self.verified_on, self.now = verified_on, now
        contract = PriceContract.parse(price_contract, model=model, base_url=base_url,
                                       verified_on=verified_on, now=now)
        self._validate_transport(client, contract)
        self.client, self.path, self.budget = client, Path(path), budget
        self.contracts = {"sub": contract}
        self.spent = Decimal(0)
        self.calls, self.unknown = [], False
        self.lock = asyncio.Lock()
        self.chat = SimpleNamespace(completions=self)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x", encoding="utf-8") as handle:
            json.dump(self.summary(), handle, ensure_ascii=False, indent=2)

    def summary(self):
        return {"budget_cny": str(self.budget), "accounted_upper_cny": str(self.spent),
                "usage_unknown": self.unknown,
                "price_contracts": {role: asdict(contract) for role, contract in self.contracts.items()},
                "request_count": len(self.calls), "requests": self.calls,
                "boundary": "仅本次验收；不限制同账户其他客户端；按已核验原价保守估算，不是账户账单。"}

    def _save(self):
        temporary = self.path.with_suffix(".pending")
        temporary.write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _validate_transport(client, contract):
        if getattr(client, "max_retries", None) != 0:
            raise SampleBudgetError("验收客户端必须关闭 SDK 隐式重试")
        if str(getattr(client, "base_url", "")).rstrip("/") != contract.base_url.rstrip("/"):
            raise SampleBudgetError("客户端实际服务地址与费用契约不一致")
        policy = getattr(client, "_policy", None)
        if policy is not None:
            if policy["output_limit"] != contract.output_limit:
                raise SampleBudgetError("角色输出参数与价格契约不一致")
            if set(policy["extra_body"]) - {"enable_thinking", "thinking_budget", "reasoning_effort", "thinking"}:
                raise SampleBudgetError("验收请求策略包含未核验的额外计费能力")

    def bind_client(self, client, *, model, base_url, role, price_contract):
        if role != "main" or role in self.contracts:
            raise SampleBudgetError("未知或重复验收模型角色")
        contract = PriceContract.parse(price_contract, model=model, base_url=base_url,
                                       verified_on=self.verified_on, now=self.now)
        self._validate_transport(client, contract)
        self.contracts[role] = contract
        self._save()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=partial(self._create, client, role))))

    async def create(self, **kwargs):
        return await self._create(self.client, "sub", **kwargs)

    async def _create(self, transport, role, **kwargs):
        contract = self.contracts[role]
        allowed = {"model", "messages", "temperature", "max_tokens", "max_completion_tokens", "response_format"}
        if set(kwargs) - allowed or kwargs.get("model") != contract.model:
            raise SampleBudgetError("验收不允许工具、流式、额外计费参数或模型切换")
        maxima = [kwargs[key] for key in ("max_tokens", "max_completion_tokens") if key in kwargs]
        maximum = maxima[0] if maxima else None
        messages = kwargs.get("messages")
        if type(maximum) is not int or not 1 <= maximum <= 8000 or any(item != maximum for item in maxima):
            raise SampleBudgetError("验收输出上限无效")
        if not isinstance(messages, list) or not messages or any(
            not isinstance(item, dict) or set(item) != {"role", "content"}
            or item["role"] not in {"system", "user", "assistant"}
            or not isinstance(item["content"], str) for item in messages
        ):
            raise SampleBudgetError("验收仅允许纯文本上下文")
        ceiling = maximum + contract.output_tolerance
        input_price, output_price = Decimal(contract.input_price_cny), Decimal(contract.output_price_cny)
        reserve = (contract.input_limit * input_price + ceiling * output_price) / 1_000_000
        async with self.lock:
            if self.unknown or len(self.calls) >= 24 or self.spent + reserve > self.budget:
                raise SampleBudgetError("费用/请求次数停止线或未知扣费阻断；未发出请求")
            self.spent += reserve
            record = {"number": len(self.calls) + 1, "role": role, "model": contract.model,
                      "status": "reserved", "reserved_upper_cny": str(reserve),
                      "maximum_output_tokens": ceiling}
            self.calls.append(record)
            try:
                self._save()
            except BaseException:
                self.unknown = True
                raise
            payload = dict(kwargs)
            payload.pop("max_tokens", None)
            payload.pop("max_completion_tokens", None)
            payload[contract.output_limit] = maximum
            try:
                response = await transport.chat.completions.create(**payload)
                if getattr(response, "model", "") not in contract.response_models:
                    raise SampleBudgetError("响应模型与已核验计费模型不一致")
                usage = getattr(response, "usage", None)
                prompt = getattr(usage, "prompt_tokens", None)
                output = getattr(usage, "completion_tokens", None)
                total = getattr(usage, "total_tokens", None)
                reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
                if (any(type(value) is not int for value in (prompt, output, total))
                    or not 0 < prompt <= contract.input_limit or not 0 <= output <= ceiling
                    or (reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= output))
                    or total != prompt + output):
                    raise SampleBudgetError("响应用量缺失或超出费用契约")
                actual_upper = (prompt * input_price + output * output_price) / 1_000_000
                self.spent += actual_upper - reserve
                record.update(status="confirmed", prompt_tokens=prompt, completion_tokens=output,
                              total_tokens=total, reasoning_tokens=reasoning, upper_cny=str(actual_upper))
                self._save()
                return response
            except BaseException as exc:
                if record.get("status") == "confirmed":
                    self.spent += reserve - Decimal(record["upper_cny"])
                self.unknown = True
                record["status"] = "unknown_charge"
                record["error_type"] = type(exc).__name__
                try:
                    self._save()
                finally:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise SampleBudgetError("模型请求/用量登记失败；保留费用预留并停止，不自动重试") from None
