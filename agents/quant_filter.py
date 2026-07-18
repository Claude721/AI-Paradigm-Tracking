"""
QuantFilter - LLM 之前的定量预过滤层

目标：
1) 先用结构化信号（新鲜度/势能/互动）淘汰明显低价值样本
2) 将有限的 LLM 预算留给更可能有 VC 价值的项目
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import config
from sources.base import RawProject

logger = logging.getLogger(__name__)

APP_KEYWORDS = [
    "medical", "health", "autonomous", "robot", "finance", "education",
    "enterprise", "workflow", "e-commerce", "video", "image", "design",
    "assistant", "agent", "saas", "consumer", "product", "application",
    "b2b", "b2c", "crm", "erp", "marketing", "sales", "game", 
    "entertainment", "social", "creator", "productivity", "app", "platform",
]

OPT_KEYWORDS = [
    "benchmark", "latency", "throughput", "quantization", "distillation",
    "compression", "memory", "attention", "loss function", "training efficiency",
    "eval", "evaluation", "survey", "comprehensive study", "literature review", 
    "ablation study", "optimization", "parameter-efficient", "peft", "lora",
]


class QuantFilter:
    """基于可量化信号的规则过滤器"""

    def __init__(self) -> None:
        self.enabled = config.QUANT_FILTER_ENABLED
        self.min_score = config.QUANT_MIN_SCORE
        self.max_candidates = config.QUANT_MAX_CANDIDATES
        self.min_text_len = config.QUANT_MIN_TEXT_LEN

        self.w_fresh = config.QUANT_W_FRESHNESS
        self.w_momentum = config.QUANT_W_MOMENTUM
        self.w_engagement = config.QUANT_W_ENGAGEMENT
        self.source_min = {
            "github": config.QUANT_SRC_MIN_GITHUB,
            "github-trending": config.QUANT_SRC_MIN_GITHUB_TRENDING,
            "huggingface-model": config.QUANT_SRC_MIN_HUGGINGFACE_MODEL,
            "huggingface-space": config.QUANT_SRC_MIN_HUGGINGFACE_SPACE,
            "huggingface-papers": config.QUANT_SRC_MIN_HUGGINGFACE_PAPERS,
            "arxiv": config.QUANT_SRC_MIN_ARXIV,
            "hackernews": config.QUANT_SRC_MIN_HACKERNEWS,
            "producthunt": config.QUANT_SRC_MIN_PRODUCTHUNT,
            "twitter": config.QUANT_SRC_MIN_TWITTER,
            "wechat": config.QUANT_SRC_MIN_WECHAT,
            "follow-builders-x": config.QUANT_SRC_MIN_FOLLOW_BUILDERS_X,
            "follow-builders-podcast": config.QUANT_SRC_MIN_FOLLOW_BUILDERS_PODCAST,
            "follow-builders-blog": config.QUANT_SRC_MIN_FOLLOW_BUILDERS_BLOG,
        }
        self.source_cap = {
            "github": config.QUANT_SRC_MAX_GITHUB,
            "github-trending": config.QUANT_SRC_MAX_GITHUB_TRENDING,
            "huggingface-model": config.QUANT_SRC_MAX_HUGGINGFACE_MODEL,
            "huggingface-space": config.QUANT_SRC_MAX_HUGGINGFACE_SPACE,
            "huggingface-papers": config.QUANT_SRC_MAX_HUGGINGFACE_PAPERS,
            "arxiv": config.QUANT_SRC_MAX_ARXIV,
            "hackernews": config.QUANT_SRC_MAX_HACKERNEWS,
            "producthunt": config.QUANT_SRC_MAX_PRODUCTHUNT,
            "twitter": config.QUANT_SRC_MAX_TWITTER,
            "wechat": config.QUANT_SRC_MAX_WECHAT,
            "follow-builders-x": config.QUANT_SRC_MAX_FOLLOW_BUILDERS_X,
            "follow-builders-podcast": config.QUANT_SRC_MAX_FOLLOW_BUILDERS_PODCAST,
            "follow-builders-blog": config.QUANT_SRC_MAX_FOLLOW_BUILDERS_BLOG,
        }
        self.source_bias = {
            "github": config.QUANT_BIAS_GITHUB,
            "github-trending": config.QUANT_BIAS_GITHUB_TRENDING,
            "huggingface-model": config.QUANT_BIAS_HUGGINGFACE_MODEL,
            "huggingface-space": config.QUANT_BIAS_HUGGINGFACE_SPACE,
            "huggingface-papers": config.QUANT_BIAS_HUGGINGFACE_PAPERS,
            "arxiv": config.QUANT_BIAS_ARXIV,
            "hackernews": config.QUANT_BIAS_HACKERNEWS,
            "producthunt": config.QUANT_BIAS_PRODUCTHUNT,
            "twitter": config.QUANT_BIAS_TWITTER,
            "wechat": config.QUANT_BIAS_WECHAT,
            "follow-builders-x": config.QUANT_BIAS_FOLLOW_BUILDERS_X,
            "follow-builders-podcast": config.QUANT_BIAS_FOLLOW_BUILDERS_PODCAST,
            "follow-builders-blog": config.QUANT_BIAS_FOLLOW_BUILDERS_BLOG,
        }

    def run(self, projects: list[RawProject]) -> list[RawProject]:
        if not self.enabled:
            logger.info("QuantFilter 已禁用，跳过定量预过滤")
            for p in projects:
                p.extra["project_type"] = self._determine_project_type(p)
            return projects
        if not projects:
            return []

        scored: list[tuple[float, RawProject]] = []
        dropped = 0
        for p in projects:
            p.extra["project_type"] = self._determine_project_type(p)
            
            fresh = self._freshness_score(p)
            mom = self._momentum_score(p)
            eng = self._engagement_score(p)
            score = self._blend_score(p, fresh, mom, eng)

            p.extra["quant_score"] = round(score, 1)
            p.extra["quant_freshness"] = round(fresh, 1)
            p.extra["quant_momentum"] = round(mom, 1)
            p.extra["quant_engagement"] = round(eng, 1)

            if not self._hard_pass(p):
                dropped += 1
                continue
            threshold = self.source_min.get(p.source, self.min_score)
            if score < threshold:
                dropped += 1
                continue

            scored.append((score, p))

        kept = self._apply_source_caps(scored)

        if self.max_candidates > 0 and len(kept) > self.max_candidates:
            kept = kept[: self.max_candidates]

        logger.info(
            "QuantFilter 完成: 保留 %s/%s（淘汰 %s，阈值=%.1f，Top-K=%s）",
            len(kept),
            len(projects),
            dropped,
            self.min_score,
            self.max_candidates,
        )
        self._log_source_distribution(kept)
        return kept

    def _determine_project_type(self, p: RawProject) -> str:
        text = f"{p.name} {p.description} {p.readme_summary}".lower()
        if p.source in {
            "producthunt", "hackernews", "wechat",
            "follow-builders-x", "follow-builders-podcast", "follow-builders-blog",
        }:
            return "app"
        
        # 对于 Github / HF / Arxiv 等，根据关键字判断
        if any(k in text for k in APP_KEYWORDS):
            return "app"
        
        return "tech"

    def _blend_score(self, p: RawProject, freshness: float, momentum: float, engagement: float) -> float:
        score = (
            self.w_fresh * freshness
            + self.w_momentum * momentum
            + self.w_engagement * engagement
        )

        text = f"{p.name} {p.description} {p.readme_summary}".lower()
        if any(k in text for k in APP_KEYWORDS):
            score += 12  # 提升应用类项目的加分
        
        # 惩罚所有包含过多优化/学术词汇的项目（降低技术尝试的入库率）
        if sum(1 for k in OPT_KEYWORDS if k in text) >= 2 or ("paper" in p.source or p.source == "arxiv"):
            if any(k in text for k in OPT_KEYWORDS):
                score -= 15  # 加大技术尝试/学术跑分论文的扣分力度
                
        score += self.source_bias.get(p.source, 0.0)

        return max(0.0, min(100.0, score))

    def _apply_source_caps(self, scored: list[tuple[float, RawProject]]) -> list[RawProject]:
        """按信源配额做一次均衡采样，避免单一来源挤占 LLM 预算"""
        by_source: dict[str, list[tuple[float, RawProject]]] = {}
        for s, p in scored:
            by_source.setdefault(p.source, []).append((s, p))

        for src in by_source:
            by_source[src].sort(key=lambda x: x[0], reverse=True)

        kept: list[RawProject] = []
        for src, items in by_source.items():
            cap = self.source_cap.get(src, 0)
            if cap and cap > 0:
                selected = items[:cap]
            else:
                selected = items
            kept.extend([p for _, p in selected])

        # 最终按 quant 分降序
        kept.sort(key=lambda p: float(p.extra.get("quant_score", 0)), reverse=True)
        return kept

    def _hard_pass(self, p: RawProject) -> bool:
        text_len = len((p.description or "").strip()) + len((p.readme_summary or "").strip())
        if text_len < self.min_text_len and p.stars < 10:
            return False

        if p.source in {"arxiv", "huggingface-papers"}:
            text = f"{p.name} {p.description} {p.readme_summary}".lower()
            has_app = any(k in text for k in APP_KEYWORDS)
            if p.stars < 15 and not has_app:
                return False

        # 排除太老的项目（超过 1 年），除非近期势能极高（老树发新芽机制）
        created_dt = self._parse_datetime(p.created_at)
        if created_dt:
            created_days = max((datetime.now(timezone.utc) - created_dt).total_seconds() / (3600 * 24), 1.0)
            if created_days > 180: # 缩紧至半年，只要半年以上没有真实势能就淘汰
                stars_period = self._safe_float(p.extra.get("stars_period", 0))
                real_added = self._safe_float(p.extra.get("real_recent_added_stars", 0))
                # 如果是半年以上的老项目，必须有很强的近期势能指标（历史平均不再奏效，必须看真实近期数据）
                if stars_period < 30 and real_added < 10:
                    return False

        return True

    def _freshness_score(self, p: RawProject) -> float:
        dt = self._best_datetime(p)
        if not dt:
            return 35.0

        age_hours = max((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 0)
        if age_hours <= 24:
            score = 100.0
        elif age_hours <= 72:
            score = 86.0
        elif age_hours <= 7 * 24:
            score = 72.0
        elif age_hours <= 14 * 24:
            score = 56.0
        elif age_hours <= 30 * 24:
            score = 40.0
        else:
            score = 22.0

        # 新增项目年龄惩罚（基于 created_at），降低老项目的“伪新鲜度”
        created_dt = self._parse_datetime(p.created_at)
        if created_dt:
            created_days = max((datetime.now(timezone.utc) - created_dt).total_seconds() / (3600 * 24), 0)
            if created_days > 180:
                score *= 0.2
            elif created_days > 90:
                score *= 0.5
            elif created_days > 30:
                score *= 0.8

        return score

    def _momentum_score(self, p: RawProject) -> float:
        stars_period = self._safe_float(p.extra.get("stars_period", 0))
        downloads = self._safe_float(p.extra.get("downloads", 0))
        likes = self._safe_float(p.stars)

        if stars_period > 0:
            return min(100.0, 24 * math.log10(stars_period + 1))
        if downloads > 0:
            return min(100.0, 18 * math.log10(downloads + 1))
            
        created_dt = self._parse_datetime(p.created_at)
        if created_dt:
            created_days = max((datetime.now(timezone.utc) - created_dt).total_seconds() / (3600 * 24), 1.0)
            velocity = likes / created_days
            return min(100.0, 40 * math.log10(velocity + 1))
            
        return min(100.0, 20 * math.log10(likes + 1))

    def _engagement_score(self, p: RawProject) -> float:
        hotness = self._safe_float(p.stars)

        if p.source == "github":
            return min(100.0, 18 * math.log10(hotness + 1))
        if p.source == "github-trending":
            return min(100.0, 22 * math.log10(hotness + 1))
        if p.source in {"huggingface-model", "huggingface-space"}:
            return min(100.0, 20 * math.log10(hotness + 1))
        if p.source in {"hackernews", "producthunt"}:
            return min(100.0, 28 * math.log10(hotness + 1))
        if p.source in {"arxiv", "huggingface-papers"}:
            return min(100.0, 24 * math.log10(hotness + 1))
        if p.source.startswith("follow-builders"):
            return min(100.0, 30 * math.log10(hotness + 1)) if hotness > 0 else 50.0
        return min(100.0, 20 * math.log10(hotness + 1))

    def _best_datetime(self, p: RawProject) -> datetime | None:
        candidates = [
            p.extra.get("last_modified", ""),
            p.extra.get("pushed_at", ""),
            p.extra.get("updated_at", ""),
            p.created_at,
        ]
        for v in candidates:
            dt = self._parse_datetime(v)
            if dt:
                return dt
        return None

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            if value <= 0:
                return None
            return datetime.fromtimestamp(float(value), tz=timezone.utc)

        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc)

        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _safe_float(value: object) -> float:
        try:
            return float(value or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _log_source_distribution(projects: list[RawProject]) -> None:
        by_source: dict[str, int] = {}
        for p in projects:
            by_source[p.source] = by_source.get(p.source, 0) + 1
        summary = ", ".join(f"{k}:{v}" for k, v in sorted(by_source.items()))
        logger.info("QuantFilter 来源分布: %s", summary)
