"""Every relative link in the ops run-books resolves (docs/ops/*.md).

The run-books point at each other (the index pages, the manual VACUUM page, CHECKS.md). A link that
breaks sends an operator mid-incident to a 404, so a renamed or moved page must fail here first.

Run with::

    venv/bin/python -m pytest tests/test_ops_doc_links.py -q
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent / "docs" / "ops"
# [text](target) where target has no scheme and is not an in-page anchor
LINK = re.compile(r"\[[^\]]*\]\(([^)\s#]+)(?:#[^)]*)?\)")


def relative_links(text: str) -> list[str]:
    return [t for t in LINK.findall(text) if not re.match(r"^[a-z][a-z0-9+.-]*:", t)]


class OpsDocLinks(unittest.TestCase):
    def test_the_pattern_sees_a_relative_link_and_skips_urls(self):
        text = "see [a](b.md), [c](../CHECKS.md#rows), [d](https://x.example/y), [e](#here)"
        self.assertEqual(relative_links(text), ["b.md", "../CHECKS.md"])

    def test_every_relative_link_in_the_run_books_resolves(self):
        pages = sorted(OPS.glob("*.md"))
        self.assertTrue(pages, "no run-books found: the instrument is blind")
        broken = [
            f"{p.name} -> {t}"
            for p in pages
            for t in relative_links(p.read_text(encoding="utf-8"))
            if not (p.parent / t).exists()
        ]
        self.assertEqual(broken, [])


if __name__ == "__main__":
    unittest.main()
