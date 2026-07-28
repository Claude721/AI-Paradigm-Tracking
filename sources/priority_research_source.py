"""高优先级官方研究入口：补足无 RSS、尚未进入 arXiv 的重要发布。"""

from __future__ import annotations

import asyncio
import html as html_lib
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence
from paradigms.publication import (
    classify_publication,
    looks_like_linked_research_document,
)
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

    def __init__(
        self,
        lookback_days: int = 7,
        per_page: int | None = None,
        pages: list[str] | None = None,
        concurrency: int | None = None,
    ):
        self.lookback_days = max(lookback_days, 1)
        configured = config.PRIORITY_RESEARCH_LINK_SAFETY_LIMIT
        self.per_page = max(
            per_page if per_page is not None else configured, 0
        )
        self.pages = config.PRIORITY_RESEARCH_PAGES if pages is None else pages
        self.concurrency = max(
            concurrency
            if concurrency is not None
            else config.PRIORITY_RESEARCH_CONCURRENCY,
            1,
        )
        self.page_coverage: dict[str, dict[str, object]] = {}

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not self.pages:
            return []
        try:
            return await self.fetch()
        except Exception:
            logger.exception("[priority-research-page] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        headers = {"User-Agent": "AI-Paradigm-Radar/3.1"}
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def bounded_get(client: httpx.AsyncClient, url: str):
            async with semaphore:
                return await client.get(url)

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers=headers
        ) as client:
            index_responses = await asyncio.gather(
                *(bounded_get(client, url) for url in self.pages),
                return_exceptions=True,
            )
            discovered: list[tuple[str, _ResearchLink]] = []
            for index_url, response in zip(
                self.pages, index_responses
            ):
                if isinstance(response, Exception):
                    self.page_coverage[index_url] = {
                        "status": "request_failed",
                        "error": type(response).__name__,
                        "discovered_links": 0,
                        "evidence": 0,
                    }
                    logger.warning("官方研究入口读取失败 %s: %s", index_url, response)
                    continue
                try:
                    response.raise_for_status()
                except Exception as exc:
                    self.page_coverage[index_url] = {
                        "status": "request_failed",
                        "error": f"HTTP {response.status_code}",
                        "discovered_links": 0,
                        "evidence": 0,
                    }
                    logger.warning("官方研究入口返回失败 %s: %s", index_url, exc)
                    continue
                links = _discover_index_links(response.text, str(response.url))
                allowed_links = [
                    link for link in links if source_link_allowed(index_url, link.url)
                ]
                recent_links = [
                    link
                    for link in allowed_links
                    if not link.published_at
                    or not _parse_date(link.published_at)
                    or _parse_date(link.published_at) >= cutoff
                ]
                selected_links = (
                    recent_links[: self.per_page]
                    if self.per_page > 0
                    else recent_links
                )
                self.page_coverage[index_url] = {
                    "status": (
                        "parsed"
                        if selected_links
                        else ("no_recent_links" if links else "parse_zero_links")
                    ),
                    "discovered_links": len(links),
                    "allowed_links": len(allowed_links),
                    "recent_links": len(recent_links),
                    "selected_links": len(selected_links),
                    "detail_failures": 0,
                    "evidence": 0,
                }
                if self.per_page > 0 and len(recent_links) > self.per_page:
                    logger.warning(
                        "官方研究入口 %s 触发显式 link safety limit：%s → %s",
                        index_url,
                        len(recent_links),
                        self.per_page,
                    )
                for link in selected_links:
                    discovered.append((index_url, link))

            detail_responses = await asyncio.gather(
                *(
                    bounded_get(client, _download_url(link.url))
                    for _, link in discovered
                ),
                return_exceptions=True,
            )

        results: list[TechnicalEvidence] = []
        for (index_url, link), response in zip(discovered, detail_responses):
            if isinstance(response, Exception):
                self.page_coverage[index_url]["detail_failures"] = int(
                    self.page_coverage[index_url].get("detail_failures", 0)
                ) + 1
                continue
            try:
                response.raise_for_status()
            except Exception:
                self.page_coverage[index_url]["detail_failures"] = int(
                    self.page_coverage[index_url].get("detail_failures", 0)
                ) + 1
                continue
            if _is_pdf_response(response, str(response.url)):
                try:
                    title, body, authors = _extract_pdf_document(response.content)
                except Exception as exc:
                    logger.warning("官方 Technical Report PDF 解析失败 %s: %s", link.url, exc)
                    continue
                site_name = ""
                published = link.published_at
                linked_documents: list[dict[str, str]] = []
            else:
                article = _ArticleParser(str(response.url))
                article.feed(response.text)
                title = article.title
                body = _compact_text(article.text)
                authors = article.authors
                site_name = article.site_name
                published = (
                    article.modified_at
                    or article.published_at
                    or link.published_at
                )
                linked_documents = [
                    {"title": label or "linked research document", "url": url}
                    for label, url in article.links
                    if source_link_allowed(index_url, url)
                    and looks_like_linked_research_document(label, url)
                    and url.rstrip("/") != str(response.url).rstrip("/")
                ]
                linked_documents = list(
                    {
                        item["url"].rstrip("/"): item
                        for item in linked_documents
                    }.values()
                )
            published_dt = _parse_date(published)
            if published_dt and published_dt < cutoff:
                continue
            title = title or link.title
            if not title or len(body) < 120:
                continue
            # PDF 元数据标题有时只写模型名；保留索引页锚文本，避免把明确标注的
            # “Technical Report”误判成普通模型发布。
            classification = classify_publication(
                title=f"{title} {link.title}",
                url=link.url,
                summary=body,
                metadata=" ".join(
                    [
                        site_name,
                        *[
                            f"{item['title']} {item['url']}"
                            for item in linked_documents
                        ],
                    ]
                ),
                official=True,
            )
            origin_kind = classification.origin_kind
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
                        "origin_classification_reason": classification.reason,
                        "document_format": classification.document_format,
                        "system_layer_count": classification.system_layer_count,
                        "origin_priority": 3 if origin_kind == "technical_report" else 2,
                        "publisher_tier": publisher_tier,
                        "publisher_evidence": publisher_evidence,
                        "research_index_url": index_url,
                        "linked_research_documents": linked_documents,
                        "source_owner_id": str(source_meta.get("owner", "")) if source_meta else "",
                    },
                )
            )
            self.page_coverage[index_url]["evidence"] = int(
                self.page_coverage[index_url].get("evidence", 0)
            ) + 1
        return list({item.fingerprint: item for item in results}.values())

    def coverage(self) -> dict[str, object]:
        statuses = list(self.page_coverage.values())
        return {
            "total_pages": len(self.pages),
            "checked_pages": len(statuses),
            "request_failed": sum(
                item.get("status") == "request_failed" for item in statuses
            ),
            "parse_zero_links": sum(
                item.get("status") == "parse_zero_links" for item in statuses
            ),
            "no_recent_links": sum(
                item.get("status") == "no_recent_links" for item in statuses
            ),
            "detail_failures": sum(
                int(item.get("detail_failures", 0) or 0) for item in statuses
            ),
            "evidence": sum(int(item.get("evidence", 0) or 0) for item in statuses),
            "pages": self.page_coverage,
        }


