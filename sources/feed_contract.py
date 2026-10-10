"""Shared RSS/Atom protocol checks; a login page is not an empty feed."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

ATOM = "http://www.w3.org/2005/Atom"


def feed_nodes(xml_text: str) -> list[ET.Element]:
    root = ET.fromstring(xml_text)
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise ValueError("RSS is missing channel")
        nodes = channel.findall("item")
    elif root.tag == f"{{{ATOM}}}feed":
        nodes = root.findall(f"{{{ATOM}}}entry")
    elif root.tag == "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF":
        nodes = root.findall("{http://purl.org/rss/1.0/}item")
    else:
        raise ValueError("Response is not RSS/Atom")
    # Validate before date/relevance filtering: bad records are protocol gaps,
    # not evidence that the research window contains no relevant work.
    for node in nodes:
        if not feed_text(node, "title") or not feed_link(node):
            raise ValueError("Feed entry is missing title or article link")
    return nodes


def feed_text(node: ET.Element, name: str) -> str:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == name:
            if name == "author":
                author_name = next((item for item in child if item.tag.rsplit("}", 1)[-1] == "name"), None)
                if author_name is not None:
                    return "".join(author_name.itertext()).strip()
            return "".join(child.itertext()).strip()
    return ""


def feed_link(node: ET.Element, base_url: str = "") -> str:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] != "link":
            continue
        if child.get("rel", "alternate") not in {"", "alternate"}:
            continue
        value = (child.get("href") or child.text or "").strip()
        if not value:
            continue
        value = urljoin(base_url, value)
        if base_url and urlparse(value).scheme not in {"http", "https"}:
            raise ValueError("Feed article link is not HTTP(S)")
        return value
    return ""
