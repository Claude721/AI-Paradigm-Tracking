"""Small, fixed professional-link registry; no username generation or OSINT scan.

Existence and person ownership are separate. Only a work-supplied person page,
or that person's explicit links, may authorize contact facts.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

from paradigms.models import _canonical_material_url
from paradigms.publication import nonresearch_page_reason
from paradigms.researcher_identity import normalized_person_name

RULE_VERSION = "professional-public-page-v1"
PLATFORMS = {
    "github": ("github.com", r"/[A-Za-z0-9-]{1,39}/?"),
    "huggingface": ("huggingface.co", r"/[A-Za-z0-9_-]{1,96}/?"),
    "gitee": ("gitee.com", r"/[A-Za-z0-9_-]{1,96}/?"),
    "linkedin": ("linkedin.com", r"/in/[A-Za-z0-9_%.-]+/?"),
    "orcid": ("orcid.org", r"/\d{4}-\d{4}-\d{4}-\d{3}[\dX]/?"),
    "google_scholar": ("scholar.google.com", r"/citations/?"),
    "x": ("x.com", r"/[A-Za-z0-9_]{1,15}/?"),
    "twitter": ("twitter.com", r"/[A-Za-z0-9_]{1,15}/?"),
}
_RESERVED = {"about", "login", "signup", "settings", "explore", "topics", "organizations", "orgs",
             "features", "marketplace", "pricing", "collections", "datasets", "spaces", "models", "docs", "api", "search"}
_SOCIAL_NO_FETCH = {"linkedin.com", "x.com", "twitter.com", "reddit.com", "xiaohongshu.com"}


class IdentityProbeError(ValueError):
    """A fixed, safe diagnostic code; never include response bodies or Secrets."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _Node:
    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs or []), parent, []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self, *, visible_only=False):
        return " ".join(child.text(visible_only=visible_only) if isinstance(child, _Node) else child
                        for child in self.children if not visible_only or not isinstance(child, _Node) or child.visible())

    def visible(self):
        current = self
        while current:
            if current.tag in {"script", "style", "nav", "footer", "aside", "form"} or "hidden" in current.attrs:
                return False
            current = current.parent
        return True


class _Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack, self.count = [self.root], 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 20000 or len(self.stack) > 100:
            raise IdentityProbeError("IdentityHTMLStructureLimit")
        node = _Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def host_matches(host, domain):
    return host == domain or host.endswith("." + domain)


def public_url(url):
    if not isinstance(url, str) or not _canonical_material_url(url):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return False
        if any(re.sub(r"[-_]", "", key.casefold()) in {"apikey", "key", "token", "accesstoken", "authtoken", "authorization", "password"}
               for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            return False
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return bool(host and "." in host)
    except (ValueError, TypeError):
        return False


def profile_label(url, supplied="homepage"):
    if not public_url(url):
        return ""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    for label, (domain, pattern) in PLATFORMS.items():
        if host_matches(host, domain):
            if (not re.fullmatch(pattern, parsed.path) or parsed.path.strip("/").lower() in _RESERVED
                    or (label == "google_scholar" and "user=" not in parsed.query)):
                return ""  # repo/org/index/login is not a person profile.
            return label
    # Do not let lookalike hostnames become a professional-platform fact.
    if any(domain in host for domain, _ in PLATFORMS.values()):
        return ""
    label = re.sub(r"[^a-z0-9]+", "_", supplied.casefold()).strip("_")
    if label in {"homepage", "home", "website", "personal_website", "personal_homepage", "personal_site", "webpage", "research_website"} or not label:
        return "homepage"
    return label


async def validate_public_host(url):
    if not public_url(url):
        raise IdentityProbeError("NonPublicIdentityURL")
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    if any(host_matches(host, domain) for domain in _SOCIAL_NO_FETCH):
        raise IdentityProbeError("IdentityPlatformRequiresAuthorizedInterface")
    addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host,
        parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM), timeout=5)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise IdentityProbeError("IdentityHostIsNotPublic")


def page_status(response):
    text = response.text
    if (response.status_code in {401, 403, 429} or
            response.headers.get("cf-mitigated", "").lower() == "challenge" or
            response.headers.get("x-vercel-mitigated", "").lower() == "challenge"):
        return "blocked", "AccessPolicyOrRateLimit"
    if response.status_code in {404, 410}:
        return "not_found", "ProfileHTTPNotFound"
    if response.status_code >= 400:
        return "failed", "ProfileHTTPError"
    if nonresearch_page_reason(str(response.url), text):
        return "blocked", "NonPublicProfileOrChallenge"
    nodes = list(_Page(text).root.walk())
    heading = " ".join(node.text() for node in nodes if node.tag in {"title", "h1"})
    if re.search(r"\b(?:404|page not found|user not found|sign in|log in|just a moment|verify you are human)\b|页面不存在|用户不存在|访问验证|登录", heading, re.I):
        return "blocked", "ProfileShellOrSoft404"
    if not any(node.tag in {"html", "main", "article", "h1", "p"} for node in nodes):
        return "invalid", "UnrecognizedProfileDocument"
    return "completed", ""


