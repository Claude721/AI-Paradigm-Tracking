"""通过 arXiv HTML 为已通过初筛的论文补齐正文、作者与项目链接。"""

from __future__ import annotations

import logging
import ipaddress
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from paradigms.models import TechnicalEvidence

logger = logging.getLogger(__name__)


class ArxivDocumentClient:
    """只在深挖阶段读取官方 HTML，不把全量论文 PDF 送入初筛模型。"""

    async def hydrate(self, evidence: TechnicalEvidence) -> dict[str, str]:
        if evidence.raw.get("document_excerpt"):
            return {
                "primary_document": (
                    "已在高优先级初筛前读取 arXiv HTML/项目页，本阶段复用"
                )
            }
        arxiv_id = str(evidence.identifiers.get("arxiv", "")).strip()
        if not arxiv_id:
            return {"primary_document": "非 arXiv 原点，未执行 arXiv HTML 补水"}
        versionless = re.sub(r"v\d+$", "", arxiv_id)
        url = f"https://arxiv.org/html/{versionless}"
        try:
            async with httpx.AsyncClient(
                timeout=35, follow_redirects=True
            ) as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": "AI-Paradigm-Radar/3.3"},
                )
                response.raise_for_status()
        except Exception as exc:
            logger.warning("arXiv HTML 补水失败 [%s]: %s", arxiv_id, exc)
            return {
                "primary_document": (
                    f"已尝试 arXiv HTML，但读取失败：{type(exc).__name__}"
                )
            }

        parsed = parse_arxiv_html(response.text, base_url=str(response.url))
        project_detail = {}
        project_page = next(
            (
                value
                for value in parsed["project_urls"]
                if "github.com" not in urlparse(value).netloc.casefold()
            ),
            "",
        )
        if project_page and _safe_public_url(project_page):
            try:
                async with httpx.AsyncClient(
                    timeout=30, follow_redirects=True
                ) as project_client:
                    project_response = await project_client.get(
                        project_page,
                        headers={"User-Agent": "AI-Paradigm-Radar/3.3"},
                    )
                    project_response.raise_for_status()
                project_detail = parse_project_page(
                    project_response.text,
                    base_url=str(project_response.url),
                    author_names=evidence.authors,
                )
            except Exception as exc:
                logger.warning(
                    "论文项目页补水失败 [%s]: %s", project_page, exc
                )
        if parsed["document_excerpt"]:
            evidence.raw["document_excerpt"] = parsed["document_excerpt"]
            evidence.raw["document_source_url"] = str(response.url)
        if parsed["affiliations"]:
            evidence.raw["affiliations"] = parsed["affiliations"]
            if not evidence.organization:
                evidence.organization = "；".join(parsed["affiliations"][:4])
        author_profiles = {
            **parsed["author_profile_urls"],
            **project_detail.get("author_profile_urls", {}),
        }
        if author_profiles:
            evidence.raw["author_profile_urls"] = author_profiles
        if project_detail.get("author_public_emails"):
            evidence.raw["author_public_emails"] = project_detail[
                "author_public_emails"
            ]
        if parsed["project_urls"]:
            evidence.raw["project_urls"] = parsed["project_urls"]
        if parsed["github_repositories"]:
            evidence.raw["github_repositories"] = parsed["github_repositories"]
        author_roles = {
            **parsed["author_roles"],
            **project_detail.get("author_roles", {}),
        }
        if author_roles:
            evidence.raw["author_roles"] = author_roles
        return {
            "primary_document": (
                f"已读取 arXiv HTML 正文 {len(parsed['document_excerpt'])} 字符；"
                f"发现 {len(parsed['project_urls'])} 个项目/代码链接、"
                f"{len(author_profiles)} 个作者公开主页线索；"
                f"项目页{'已核验' if project_detail else '未读取或无可用页面'}"
            )
        }


def parse_arxiv_html(html_text: str, *, base_url: str) -> dict:
    parser = _ArxivHTMLParser(base_url)
    parser.feed(html_text)
    document = _compact_text(" ".join(parser.document_parts))
    # 开头通常包含摘要、方法与实验；保留足够上下文，同时阻止单篇论文无限
    # 膨胀主 Agent 输入。正文截断本身会明确留在 document coverage 中。
    excerpt = document[:18_000]
    author_profiles = {
        name: url
        for name, url in parser.author_profile_urls.items()
        if name and _public_external_url(url)
    }
    project_urls = [
        url
        for url in _unique(parser.project_urls)
        if _public_external_url(url)
    ]
    repositories = [
        url
        for url in project_urls
        if urlparse(url).netloc.casefold() in {"github.com", "www.github.com"}
    ]
    author_roles = _infer_author_roles(parser.authors, parser.equal_contribution_names)
    return {
        "document_excerpt": excerpt,
        "affiliations": _unique(parser.affiliations),
        "author_profile_urls": author_profiles,
        "project_urls": project_urls,
        "github_repositories": repositories,
        "author_roles": author_roles,
    }


