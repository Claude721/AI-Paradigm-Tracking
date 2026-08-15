"""Manual legacy Hugging Face model probe; never run during test import."""

import asyncio

import httpx


async def main() -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            "https://huggingface.co/api/models",
            params={"sort": "trendingScore", "direction": "-1", "limit": 5, "filter": "text-generation"}
        )
        print("trendingScore:", resp.status_code)
        resp.raise_for_status()
        for item in resp.json():
            print(item.get("id"), item.get("likes"))
        
        resp2 = await client.get(
            "https://huggingface.co/api/models",
            params={"sort": "likes", "direction": "-1", "limit": 5, "filter": "text-generation"}
        )
        print("likes:", resp2.status_code)
        resp2.raise_for_status()


if __name__ == "__main__":
    asyncio.run(main())