def parse_person_page(text, url, name, *, inherited_owner=False):
    """Extract only person-owned sections; unrelated links remain leads."""
    root = _Page(text).root
    nodes = list(root.walk())
    expected = normalized_person_name(name)
    persons, all_people = [], []
    for script in (n for n in nodes if n.tag == "script" and n.attrs.get("type") == "application/ld+json"):
        try:
            payload = json.loads(script.text())
        except (ValueError, TypeError):
            continue
        json_nodes = payload if isinstance(payload, list) else [payload]
        for node in json_nodes:
            if isinstance(node, dict):
                nodes_graph = node.get("@graph", [])
                nodes_graph = nodes_graph if isinstance(nodes_graph, list) else []
                all_people.extend(v for v in [node, *nodes_graph] if isinstance(v, dict) and v.get("@type") == "Person")
    persons = [v for v in all_people if expected and normalized_person_name(v.get("name", "")) == expected]
    visible = [node for node in nodes if node.visible()]
    headings = [node.text(visible_only=True).strip() for node in visible if node.tag in {"h1", "title"}]
    structured_people = [node for node in visible if str(node.attrs.get("itemtype", "")).endswith("/Person")]
    matching_sections = [node for node in structured_people
                         if any(normalized_person_name(v.text()) == expected for v in node.walk() if v.attrs.get("itemprop") == "name")]
    # A same-name item in a multi-person directory cannot select an arbitrary
    # person's contacts, even when its heading happens to include the name.
    owned = bool(expected and ((len(persons) == len(all_people) == 1) or len(matching_sections) == 1
                               or (any(expected == normalized_person_name(h) or re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", h.casefold()) for h in headings)
                                   and not structured_people and not all_people)
                               or (inherited_owner and not structured_people and not all_people)))
    scope = matching_sections[0] if len(matching_sections) == 1 else root
    links, follow, leads, emails = [], [], [], []
    if not owned:
        return {"owned": False, "links": [], "follow": [], "leads": [], "emails": [], "bio": ""}
    for person in persons[:1]:
        values = person.get("sameAs", [])
        values = [values] if isinstance(values, str) else values
        for value in values if isinstance(values, list) else []:
            label = profile_label(value)
            if label:
                links.append((label, value))
    for anchor in (n for n in scope.walk() if n.tag == "a" and n.visible() and n.attrs.get("href")):
        href = anchor.attrs["href"].strip()
        label_text = anchor.text(visible_only=True).strip().lower()
        if href.startswith("mailto:"):
            email = href[7:].split("?")[0]
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                emails.append(email)
            continue
        target = urljoin(url, href)
        if not public_url(target) or target == url:
            continue
        label = profile_label(target)
        host = (urlsplit(target).hostname or "").lower()
        is_platform = any(host_matches(host, domain) for domain, _ in PLATFORMS.values())
        personal = ("me" in str(anchor.attrs.get("rel", "")).split()
                    or label_text in {"github", "my github", "linkedin", "hugging face", "huggingface", "gitee", "google scholar", "scholar", "orcid"})
        if is_platform:
            if label and personal:
                links.append((label, target))
            else:
                leads.append(target)
        elif (urlsplit(target).netloc == urlsplit(url).netloc
              and urlsplit(target).path.startswith(urlsplit(url).path.rstrip("/") + "/")
              and re.search(r"(?:^|/)(?:about|contact|bio|cv)(?:[./]|$)", urlsplit(target).path, re.I)):
            follow.append(target)
    paragraphs = []
    for node in (n for n in scope.walk() if n.tag == "p" and n.visible()):
        value = re.sub(r"\s+", " ", node.text(visible_only=True)).strip()
        if (40 <= len(value) <= 2500 and re.search(r"research|professor|Ph\.?D|doctoral|postdoc|student|engineer|研究|博士|教授|工程师", value, re.I)
                and (normalized_person_name(name) in normalized_person_name(value)
                     or re.search(r"\b(?:I am|I'm|My research|I received|I work)\b|本人|我[是的在]", value, re.I))):
            paragraphs.append(value)
    return {"owned": True, "links": list(dict.fromkeys(links)), "follow": list(dict.fromkeys(follow)),
            "leads": list(dict.fromkeys(leads)), "emails": list(dict.fromkeys(emails)),
            "bio": " ".join(paragraphs)[:1200], "body_sha256": hashlib.sha256(text.encode()).hexdigest()}
