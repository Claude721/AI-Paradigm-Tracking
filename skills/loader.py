"""
Skill 加载器 — 一比一复刻 AgentSkills 机制
支持标准的可复用知识模块包（Skill 包）

每个 Skill 存为一个独立文件夹，包含 SKILL.md 文件。
格式约定：
  - 文件开头是标准的 YAML Frontmatter（包含 name, description 等元数据）
  - Frontmatter 之后的正文部分是 Agent 的执行指令和模板

使用方式：
  loader = SkillLoader()
  prompt = loader.render("triage_scoring", name="foo", stars=100)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent


class SkillLoader:
    """从 skills/ 目录读取并渲染 SKILL.md"""

    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory else SKILLS_DIR
        self._cache: dict[str, str] = {}

    def load(self, name: str) -> str:
        """加载指定名称的 Skill 模板（去除 YAML Frontmatter）"""
        if name in self._cache:
            return self._cache[name]

        filepath = self.directory / name / "SKILL.md"
        if not filepath.exists():
            raise FileNotFoundError(f"Skill 文件不存在: {filepath}")

        raw = filepath.read_text(encoding="utf-8")
        template = self._extract_template(raw)
        self._cache[name] = template
        return template

    def render(self, skill_name: str, **kwargs: object) -> str:
        """加载 Skill 模板并填充变量。

        ``str.format`` 不会递归解析已经插入的参数值，因此 JSON 参数中的
        花括号无需转义。旧实现会把真实 JSON 变成 ``{{...}}``，削弱 Agent
        对证据结构的理解。
        """
        template = self.load(skill_name)
        return template.format(**kwargs)

    def reload(self, name: str | None = None) -> None:
        """清除缓存，强制重新读取文件（支持运行中热更新 Skill）"""
        if name:
            self._cache.pop(name, None)
        else:
            self._cache.clear()

    def list_skills(self) -> list[str]:
        """列出所有可用的 Skill 名称"""
        return sorted(
            p.parent.name for p in self.directory.glob("*/SKILL.md")
        )

    @staticmethod
    def _extract_template(raw: str) -> str:
        """从 Markdown 文件中提取 YAML Frontmatter 之后的正文部分"""
        if raw.startswith("---"):
            # 匹配两个 --- 之间的部分，并去除
            match = re.match(r"^---\n.*?\n---\n+", raw, re.DOTALL)
            if match:
                return raw[match.end():].strip()
        return raw.strip()
