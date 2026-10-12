"""Minimal Tavily wire contract, independent of production source imports."""

from __future__ import annotations


async def read_account_usage(client, api_key: str) -> int:
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
        raise ValueError("Invalid Tavily account usage")
    return account["plan_usage"] + account["paygo_usage"]
