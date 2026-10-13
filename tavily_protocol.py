"""Minimal Tavily wire contract, independent of production source imports."""

from __future__ import annotations


class TavilyIncludedCreditUnavailable(ValueError):
    """A fixed public policy reason, not an authentication failure."""
    def __init__(self, reason):
        if reason not in {"account_usage_unknown", "plan_limit_unknown", "plan_credits_exhausted",
                "payg_limit_unknown", "payg_enabled", "key_limit_unknown", "key_credits_exhausted"}:
            raise ValueError("Unknown included-credit guard reason")
        self.reason_code = "tavily_included_" + reason
        super().__init__(self.reason_code)


async def read_account_usage(client, api_key: str, *, require_included_credit=False) -> int:
    """Read account credits without a paid search or following redirects."""
    response = await client.get(
        "https://api.tavily.com/usage",
        headers={"Authorization": f"Bearer {api_key}"},
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    account = payload.get("account") if isinstance(payload, dict) else None
    if (
        not isinstance(account, dict)
        or any(
            type(account.get(key)) is not int or account[key] < 0
            for key in ("plan_usage", "paygo_usage")
        )
    ):
        if require_included_credit:
            raise TavilyIncludedCreditUnavailable("account_usage_unknown")
        raise ValueError("Invalid Tavily account usage")
    if require_included_credit:
        # A bounded acceptance may authorize existing credits but not new
        # PAYG charges. Unknown limits must stop BEFORE the search, including
        # a key-specific limit which can be lower than the account allowance.
        key = payload.get("key")
        if type(account.get("plan_limit")) is not int or account["plan_limit"] < 0:
            raise TavilyIncludedCreditUnavailable("plan_limit_unknown")
        if account["plan_limit"] - account["plan_usage"] < 1:
            raise TavilyIncludedCreditUnavailable("plan_credits_exhausted")
        if type(account.get("paygo_limit")) is not int or account["paygo_limit"] < 0:
            raise TavilyIncludedCreditUnavailable("payg_limit_unknown")
        if account["paygo_limit"] != 0:
            raise TavilyIncludedCreditUnavailable("payg_enabled")
        if (not isinstance(key, dict)
                or any(type(key.get(name)) is not int or key[name] < 0 for name in ("usage", "limit"))):
            raise TavilyIncludedCreditUnavailable("key_limit_unknown")
        if key["limit"] - key["usage"] < 1:
            raise TavilyIncludedCreditUnavailable("key_credits_exhausted")
    return account["plan_usage"] + account["paygo_usage"]
