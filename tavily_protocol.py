"""Minimal Tavily wire contract, independent of production source imports."""

from __future__ import annotations


class TavilyIncludedCreditUnavailable(ValueError):
    """A fixed public policy reason, not an authentication failure."""
    def __init__(self, reason, *, contract_shape=None):
        if reason not in {"account_usage_unknown", "plan_limit_unknown", "plan_credits_exhausted",
                "payg_limit_unknown", "payg_enabled", "key_limit_unknown", "key_credits_exhausted"}:
            raise ValueError("Unknown included-credit guard reason")
        fields = {"account.plan_usage", "account.plan_limit", "account.paygo_usage",
                  "account.paygo_limit", "key.usage", "key.limit"}
        labels = {"missing", "null", "boolean", "integer", "number", "string", "array", "object", "other"}
        if contract_shape is not None and (not isinstance(contract_shape, dict)
                or not set(contract_shape).issubset(fields)
                or any(not isinstance(value, str) or value not in labels for value in contract_shape.values())):
            raise ValueError("Invalid included-credit contract shape")
        self.reason_code = "tavily_included_" + reason
        self.contract_shape = dict(contract_shape or {})
        super().__init__(self.reason_code)


def usage_contract_shape(payload):
    """Only fixed field names/type labels; never account values or text."""
    def shape(container, field):
        if not isinstance(container, dict) or field not in container:
            return "missing"
        value = container[field]
        if value is None:
            return "null"
        return {bool: "boolean", int: "integer", float: "number", str: "string",
                list: "array", dict: "object"}.get(type(value), "other")
    account = payload.get("account") if isinstance(payload, dict) else None
    key = payload.get("key") if isinstance(payload, dict) else None
    return {**{"account." + name: shape(account, name) for name in
               ("plan_usage", "plan_limit", "paygo_usage", "paygo_limit")},
            **{"key." + name: shape(key, name) for name in ("usage", "limit")}}


async def read_account_usage(client, api_key: str, *, require_included_credit=False) -> int:
    """Read account credits without a paid search or following redirects."""
    response = await client.get(
        "https://api.tavily.com/usage",
        headers={"Authorization": f"Bearer {api_key}"},
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    def reject(reason):
        raise TavilyIncludedCreditUnavailable(reason, contract_shape=usage_contract_shape(payload))
    account = payload.get("account") if isinstance(payload, dict) else None
    if (
        not isinstance(account, dict)
        or any(
            type(account.get(key)) is not int or account[key] < 0
            for key in ("plan_usage", "paygo_usage")
        )
    ):
        if require_included_credit:
            reject("account_usage_unknown")
        raise ValueError("Invalid Tavily account usage")
    if require_included_credit:
        # A bounded acceptance may authorize existing credits but not new
        # PAYG charges. Unknown limits must stop BEFORE the search, including
        # a key-specific limit which can be lower than the account allowance.
        key = payload.get("key")
        if type(account.get("plan_limit")) is not int or account["plan_limit"] < 0:
            reject("plan_limit_unknown")
        if account["plan_limit"] - account["plan_usage"] < 1:
            reject("plan_credits_exhausted")
        if type(account.get("paygo_limit")) is not int or account["paygo_limit"] < 0:
            reject("payg_limit_unknown")
        if account["paygo_limit"] != 0:
            reject("payg_enabled")
        if (not isinstance(key, dict)
                or any(type(key.get(name)) is not int or key[name] < 0 for name in ("usage", "limit"))):
            reject("key_limit_unknown")
        if key["limit"] - key["usage"] < 1:
            reject("key_credits_exhausted")
    return account["plan_usage"] + account["paygo_usage"]