class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href = ""
        self._label = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        attributes = dict(attrs)
        self._href = attributes.get("href") or ""
        self._label = attributes.get("aria-label") or attributes.get("title") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href:
            title = _compact_text(" ".join(self._text)) or _compact_text(self._label)
            self.anchors.append((title, self._href))
            self._href = ""
            self._label = ""
            self._text = []


class _ArticleParser(HTMLParser):
    def __init__(self, base_url: str = ""):
        super().__init__()
        self.base_url = base_url
        self.title = ""
        self.published_at = ""
        self.modified_at = ""
        self.authors: list[str] = []
        self.site_name = ""
        self.links: list[tuple[str, str]] = []
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False
        self._href = ""
        self._link_label: list[str] = []

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
        if lowered == "a" and attributes.get("href"):
            self._href = urljoin(self.base_url, attributes["href"])
            self._link_label = []
        if lowered == "link":
            rel = attributes.get("rel", "").casefold()
            href = attributes.get("href", "")
            if href and (
                "alternate" in rel
                or "canonical" in rel
                or attributes.get("type", "").casefold() == "application/pdf"
            ):
                self.links.append(
                    (
                        attributes.get("title", "") or rel,
                        urljoin(self.base_url, href),
                    )
                )
        if lowered == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").casefold()
            if key in {
                "article:published_time",
                "date",
                "datepublished",
                "citation_publication_date",
            }:
                self.published_at = self.published_at or attributes.get("content", "")
            if key in {"article:modified_time", "datemodified", "last-modified"}:
                self.modified_at = self.modified_at or attributes.get("content", "")
            if key in {"author", "citation_author"}:
                author = _compact_text(attributes.get("content", ""))
                if author and not author.startswith(("http://", "https://")):
                    self.authors.append(author)
            if key == "og:site_name":
                self.site_name = self.site_name or _compact_text(
                    attributes.get("content", "")
                )
            if key in {"og:title", "citation_title", "twitter:title"}:
                self.title = self.title or _compact_text(
                    attributes.get("content", "")
                )
            if key in {"citation_pdf_url", "pdf_url"} and attributes.get("content"):
                self.links.append(
                    (
                        "citation PDF",
                        urljoin(self.base_url, attributes["content"]),
                    )
                )

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "svg", "nav", "footer"} and self._ignored_depth:
            self._ignored_depth -= 1
        if lowered == "title":
            self._in_title = False
            self.title = self.title or _compact_text(
                " ".join(self._title_parts)
            ).split(" | ", 1)[0]
        if lowered == "a" and self._href:
            self.links.append(
                (_compact_text(" ".join(self._link_label)), self._href)
            )
            self._href = ""
            self._link_label = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._href:
            self._link_label.append(data)
        if not self._ignored_depth and data.strip():
            self._text_parts.append(data.strip())