def parse_project_page(
    html_text: str, *, base_url: str, author_names: list[str]
) -> dict:
    parser = _ProjectPageParser(base_url)
    parser.feed(html_text)
    profiles: dict[str, str] = {}
    emails: dict[str, str] = {}
    starred: list[str] = []
    for label, url in parser.links:
        normalized_label = _compact_name(label.removesuffix("*"))
        author = next(
            (
                name
                for name in author_names
                if normalized_label == _compact_name(name)
            ),
            "",
        )
        if not author:
            continue
        if label.rstrip().endswith("*"):
            starred.append(author)
        if url.startswith("mailto:"):
            email = url.removeprefix("mailto:").split("?", 1)[0]
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                emails[author] = email
        elif _safe_public_url(url):
            profiles[author] = url
    roles = {}
    page_text = _compact_text(" ".join(parser.text_parts)).casefold()
    if len(starred) >= 2 and (
        "equal contribution" in page_text
        or "contributed equally" in page_text
    ):
        roles = {name: "共同第一作者" for name in starred}
    return {
        "author_profile_urls": profiles,
        "author_public_emails": emails,
        "author_roles": roles,
    }


class _ArxivHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.document_parts: list[str] = []
        self.affiliations: list[str] = []
        self.authors: list[str] = []
        self.equal_contribution_names: set[str] = set()
        self.author_profile_urls: dict[str, str] = {}
        self.project_urls: list[str] = []
        self._capture_document = 0
        self._capture_affiliation = 0
        self._capture_author = 0
        self._capture_note = 0
        self._current_affiliation: list[str] = []
        self._current_author: list[str] = []
        self._current_note: list[str] = []
        self._current_href = ""
        self._current_anchor: list[str] = []
        self._in_author_anchor = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag in {"article", "section", "p", "figcaption"}:
            self._capture_document += 1
        if "ltx_affiliation" in classes:
            self._capture_affiliation += 1
        if "ltx_personname" in classes:
            self._capture_author += 1
        if "ltx_note" in classes or "ltx_role_footnote" in classes:
            self._capture_note += 1
        if tag == "meta":
            self._handle_meta(values)
        if tag == "a" and values.get("href"):
            self._current_href = urljoin(self.base_url, values["href"])
            self._current_anchor = []
            self._in_author_anchor = self._capture_author > 0

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href:
            label = _compact_text(" ".join(self._current_anchor))
            if self._in_author_anchor and label:
                self.author_profile_urls.setdefault(label, self._current_href)
            elif _looks_like_project_link(label, self._current_href):
                self.project_urls.append(self._current_href)
            self._current_href = ""
            self._current_anchor = []
            self._in_author_anchor = False
        if self._capture_author and tag == "span":
            name = _compact_text(" ".join(self._current_author))
            if name and name not in self.authors:
                self.authors.append(name)
            self._current_author = []
            self._capture_author -= 1
        if self._capture_affiliation and tag in {"span", "div"}:
            affiliation = _compact_text(" ".join(self._current_affiliation))
            if affiliation:
                self.affiliations.append(affiliation)
            self._current_affiliation = []
            self._capture_affiliation -= 1
        if self._capture_note and tag in {"span", "div"}:
            note = _compact_text(" ".join(self._current_note))
            note_lower = note.casefold()
            if (
                "equal contribution" in note_lower
                or "contributed equally" in note_lower
                or "equal contributors" in note_lower
            ):
                named = [
                    author
                    for author in self.authors
                    if _compact_name(author) in _compact_name(note)
                ]
                for author in named:
                    self.equal_contribution_names.add(author)
            self._current_note = []
            self._capture_note -= 1
        if self._capture_document and tag in {"article", "section", "p", "figcaption"}:
            self._capture_document -= 1

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        if self._capture_document:
            self.document_parts.append(value)
        if self._capture_affiliation:
            self._current_affiliation.append(value)
        if self._capture_author:
            self._current_author.append(value)
        if self._capture_note:
            self._current_note.append(value)
        if self._current_href:
            self._current_anchor.append(value)

    def _handle_meta(self, values: dict[str, str]) -> None:
        name = values.get("name", "").casefold()
        content = _compact_text(values.get("content", ""))
        if not content:
            return
        if name == "citation_author" and content not in self.authors:
            self.authors.append(content)
        elif name == "citation_author_institution":
            self.affiliations.append(content)


class _ProjectPageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self._href = ""
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = {key: value or "" for key, value in attrs}
        href = values.get("href", "")
        if not href:
            return
        self._href = (
            href
            if href.startswith("mailto:")
            else urljoin(self.base_url, href)
        )
        self._label = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append(
                (_compact_text(" ".join(self._label)), self._href)
            )
            self._href = ""
            self._label = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.text_parts.append(value)
            if self._href:
                self._label.append(value)


def _infer_author_roles(
    authors: list[str], equal_contributors: set[str]
) -> dict[str, str]:
    roles: dict[str, str] = {}
    for index, name in enumerate(authors):
        if name in equal_contributors:
            roles[name] = "共同第一作者"
        elif index == 0:
            roles[name] = "第一作者"
        elif index == len(authors) - 1 and len(authors) > 1:
            roles[name] = "末位作者/资深作者线索"
    return roles


def _looks_like_project_link(label: str, url: str) -> bool:
    host = urlparse(url).netloc.casefold()
    if not host or "arxiv.org" in host:
        return False
    text = f"{label} {url}".casefold()
    markers = ("project", "code", "github", "demo", "dataset", "model")
    return any(marker in text for marker in markers)


def _public_external_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and bool(host) and "arxiv.org" not in host


def _safe_public_url(url: str) -> bool:
    if not _public_external_url(url):
        return False
    host = (urlparse(url).hostname or "").casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
