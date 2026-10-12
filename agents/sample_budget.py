"""Fail-closed monetary admission for an explicitly authorized one-route check.

Not a production/account budget. Only the exact, verified Beijing Qwen service
is supported; proxy prices, model migrations and unverified future prices fail.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

MODEL = "qwen3.7-plus"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PRICE_DATE = date(2026, 10, 12)
PRICE_URL = "https://help.aliyun.com/zh/model-studio/model-pricing"
CONTRACT_URL = "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions"
INPUT_LIMIT = 1_000_000  # Whole published context, not a tokenizer estimate.
INPUT_PRICE = Decimal("6")  # Highest Beijing tier, no discounts/cache assumptions.
OUTPUT_PRICE = Decimal("24")


class SampleBudgetError(RuntimeError):
    pass


class SampleBudgetClient:
    """Inject this client into EVERY sampled production model stage.

    Serialize calls, journal full worst-case reservation BEFORE I/O, settle only
    valid provider usage. Unknown charge/cancellation stops all further calls.
    max_completion_tokens covers reasoning AND visible output (+10 tolerance).
    Existing journals are never reset or resumed by this one-shot test.
    """
    def __init__(self, client, *, path: Path, budget_cny, verified_on: date,
                 model=MODEL, base_url=BASE_URL, now: date | None = None):
        try:
            budget = Decimal(str(budget_cny))
        except Exception as exc:
            raise SampleBudgetError("费用上限无效") from exc
        if not budget.is_finite() or not 0 < budget <= 20:
            raise SampleBudgetError("单路线验收费用必须在 0 到 20 元内")
        if model != MODEL or base_url.rstrip("/") != BASE_URL:
            raise SampleBudgetError("实际模型/地域没有匹配的已核验价格")
        if verified_on != PRICE_DATE or (now or datetime.now(timezone.utc).date()) != PRICE_DATE:
            raise SampleBudgetError("价格核验已过期；重新核验官方价格后才能运行")
        if getattr(client, "max_retries", None) != 0:
            raise SampleBudgetError("验收客户端必须关闭 SDK 隐式重试")
        if str(getattr(client, "base_url", "")).rstrip("/") != BASE_URL:
            raise SampleBudgetError("客户端实际服务地址与费用契约不一致")
        self.client, self.path, self.budget = client, Path(path), budget
        self.spent = Decimal(0)
        self.calls, self.unknown = [], False
        self.lock = asyncio.Lock()
        self.chat = SimpleNamespace(completions=self)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents an accidental second process/test retry
        # from reusing an authorization while losing the prior reservation.
        with self.path.open("x", encoding="utf-8") as handle:
            json.dump(self.summary(), handle, ensure_ascii=False, indent=2)

    def summary(self):
        return {"budget_cny": str(self.budget), "accounted_upper_cny": str(self.spent),
                "usage_unknown": self.unknown, "model": MODEL, "region": "cn-beijing",
                "price_verified_on": PRICE_DATE.isoformat(), "pricing_url": PRICE_URL,
                "contract_url": CONTRACT_URL, "request_count": len(self.calls),
                "requests": self.calls,
                "boundary": "仅本次单路线；不限制同账户其他客户端；按官方最高阶梯保守估算，不是账户账单。"}

    def _save(self):
        temporary = self.path.with_suffix(".pending")
        temporary.write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    async def create(self, **kwargs):
        allowed = {"model", "messages", "temperature", "max_tokens", "response_format"}
        if set(kwargs) - allowed or kwargs.get("model") != MODEL:
            raise SampleBudgetError("验收不允许工具、流式、额外计费参数或模型切换")
        maximum = kwargs.get("max_tokens")
        messages = kwargs.get("messages")
        if type(maximum) is not int or not 1 <= maximum <= 8000:
            raise SampleBudgetError("验收输出上限无效")
        if not isinstance(messages, list) or not messages or any(
            not isinstance(item, dict) or set(item) != {"role", "content"}
            or item["role"] not in {"system", "user", "assistant"}
            or not isinstance(item["content"], str) for item in messages
        ):
            raise SampleBudgetError("验收仅允许纯文本上下文")
        # No UTF-8/tokenizer heuristic: reserve the WHOLE model input allowance.
        reserve = (INPUT_LIMIT * INPUT_PRICE + (maximum + 10) * OUTPUT_PRICE) / 1_000_000
        async with self.lock:
            if self.unknown or len(self.calls) >= 24 or self.spent + reserve > self.budget:
                raise SampleBudgetError("费用/请求次数停止线或未知扣费阻断；未发出请求")
            self.spent += reserve
            record = {"number": len(self.calls) + 1, "status": "reserved",
                      "reserved_upper_cny": str(reserve), "maximum_output_tokens": maximum + 10}
            self.calls.append(record)
            try:
                self._save()
            except BaseException:
                self.unknown = True
                raise  # No request if reservation cannot be saved.
            payload = dict(kwargs)
            payload.pop("max_tokens")
            payload["max_completion_tokens"] = maximum
            try:
                response = await self.client.chat.completions.create(**payload)
                if getattr(response, "model", "") not in {MODEL, "qwen3.7-plus-2026-05-26"}:
                    raise SampleBudgetError("响应模型与已核验计费模型不一致")
                usage = getattr(response, "usage", None)
                prompt = getattr(usage, "prompt_tokens", None)
                output = getattr(usage, "completion_tokens", None)
                total = getattr(usage, "total_tokens", None)
                reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
                if (any(type(value) is not int for value in (prompt, output, total, reasoning))
                    or not 0 < prompt <= INPUT_LIMIT or not 0 <= output <= maximum + 10
                    or not 0 <= reasoning <= output or total != prompt + output):
                    raise SampleBudgetError("响应用量缺失或超出费用契约")
                actual_upper = (prompt * INPUT_PRICE + output * OUTPUT_PRICE) / 1_000_000
                self.spent += actual_upper - reserve
                record.update(status="confirmed", prompt_tokens=prompt, completion_tokens=output,
                              total_tokens=total, reasoning_tokens=reasoning, upper_cny=str(actual_upper))
                self._save()
                return response
            except BaseException as exc:
                # A timeout is NOT a refund. If final journal save failed after
                # a response, retain the entire reservation rather than reuse it.
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