class _StructuredDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks: list[str] = []
        self._capture = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if (
            tag.casefold() == "script"
            and attributes.get("type", "").casefold() == "application/ld+json"
        ):
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capture:
            self.blocks.append("".join(self._parts))
            self._capture = False
            self._parts = []


def _discover_index_links(html_text: str, base_url: str) -> list[_ResearchLink]:
    parser = _AnchorParser()
    parser.feed(html_text)
    by_url: dict[str, _ResearchLink] = {}
    raw_links = list(parser.anchors)
    # Next.js 等站点常把发布日期放在卡片外部，只在 hydration 数据里保留
    # title/href/date。读取这份公开结构化数据，避免“链接抓到了但日期丢失”。
    raw_links.extend(
        (item.title, item.url, item.published_at)
        for item in _discover_embedded_publications(html_text, base_url)
    )
    for raw_link in raw_links:
        if len(raw_link) == 2:
            title, href = raw_link
            embedded_date = ""
        else:
            title, href, embedded_date = raw_link
        url = urljoin(base_url, href)
        if not _looks_like_research_link(title, url):
            continue
        date = embedded_date or _date_from_text(title)
        cleaned_title = re.sub(
            r"^\s*\d{4}[-/.]\d{2}[-/.]\d{2}\s*",
            "",
            title,
        ).strip()
        key = url.rstrip("/")
        previous = by_url.get(key)
        candidate = _ResearchLink(
            title=cleaned_title or title,
            url=url,
            published_at=date,
        )
        if (
            previous is None
            or (candidate.published_at and not previous.published_at)
            or len(candidate.title) > len(previous.title)
        ):
            by_url[key] = candidate
    return sorted(
        by_url.values(),
        key=lambda item: (bool(item.published_at), item.published_at),
        reverse=True,
    )


