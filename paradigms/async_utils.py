"""Bound the lifetime of concurrent calls to the stage that owns them."""
from __future__ import annotations

import asyncio


async def gather_scoped(*awaitables):
    tasks = [asyncio.create_task(value) for value in awaitables]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
