#!/usr/bin/env python3
"""Validate local docs graph invariants.

This is intentionally a local graph check, not a Mintlify replacement. It
checks the pieces Bazel should be able to reason about cheaply:

- every page listed in docs.json exists
- duplicate docs.json page entries are reported
- local markdown and JSX href links resolve to checked-in docs pages
- internal redirect destinations resolve to checked-in docs pages
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DOCS_ROOT = Path(__file__).resolve().parents[1]
DOCS_JSON = DOCS_ROOT / "docs.json"

DOC_EXTENSIONS = (".mdx", ".md")
DOC_FILE_GLOBS = ("**/*.mdx", "**/*.md")
IGNORED_PATH_PARTS = {".snippets", ".pytest_cache"}
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?P<href>[^)]+)\)")
HTML_HREF_RE = re.compile(r"href=[\"'](?P<href>[^\"']+)[\"']")
FENCED_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


@dataclass(frozen=True)
class DocsGraphIssue:
    source: Path
    message: str

    def display(self) -> str:
        try:
            source = self.source.relative_to(DOCS_ROOT).as_posix()
        except ValueError:
            source = self.source.as_posix()
        return f"{source}: {self.message}"


def load_docs_config(path: Path = DOCS_JSON) -> dict[str, object]:
    return json.loads(path.read_text())


def iter_navigation_pages(value: object) -> list[str]:
    pages: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "pages" and isinstance(child, list):
                pages.extend(iter_navigation_pages(child))
            else:
                pages.extend(iter_navigation_pages(child))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                pages.append(item)
            else:
                pages.extend(iter_navigation_pages(item))
    return pages


def iter_doc_files(root: Path = DOCS_ROOT) -> list[Path]:
    files: list[Path] = []
    for pattern in DOC_FILE_GLOBS:
        files.extend(root.glob(pattern))
    return sorted(
        path
        for path in files
        if not any(part in IGNORED_PATH_PARTS for part in path.relative_to(root).parts)
    )


def page_candidates(page: str, root: Path = DOCS_ROOT) -> tuple[Path, ...]:
    raw = page.removeprefix("/").strip()
    path = root / raw
    if path.suffix in DOC_EXTENSIONS:
        return (path,)
    return tuple(path.with_suffix(extension) for extension in DOC_EXTENSIONS)


def page_exists(page: str, root: Path = DOCS_ROOT) -> bool:
    return any(candidate.exists() for candidate in page_candidates(page, root))


def normalized_local_href(href: str, source: Path) -> str | None:
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    if any(character.isspace() for character in href):
        return None
    parsed = urlparse(href)
    if parsed.scheme in {"http", "https", "mailto", "tel"}:
        return None
    if parsed.scheme:
        return None

    path = parsed.path
    if not path or path.startswith("#"):
        return None
    if path.startswith("/"):
        normalized = path.removeprefix("/")
    else:
        normalized = (source.parent / path).resolve().relative_to(DOCS_ROOT).as_posix()

    if normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    if not normalized:
        return None
    return normalized


def iter_local_hrefs(path: Path) -> list[str]:
    text = FENCED_CODE_BLOCK_RE.sub("", path.read_text())
    hrefs = [match.group("href") for match in MARKDOWN_LINK_RE.finditer(text)]
    hrefs.extend(match.group("href") for match in HTML_HREF_RE.finditer(text))
    return hrefs


def validate_navigation_pages(config: dict[str, object]) -> list[DocsGraphIssue]:
    pages = iter_navigation_pages(config.get("navigation", {}))
    issues: list[DocsGraphIssue] = []

    counts = Counter(pages)
    for page, count in sorted(counts.items()):
        if count > 1:
            issues.append(
                DocsGraphIssue(DOCS_JSON, f"navigation page {page!r} appears {count} times")
            )

    for page in pages:
        if not page_exists(page):
            issues.append(DocsGraphIssue(DOCS_JSON, f"navigation page {page!r} is missing"))

    return issues


def validate_markdown_links(files: list[Path]) -> list[DocsGraphIssue]:
    issues: list[DocsGraphIssue] = []
    for path in files:
        for href in iter_local_hrefs(path):
            local_path = normalized_local_href(href, path)
            if local_path is None:
                continue
            if not page_exists(local_path):
                issues.append(
                    DocsGraphIssue(
                        path,
                        f"local link {href!r} resolves to missing page {local_path!r}",
                    )
                )
    return issues


def validate_redirect_destinations(config: dict[str, object]) -> list[DocsGraphIssue]:
    issues: list[DocsGraphIssue] = []
    redirects = config.get("redirects", [])
    if not isinstance(redirects, list):
        return [DocsGraphIssue(DOCS_JSON, "redirects must be a list")]

    for redirect in redirects:
        if not isinstance(redirect, dict):
            issues.append(DocsGraphIssue(DOCS_JSON, "redirect entry must be an object"))
            continue
        destination = redirect.get("destination")
        if not isinstance(destination, str):
            continue
        parsed = urlparse(destination)
        if parsed.netloc and parsed.netloc != "docs.retab.com":
            continue
        destination_path = parsed.path.removeprefix("/")
        if destination_path and not page_exists(destination_path):
            issues.append(
                DocsGraphIssue(
                    DOCS_JSON,
                    f"redirect destination {destination!r} resolves to missing page",
                )
            )
    return issues


def collect_issues() -> list[DocsGraphIssue]:
    config = load_docs_config()
    files = iter_doc_files()
    issues: list[DocsGraphIssue] = []
    issues.extend(validate_navigation_pages(config))
    issues.extend(validate_markdown_links(files))
    issues.extend(validate_redirect_destinations(config))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    issues = collect_issues()
    if not issues:
        print("docs graph ok")
        return 0

    print(f"{len(issues)} docs graph issue(s):", file=sys.stderr)
    for issue in issues:
        print(f"  {issue.display()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
