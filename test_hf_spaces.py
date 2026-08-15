"""Manual legacy Hugging Face Spaces probe; never run during test import."""

import asyncio

import httpx


async def main() -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            "https://huggingface.co/api/spaces",
            params={"sort": "trendingScore", "direction": "-1", "limit": 5}
        )
        print("spaces trendingScore:", resp.status_code)
        resp.raise_for_status()
        for item in resp.json():
            print(item.get("id"), item.get("likes"))


if __name__ == "__main__":
    asyncio.run(main())
