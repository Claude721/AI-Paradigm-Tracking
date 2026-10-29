"""Deterministic, work-scoped authorship; no identity search or model calls.

Names and paper affiliations survive optional biography failures. Publisher
names are deliberately never used as individual employment or affiliation.
All read-side helpers are pure: recovery of old excerpts must not silently
change an evidence revision or invalidate its CAS receipt.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

from .models import (ORIGIN_EVIDENCE_TYPES, EvidenceType, SourceAuthorship,
                     plausible_researcher_name, primary_material_url)


def _byline_name(value: str) -> str:
    value = re.sub(r"\\(?:dagger|ddagger|thanks|ast)\b", "", str(value))
    value = re.sub(r"[\s\d,*∗†‡🖂]+$", "", value).strip(" ,，;†‡*∗ ")
    value = re.sub(r"\s+", " ", value)
    return value if plausible_researcher_name(value) else ""


def individual_name(value: str) -> str:
    value = _byline_name(value)
    collective = re.search(r"\b(?:team|consortium|collaboration|contributors|group)\b|团队|研究组", value, re.I)
    if not plausible_researcher_name(value) or collective:
        return ""
    from .reputation import resolve_organization
    return "" if resolve_organization(value) is not None else value


def _names(evidence):
    names = list(dict.fromkeys(name for raw in evidence.authors
                               if (name := individual_name(raw))))
    if not names:
        # Old HTML hydration kept roles but dropped the author list. Split
        # only explicit byline fields, never arbitrary names in body prose.
        roles = evidence.raw.get("author_roles")
        for raw in roles if isinstance(roles, dict) else {}:
            if not isinstance(raw, str):
                continue
            for part in re.split(r"[,，;]", raw):
                name = individual_name(part)
                if name and name not in names:
                    names.append(name)
        # JSON object key order is not paper byline order (old snapshots were
        # often sorted alphabetically). Prefer actual header positions.
        header = str(evidence.raw.get("document_excerpt") or "")[:14000]
        header = re.split(r"\bAbstract\b|\bIntroduction\b", header, maxsplit=1, flags=re.I)[0]
        names.sort(key=lambda name: header.find(name) if name in header else len(header))
    return names


_INSTITUTION = re.compile(r"\b(?:university|universität|institute|institution|school|college|laborator(?:y|ies)|lab|research|inc\.?|ltd\.?|group|KAIST|Meta|NVIDIA|Google|Tencent|Microsoft|Amazon)\b|大学|研究院|研究所", re.I)


def _clean_affiliation(value):
    value = re.split(r"\b(?:Email|Correspondence|Corresponding author|Work completed|Abstract)\b\s*:?", value, maxsplit=1, flags=re.I)[0]
    value = re.sub(r"\S+@\S+", "", value).strip(" ,;†‡*∗")
    value = re.sub(r"\s+", " ", value)
    return value if (1 < len(value) <= 180 and "http" not in value
                     and not any(c in value for c in "[]{}©")
                     and value.casefold() not in {"none", "unknown", "n/a"}) else ""


def _header_affiliations(evidence, names):
    """Recover only immediately adjacent, unambiguous old byline text.

    Do not scan references or model-written summaries; do not assign a flat
    list of all paper institutions to every author. Numbered/multi-column
    layouts without a reliable binding are left unknown.
    """
    text = str(evidence.raw.get("document_excerpt") or "")[:14000]
    text = re.split(r"\bAbstract\b|\bIntroduction\b", text, maxsplit=1, flags=re.I)[0]
    positions = []
    for name in names:
        match = re.search(r"(?<!\w)" + r"\s+".join(re.escape(p) for p in name.split()) + r"(?!\w)", text)
        if match:
            positions.append((match.start(), match.end(), name))
    positions.sort()
    # Some old arXiv conversions place a numbered institution block before
    # the title or after all author names. Bind only explicit superscripts to
    # explicit definitions, never by ordering the institution list.
    definition_blocks = text.split("Affiliation:")[1:]
    if positions:
        definition_blocks.append(text[:positions[0][0]])
    numbered = {}
    ambiguous = set()
    for block in definition_blocks:
        block = re.split(r"[†‡*∗]|\bCorresponding author\b", block, maxsplit=1)[0]
        marks = list(re.finditer(r"(?<!\w)([1-9]\d?)\s+(?=[A-Z\u4e00-\u9fff])", block))
        for index, mark in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(block)
            value = _clean_affiliation(block[mark.end():end])
            if value and _INSTITUTION.search(value):
                key = mark.group(1)
                if key in numbered and numbered[key] != value:
                    ambiguous.add(key)
                numbered[key] = value
    for key in ambiguous:
        numbered.pop(key, None)
    result = {}
    for index, (_, end, name) in enumerate(positions):
        stop = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        segment = text[end:stop].strip(" ,†‡*∗")
        # Old role-only snapshots sometimes contain only the first/last name.
        # Stop before an intervening byline rather than collecting that other
        # person's institutions. Ambiguous company/name text may lose a fact,
        # but must never invent a cross-person affiliation.
        for boundary in re.finditer(r"(?=\b([A-Z][\w’'.-]+(?:\s+[A-Z][\w’'.-]+){1,3})\s+Affiliation:)", segment):
            if not _INSTITUTION.search(boundary.group(1)):
                segment = segment[:boundary.start()]
                break
        # Superscript indices require an explicit HTML ref binding. Removing
        # them and guessing a nearby institution would be unsafe.
        if segment[:1].isdigit():
            marker = re.match(r"(\d+(?:\s*,\s*\d+)*)", segment)
            indices = re.findall(r"\d+", marker.group())
            if indices and all(key in numbered for key in indices):
                result[name] = list(dict.fromkeys(numbered[key] for key in indices))
            continue
        if segment.startswith("Affiliation:"):
            values = []
            for chunk in segment.split("Affiliation:")[1:]:
                value = _clean_affiliation(chunk)
                if value:
                    values.append(value)
                if re.search(r"\b(?:Email|Correspondence|Work completed)\b", chunk, re.I):
                    break
            if values:
                result[name] = list(dict.fromkeys(values))
        elif "@" in segment and (value := _clean_affiliation(segment)) and _INSTITUTION.search(value):
            result[name] = [value]
    return result


def source_authorship(evidence, *, limit=None):
    if evidence.evidence_type not in ORIGIN_EVIDENCE_TYPES:
        return []
    url = primary_material_url(evidence)
    if not url:
        return []
    names = _names(evidence)
    raw_roles = evidence.raw.get("author_roles")
    roles = {}
    for key, value in raw_roles.items() if isinstance(raw_roles, dict) else ():
        if name := individual_name(key):
            roles.setdefault(name, str(value))
    raw_affiliations = evidence.raw.get("author_affiliations")
    affiliations = {}
    for key, values in raw_affiliations.items() if isinstance(raw_affiliations, dict) else ():
        if (name := individual_name(key)) and isinstance(values, list):
            affiliations.setdefault(name, values)
    recovered = _header_affiliations(evidence, names)
    header = re.split(r"\bAbstract\b|\bIntroduction\b", str(evidence.raw.get("document_excerpt") or "")[:14000], maxsplit=1, flags=re.I)[0]
    facts = []
    for index, name in enumerate(names):
        role = roles.get(name, "")
        if role == "第一作者":
            role = "署名首位作者" if evidence.evidence_type == EvidenceType.PRIMARY_PAPER else "公开署名作者/贡献角色未披露"
        elif not role:
            role = ("署名首位作者" if index == 0 else "共同署名作者") if evidence.evidence_type == EvidenceType.PRIMARY_PAPER else "公开署名作者/贡献角色未披露"
            if index == 0 and evidence.evidence_type == EvidenceType.PRIMARY_PAPER and evidence.authors and individual_name(evidence.authors[0]) != name:
                role = "第一位具名作者/贡献角色待核验"
        if "末位作者/资深作者线索" in role:
            role = "末位署名作者（贡献角色未核验）"
        if re.search(r"contributed equally|equal contribution", header, re.I):
            marker = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)([^A-Za-z]{0,15})", header)
            if marker and re.search(r"[*∗]", marker.group(1)):
                role = "共同第一作者"
        values = affiliations.get(name, [])
        values = [v.strip() for v in values if isinstance(v, str) and v.strip()]
        if not values:
            values = recovered.get(name, [])
        facts.append(SourceAuthorship(name, role, tuple(dict.fromkeys(values)), url,
                                      evidence.fingerprint, evidence.source_revision))
    # Priority order is byline order, not alphabetic JSON dictionary order.
    byline_order = {name: index for index, name in enumerate(names)}
    facts.sort(key=lambda fact: ("首位" not in fact.role and "第一" not in fact.role, byline_order[fact.name]))
    return facts if limit is None else facts[:limit]


def candidate_authorship(candidate, *, limit=3):
    from .evidence_association import countable_evidence
    facts = []
    for evidence in candidate.evidence:
        if countable_evidence(candidate, evidence):
            # Bound per work, not across independent primary works. Same names
            # on different works remain distinct source-scoped assertions.
            facts.extend(source_authorship(evidence, limit=limit))
    return facts


class _Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    @property
    def classes(self):
        return set(self.attrs.get("class", "").split())

    def text(self, *, name=False):
        if name and (self.tag == "sup" or self.classes & {"ltx_note", "ltx_role_footnote", "ltx_author_notes", "ltx_affiliation", "ltx_role_affiliation"}):
            return ""
        return " ".join(child.text(name=name) if isinstance(child, _Node) else child for child in self.children).strip()

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()


class _BylineHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {"meta", "link", "img", "br", "hr", "input", "wbr", "source"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_authorship_html(html):
    """Parse explicit author containers and affiliation refs, not page prose."""
    parser = _BylineHTML()
    parser.feed(html)
    nodes = list(parser.root.walk())
    by_id = {node.attrs['id']: node for node in nodes if node.attrs.get('id')}
    names, mapping, roles = [], {}, {}
    starred = []
    for node in nodes:
        if "ltx_personname" not in node.classes:
            continue
        ancestor = node.parent
        ignored = False
        while ancestor:
            ignored |= bool(ancestor.classes & {"ltx_bibliography", "ltx_bibitem"}) or ancestor.tag in {"nav", "footer", "script", "style"}
            ancestor = ancestor.parent
        if ignored:
            continue
        group = node.parent
        while group and not group.classes & {"ltx_author", "ltx_creator"}:
            group = group.parent
        # An isolated personname is still a byline, but not a license to use
        # all affiliations from the rest of the paper.
        parts = re.split(r"[,，;]", node.text(name=True))
        for part in parts:
            name = _byline_name(part)
            if not name:
                continue
            if name not in names:
                names.append(name)
            if not individual_name(name):
                # Preserve collective signatures and their position in the
                # complete byline, but never turn them into person profiles.
                continue
            if "*" in node.text() or "∗" in node.text():
                # For comma-combined author spans, check this person's token,
                # not another author's asterisk elsewhere in the same node.
                raw_part = next((p for p in re.split(r"[,，;]", node.text()) if individual_name(p) == name), "")
                if "*" in raw_part or "∗" in raw_part:
                    starred.append(name)
            if group:
                author_nodes = [n for n in group.walk() if "ltx_personname" in n.classes]
                own = [n for n in group.walk() if n.classes & {"ltx_affiliation", "ltx_role_affiliation"}]
                if (len(author_nodes) == 1 and len(parts) == 1
                        or len(own) == 1 and not re.search(r"\d", group.text())):
                    values = [_clean_affiliation(n.text()) for n in own]
                    mapping[name] = list(dict.fromkeys(v for v in values if v))
                # Resolve numbered affiliations only when the name's own
                # superscript explicitly links to the matching node ID.
                refs = [n.attrs.get("href", "")[1:] for n in node.walk()
                        if n.tag == "a" and n.attrs.get("href", "").startswith("#")]
                targets = [by_id[ref] for ref in refs if ref in by_id]
                values = [_clean_affiliation(target.text()) for target in targets
                          if target.classes & {"ltx_affiliation", "ltx_role_affiliation"}
                          or _INSTITUTION.search(target.text())]
                if any(values):
                    mapping[name] = list(dict.fromkeys(v for v in values if v))
                if any(re.search(r"\bcorresponding author\b", target.text(), re.I) for target in targets):
                    roles[name] = "通讯作者"
    # citation_* metadata explicitly repeats the author for each institution.
    current = ""
    metadata_names = []
    for node in nodes:
        label = node.attrs.get("name", "").casefold()
        if node.tag == "meta" and label == "citation_author":
            current = _byline_name(node.attrs.get("content", ""))
            if current and current not in metadata_names:
                metadata_names.append(current)
        elif node.tag == "meta" and label == "citation_author_institution" and current:
            value = _clean_affiliation(node.attrs.get("content", ""))
            if value and value not in mapping.setdefault(current, []):
                mapping[current].append(value)
    names = list(dict.fromkeys([*metadata_names, *names]))
    header = re.split(r"\bAbstract\b|\bIntroduction\b", parser.root.text(), maxsplit=1, flags=re.I)[0]
    if re.search(r"(?:contributed equally|equal contribution|equal contributors)", header, re.I):
        for name in starred:
            roles[name] = "共同第一作者" + ("/通讯作者" if roles.get(name) == "通讯作者" else "")
    return {"authors": names, "author_affiliations": mapping, "author_roles": roles}