def _discover_embedded_publications(
    html_text: str,
    base_url: str,
) -> list[_ResearchLink]:
    """从 JSON-LD 与公开 hydration 数据提取 title/href/date 卡片。"""
    normalized = html_lib.unescape(html_text)
    normalized = re.sub(r"\\+/", "/", normalized)
    normalized = re.sub(r'\\+"', '"', normalized)
    results: dict[str, _ResearchLink] = {}
    structured = _StructuredDataParser()
    structured.feed(html_text)
    for block in structured.blocks:
        try:
            payload = json.loads(block)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in _walk_structured_publications(payload, base_url):
            results[item.url.rstrip("/")] = item
    for chunk in re.findall(r"\{[^{}]{0,5000}\}", normalized):
        title_match = re.search(
            r'"(?:title|name)"\s*:\s*"((?:\\.|[^"])*)"',
            chunk,
            flags=re.IGNORECASE,
        )
        href_match = re.search(
            r'"(?:href|url|link)"\s*:\s*"((?:\\.|[^"])*)"',
            chunk,
            flags=re.IGNORECASE,
        )
        date_match = re.search(
            r'"(?:date|published_at|publishedAt|datePublished)"\s*:\s*"([^"]+)"',
            chunk,
            flags=re.IGNORECASE,
        )
        if not title_match or not href_match:
            continue
        title = _compact_text(title_match.group(1))
        url = urljoin(base_url, href_match.group(1))
        date = _normalize_date(date_match.group(1)) if date_match else ""
        if not title or not url.startswith(("http://", "https://")):
            continue
        results[url.rstrip("/")] = _ResearchLink(
            title=title,
            url=url,
            published_at=date,
        )
    return list(results.values())


def _walk_structured_publications(
    payload: object,
    base_url: str,
) -> list[_ResearchLink]:
    results: list[_ResearchLink] = []
    if isinstance(payload, list):
        for item in payload:
            results.extend(_walk_structured_publications(item, base_url))
        return results
    if not isinstance(payload, dict):
        return results

    title = _structured_scalar(
        payload.get("headline")
        or payload.get("name")
        or payload.get("title")
    )
    url_value = (
        payload.get("url")
        or payload.get("contentUrl")
        or payload.get("mainEntityOfPage")
    )
    if isinstance(url_value, dict):
        url_value = url_value.get("@id") or url_value.get("url")
    url = urljoin(base_url, _structured_scalar(url_value))
    published = _structured_scalar(
        payload.get("datePublished")
        or payload.get("dateCreated")
        or payload.get("uploadDate")
        or payload.get("dateModified")
    )
    if title and url.startswith(("http://", "https://")):
        results.append(
            _ResearchLink(
                title=title,
                url=url,
                published_at=_normalize_date(published),
            )
        )
    for value in payload.values():
        if isinstance(value, (dict, list)):
            results.extend(_walk_structured_publications(value, base_url))
    return results


def _structured_scalar(value: object) -> str:
    return _compact_text(str(value)) if isinstance(value, (str, int, float)) else ""


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
    blog_detail = bool(re.search(r"/blog/[^/]+/?$", lowered_path))
    structured_detail = bool(
        re.search(
            r"/(?:research|news|article|articles|detail|achievement)/[^/]+/?$",
            lowered_path,
        )
    )
    return path_signal and (
        title_signal or publication_path or blog_detail or structured_detail
    )


def _origin_kind(title: str, url: str) -> str:
    return classify_publication(
        title=title,
        url=url,
        official=True,
    ).origin_kind


def _date_from_text(value: str) -> str:
    match = re.search(r"\b(20\d{2}[-/.]\d{2}[-/.]\d{2})\b", value)
    return _normalize_date(match.group(1)) if match else ""


def _normalize_date(value: str) -> str:
    match = re.search(r"\b(20\d{2})[-/.](\d{2})[-/.](\d{2})\b", value)
    if not match:
        return value
    return f"{value[:match.start()]}{'-'.join(match.groups())}{value[match.end():]}"


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(_normalize_date(value).replace("Z", "+00:00"))
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


def _download_url(url: str) -> str:
    """把 Hugging Face 的 HTML blob 页面改成真实文件下载地址。"""
    parsed = urlparse(url)
    if parsed.hostname == "huggingface.co" and "/blob/" in parsed.path:
        return url.replace("/blob/", "/resolve/", 1)
    return url


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
