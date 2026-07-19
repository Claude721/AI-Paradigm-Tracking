"""高优先级官方研究入口：补足无 RSS、尚未进入 arXiv 的重要发布。"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence
from paradigms.reputation import source_identity, source_link_allowed

logger = logging.getLogger(__name__)


@dataclass
class _ResearchLink:
    title: str
    url: str
    published_at: str = ""


class PriorityResearchPageSource:
    """抓取重点研究入口；召回优先级与发布者背书是两件独立的事。"""

    source_name = "priority-research-page"

    def __init__(self, lookback_days: int = 7, per_page: int | None = None):
        self.lookback_days = max(lookback_days, 1)
        configured = config.PRIORITY_RESEARCH_MAX_LINKS_PER_PAGE
        self.per_page = max(per_page if per_page is not None else configured, 1)

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not config.PRIORITY_RESEARCH_PAGES:
            return []
        try:
            return await self.fetch()
        except Exception:
            logger.exception("[priority-research-page] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        headers = {"User-Agent": "AI-Paradigm-Radar/3.1"}
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers=headers
        ) as client:
            index_responses = await asyncio.gather(
                *(client.get(url) for url in config.PRIORITY_RESEARCH_PAGES),
                return_exceptions=True,
            )
            discovered: list[tuple[str, _ResearchLink]] = []
            for index_url, response in zip(
                config.PRIORITY_RESEARCH_PAGES, index_responses
            ):
                if isinstance(response, Exception):
                    logger.warning("官方研究入口读取失败 %s: %s", index_url, response)
                    continue
                try:
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning("官方研究入口返回失败 %s: %s", index_url, exc)
                    continue
                links = _discover_index_links(response.text, str(response.url))
                allowed_links = [
                    link for link in links if source_link_allowed(index_url, link.url)
                ]
                for link in allowed_links[: self.per_page]:
                    discovered.append((index_url, link))

            detail_responses = await asyncio.gather(
                *(client.get(link.url) for _, link in discovered),
                return_exceptions=True,
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        results: list[TechnicalEvidence] = []
        for (index_url, link), response in zip(discovered, detail_responses):
            if isinstance(response, Exception):
                continue
            try:
                response.raise_for_status()
            except Exception:
                continue
            if _is_pdf_response(response, link.url):
                try:
                    title, body, authors = _extract_pdf_document(response.content)
                except Exception as exc:
                    logger.warning("官方 Technical Report PDF 解析失败 %s: %s", link.url, exc)
                    continue
                site_name = ""
                published = link.published_at
            else:
                article = _ArticleParser()
                article.feed(response.text)
                title = article.title
                body = _compact_text(article.text)
                authors = article.authors
                site_name = article.site_name
                published = article.published_at or link.published_at
            published_dt = _parse_date(published)
            if published_dt and published_dt < cutoff:
                continue
            title = title or link.title
            if not title or len(body) < 120:
                continue
            # PDF 元数据标题有时只写模型名；保留索引页锚文本，避免把明确标注的
            # “Technical Report”误判成普通模型发布。
            origin_kind = _origin_kind(f"{title} {link.title}", link.url)
            host = urlparse(index_url).netloc
            source_meta, owner, publisher_tier = source_identity(index_url)
            owner_name = str(owner.get("name", "")) if owner else ""
            if source_meta:
                publisher_evidence = f"内置研究入口（{publisher_tier}）：{index_url}"
            else:
                publisher_evidence = f"用户追加的重点入口，尚无内置 owner 核验：{index_url}"
            summary_limit = 60_000 if origin_kind == "technical_report" else 16_000
            results.append(
                TechnicalEvidence(
                    source=self.source_name,
                    evidence_type=EvidenceType.TECHNICAL_BLOG,
                    title=title,
                    url=str(response.url),
                    summary=body[:summary_limit],
                    published_at=(published_dt.isoformat() if published_dt else published),
                    authors=list(dict.fromkeys(authors)),
                    # 内置入口使用已核验 owner；用户自定义入口只保留域名，避免
                    # 网页伪造 og:site_name 后继承知名机构身份。
                    organization=owner_name or host,
                    raw={
                        "origin_kind": origin_kind,
                        "origin_priority": 3 if origin_kind == "technical_report" else 2,
                        "publisher_tier": publisher_tier,
                        "publisher_evidence": publisher_evidence,
                        "research_index_url": index_url,
                        "source_owner_id": str(source_meta.get("owner", "")) if source_meta else "",
                    },
                )
            )
        return list({item.fingerprint: item for item in results}.values())


class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        self._href = dict(attrs).get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href:
            self.anchors.append((_compact_text(" ".join(self._text)), self._href))
            self._href = ""
            self._text = []


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.published_at = ""
        self.authors: list[str] = []
        self.site_name = ""
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    @property
    def text(self) -> str:
        return " ".join(self._text_parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if lowered in {"script", "style", "svg", "nav", "footer"}:
            self._ignored_depth += 1
        if lowered == "title":
            self._in_title = True
        if lowered == "time" and attributes.get("datetime"):
            self.published_at = self.published_at or attributes["datetime"]
        if lowered == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").casefold()
            if key in {"article:published_time", "date", "datepublished"}:
                self.published_at = self.published_at or attributes.get("content", "")
            if key in {"author", "citation_author"}:
                author = _compact_text(attributes.get("content", ""))
                if author and not author.startswith(("http://", "https://")):
                    self.authors.append(author)
            if key == "og:site_name":
                self.site_name = self.site_name or _compact_text(
                    attributes.get("content", "")
                )

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "svg", "nav", "footer"} and self._ignored_depth:
            self._ignored_depth -= 1
        if lowered == "title":
            self._in_title = False
            self.title = _compact_text(" ".join(self._title_parts)).split(" | ", 1)[0]

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if not self._ignored_depth and data.strip():
            self._text_parts.append(data.strip())


def _discover_index_links(html_text: str, base_url: str) -> list[_ResearchLink]:
    parser = _AnchorParser()
    parser.feed(html_text)
    by_url: dict[str, _ResearchLink] = {}
    for title, href in parser.anchors:
        url = urljoin(base_url, href)
        if not _looks_like_research_link(title, url):
            continue
        date = _date_from_text(title)
        cleaned_title = re.sub(r"^\s*\d{4}-\d{2}-\d{2}\s*", "", title).strip()
        by_url[url.rstrip("/")] = _ResearchLink(
            title=cleaned_title or title,
            url=url,
            published_at=date,
        )
    return sorted(
        by_url.values(),
        key=lambda item: (bool(item.published_at), item.published_at),
        reverse=True,
    )


def _looks_like_research_link(title: str, url: str) -> bool:
    if not title or len(title) < 5 or not url.startswith(("http://", "https://")):
        return False
    lowered_title = title.casefold()
    lowered_path = urlparse(url).path.casefold()
    if lowered_title.strip() in {"research", "blog", "news", "get more", "learn more"}:
        return False
    title_signal = bool(
        re.search(r"\d{4}-\d{2}-\d{2}", title)
        or any(
            value in lowered_title
            for value in (
                "technical report",
                "introducing",
                "model",
                "architecture",
                "reasoning",
                "agent",
                "multimodal",
                "world",
                "技术报告",
                "研究报告",
                "模型",
                "架构",
                "推理",
                "智能体",
                "多模态",
                "世界模型",
                "具身",
                "机器人",
            )
        )
    )
    path_signal = any(
        marker in lowered_path
        for marker in (
            "/blog/",
            "/research/",
            "/publication",
            "/paper",
            "/report",
            "/news/",
            "/article/",
            "/detail/",
            "/achievement/",
            ".pdf",
        )
    )
    publication_path = any(
        marker in lowered_path
        for marker in ("/publication/", "/publications/", "/paper/", "/report/", ".pdf")
    )
    return path_signal and (title_signal or publication_path)


def _origin_kind(title: str, url: str) -> str:
    lowered = f"{title} {url}".casefold()
    if any(value in lowered for value in ("technical report", "tech-report", "技术报告", "系统报告")):
        return "technical_report"
    if any(value in lowered for value in ("introducing", "release", "model", "模型发布", "发布", "/blog/kimi-")):
        return "official_model_release"
    return "official_research"


def _date_from_text(value: str) -> str:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", value)
    return match.group(1) if match else ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _is_pdf_response(response: httpx.Response, url: str) -> bool:
    content_type = response.headers.get("content-type", "").casefold()
    return "application/pdf" in content_type or urlparse(url).path.casefold().endswith(
        ".pdf"
    )


def _extract_pdf_document(content: bytes) -> tuple[str, str, list[str]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    metadata = reader.metadata
    title = _compact_text(str(getattr(metadata, "title", "") or ""))
    author_text = _compact_text(str(getattr(metadata, "author", "") or ""))
    authors = [
        value.strip()
        for value in re.split(r"\s*(?:;|\band\b)\s*", author_text)
        if value.strip()
    ]
    text_parts: list[str] = []
    text_length = 0
    for page in reader.pages[:100]:
        value = page.extract_text() or ""
        if not value:
            continue
        text_parts.append(value)
        text_length += len(value)
        if text_length >= 80_000:
            break
    return title, _compact_text("\n".join(text_parts))[:80_000], authors
