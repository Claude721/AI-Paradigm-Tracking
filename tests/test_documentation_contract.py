"""Keep operational navigation usable without interpreting historical prose as policy."""
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]\n]+\]\((<[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\)")


class DocumentationNavigationTests(unittest.TestCase):
    def test_local_markdown_links_resolve(self):
        documents = [ROOT / name for name in ("README.md", "CLAUDE.md", "信源说明.md")]
        documents.extend(sorted((ROOT / "docs").glob("*.md")))
        for document in documents:
            # Literal examples inside code fences are not navigation links.
            prose = re.sub(r"```.*?```", "", document.read_text(encoding="utf-8"), flags=re.S)
            for target in LINK.findall(prose):
                target = target.removeprefix("<").removesuffix(">")
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                with self.subTest(document=str(document.relative_to(ROOT)), target=target):
                    self.assertTrue((document.parent / unquote(parsed.path)).exists())

    def test_document_index_covers_every_guide_and_history(self):
        index = ROOT / "docs" / "README.md"
        targets = {urlsplit(target).path for target in LINK.findall(index.read_text(encoding="utf-8"))}
        for document in sorted(index.parent.glob("*.md")):
            if document != index:
                with self.subTest(document=document.name):
                    self.assertIn(document.name, targets)
