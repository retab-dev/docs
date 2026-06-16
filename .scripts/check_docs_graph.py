#!/usr/bin/env python3
"""Validate local docs graph invariants.

This is intentionally a local graph check, not a Mintlify replacement. It
checks the pieces Bazel should be able to reason about cheaply:

- every page listed in docs.json exists
- duplicate docs.json page entries are reported
- local markdown and JSX href links resolve to checked-in docs pages
- internal redirect destinations resolve to checked-in docs pages
- API reference pages with `openapi:` frontmatter resolve to public OpenAPI
  routes, unless they are explicitly documented as docs-only API pages
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DOCS_ROOT = Path(__file__).resolve().parents[1]
DOCS_JSON = DOCS_ROOT / "docs.json"
OPENAPI_JSON = DOCS_ROOT / "api-reference" / "openapi.json"

DOC_EXTENSIONS = (".mdx", ".md")
DOC_FILE_GLOBS = ("**/*.mdx", "**/*.md")
IGNORED_PATH_PARTS = {".snippets", ".pytest_cache"}
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?P<href>[^)]+)\)")
HTML_HREF_RE = re.compile(r"href=[\"'](?P<href>[^\"']+)[\"']")
FENCED_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
OPENAPI_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?:(?!^---$).)*?^openapi:\s*[\"']?"
    r"(?P<method>[A-Z]+)\s+(?P<path>/[^\"'\n]+)[\"']?\s*$",
    re.MULTILINE | re.DOTALL,
)
OPENAPI_HTTP_METHODS = {
    "delete",
    "get",
    "patch",
    "post",
    "put",
}
DOCS_ONLY_API_REFERENCE_ROUTES: set[tuple[str, str]] = {
    ("post", "/v1/workflows/{workflow_id}/diagnose-graph"),
}


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


def page_file(page: str, root: Path = DOCS_ROOT) -> Path:
    for candidate in page_candidates(page, root):
        if candidate.exists():
            return candidate
    return page_candidates(page, root)[0]


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


def _normalize_openapi_path(path: str) -> str:
    return path.split("?", 1)[0]


def _collect_public_openapi_routes(openapi_path: Path) -> set[tuple[str, str]]:
    spec = json.loads(openapi_path.read_text())
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return set()

    routes: set[tuple[str, str]] = set()
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method in path_item:
            if method in OPENAPI_HTTP_METHODS:
                routes.add((method, path))
    return routes


def resolve_input_path(path: str | Path) -> Path:
    value = Path(path)
    candidates: list[Path] = []
    if value.is_absolute():
        candidates.append(value)
    else:
        repo_root = DOCS_ROOT.parents[1]
        candidates.extend(
            [
                DOCS_ROOT / value,
                repo_root / value,
                repo_root / "bazel-bin" / value,
                Path.cwd() / value,
            ]
        )
        test_srcdir = os.environ.get("TEST_SRCDIR", "")
        test_workspace = os.environ.get("TEST_WORKSPACE", "")
        if test_srcdir and test_workspace:
            candidates.append(Path(test_srcdir) / test_workspace / value)
        if test_srcdir:
            candidates.append(Path(test_srcdir) / "_main" / value)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return value.resolve()


def default_openapi_path() -> Path:
    override = os.environ.get("RETAB_DOCS_OPENAPI_JSON")
    if override:
        return resolve_input_path(override)
    return OPENAPI_JSON


def validate_api_reference_routes_match_public_openapi(
    config: dict[str, object],
    root: Path = DOCS_ROOT,
    openapi_path: Path | None = None,
) -> list[DocsGraphIssue]:
    issues: list[DocsGraphIssue] = []
    if openapi_path is None:
        openapi_path = default_openapi_path()
    public_routes = _collect_public_openapi_routes(openapi_path)

    for page in iter_navigation_pages(config.get("navigation", {})):
        if not page.startswith("api-reference/") or not page_exists(page, root):
            continue
        markdown_file = page_file(page, root)
        match = OPENAPI_FRONTMATTER_RE.search(markdown_file.read_text())
        if match is None:
            continue

        method = match.group("method").lower()
        path = _normalize_openapi_path(match.group("path"))
        route = (method, path)
        if route in public_routes or route in DOCS_ONLY_API_REFERENCE_ROUTES:
            continue
        route_label = f"{method.upper()} {path}"
        issues.append(
            DocsGraphIssue(
                markdown_file,
                f"API reference page {page!r} documents "
                f"{route_label!r}, but that route is not present in the "
                "public OpenAPI spec",
            )
        )

    return issues


def collect_issues(openapi_path: Path | None = None) -> list[DocsGraphIssue]:
    config = load_docs_config()
    files = iter_doc_files()
    issues: list[DocsGraphIssue] = []
    issues.extend(validate_navigation_pages(config))
    issues.extend(validate_markdown_links(files))
    issues.extend(validate_redirect_destinations(config))
    issues.extend(
        validate_api_reference_routes_match_public_openapi(
            config,
            openapi_path=openapi_path,
        )
    )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openapi", default=None)
    args = parser.parse_args()

    issues = collect_issues(resolve_input_path(args.openapi) if args.openapi else None)
    if not issues:
        print("docs graph ok")
        return 0

    print(f"{len(issues)} docs graph issue(s):", file=sys.stderr)
    for issue in issues:
        print(f"  {issue.display()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
