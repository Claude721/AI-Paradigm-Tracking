"""
信源基类 - 统一接口定义
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class RawProject:
    """从信源获取的原始项目数据"""

    source: str  # github / huggingface / arxiv / hackernews
    name: str
    url: str
    description: str = ""
    readme_summary: str = ""
    stars: int = 0
    language: str = ""
    topics: list[str] = field(default_factory=list)
    author: str = ""
    created_at: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return f"[{self.source}] {self.name}"


class BaseSource(ABC):
    """所有信源的抽象基类"""

    source_name: str = "unknown"

    @abstractmethod
    async def fetch(self) -> list[RawProject]:
        """拉取项目列表，子类必须实现"""
        ...

    async def safe_fetch(self) -> list[RawProject]:
        """带异常保护的 fetch wrapper，单个信源失败不影响全局"""
        try:
            results = await self.fetch()
            logger.info(f"[{self.source_name}] 成功获取 {len(results)} 个项目")
            return results
        except Exception:
            logger.exception(f"[{self.source_name}] 获取失败")
            return []
