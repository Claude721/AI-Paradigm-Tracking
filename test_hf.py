import httpx
import asyncio

async def test():
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://huggingface.co/api/models",
            params={"sort": "trendingScore", "direction": "-1", "limit": 5, "filter": "text-generation"}
        )
        print("trendingScore:", resp.status_code)
        if resp.status_code == 200:
            for item in resp.json():
                print(item.get('id'), item.get('likes'))
        
        resp2 = await client.get(
            "https://huggingface.co/api/models",
            params={"sort": "likes", "direction": "-1", "limit": 5, "filter": "text-generation"}
        )
        print("likes:", resp2.status_code)

asyncio.run(test())
