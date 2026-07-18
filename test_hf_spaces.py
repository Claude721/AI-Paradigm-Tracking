import httpx
import asyncio

async def test():
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://huggingface.co/api/spaces",
            params={"sort": "trendingScore", "direction": "-1", "limit": 5}
        )
        print("spaces trendingScore:", resp.status_code)
        if resp.status_code == 200:
            for item in resp.json():
                print(item.get('id'), item.get('likes'))

asyncio.run(test())
