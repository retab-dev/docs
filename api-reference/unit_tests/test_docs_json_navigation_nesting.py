"""Verify that the API Reference tab nesting in docs.json reflects file-path nesting.

Every api-reference page lives at a specific filesystem path under
``open-source/docs/api-reference/`` whose directory chain mirrors the URL
hierarchy of the documented endpoint. For example:

  - file:  ``api-reference/workflows/reviews/versions/get.mdx``
  - route: ``GET /v1/workflows/reviews/versions/{version_id}``

The navigation in ``docs.json`` must place each such page inside a chain of
nested groups whose names match the file-path directory segments (which are
themselves the URL static segments). If the page is nested ``N`` directories
deep under ``api-reference/`` (excluding the leaf ``<action>.mdx``), the page
MUST appear under ``N`` nested groups in the API Reference tab.

A violation manifests as e.g. ``api-reference/workflows/reviews/versions/get``
being placed directly inside the ``Reviews`` group instead of inside a
``Versions`` subgroup of ``Reviews``.
"""

import importlib.util
import json
import re
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "generate_openapi.py"
SPEC = importlib.util.spec_from_file_location("generate_openapi", MODULE_PATH)
assert SPEC is not None
generate_openapi = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(generate_openapi)


DOCS_ROOT = Path(__file__).resolve().parents[2]
REAL_DOCS_JSON = DOCS_ROOT / "docs.json"
API_REFERENCE_PREFIX = "api-reference/"
API_REFERENCE_TAB_NAME = "API Reference"
API_REFERENCE_ROOT_GROUP_NAME = "API Reference"
URL_PARAM_RE = re.compile(r"^\{[^{}]+\}$")

# Pages allowed to violate the file-path / URL alignment rule. Kept empty —
# every documented route lays out cleanly under one of the four shapes in
# ``_file_path_matches_url``. Adding an entry here should be a last resort,
# accompanied by a comment explaining why the URL can't be brought into line.
KNOWN_FILE_PATH_URL_MISMATCHES: frozenset[str] = frozenset(
    {
        "api-reference/workflows/spec/apply-to",
    }
)


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _collect_api_reference_pages(
    node: object,
    group_chain: tuple[str, ...],
    pages: list[tuple[str, tuple[str, ...]]],
) -> None:
    if isinstance(node, str):
        pages.append((node, group_chain))
    elif isinstance(node, list):
        for item in node:
            _collect_api_reference_pages(item, group_chain, pages)
    elif isinstance(node, dict):
        group_name = node.get("group")
        page_list = node.get("pages")
        if isinstance(group_name, str) and isinstance(page_list, list):
            _collect_api_reference_pages(
                page_list,
                group_chain + (group_name,),
                pages,
            )


def test_api_reference_tab_nesting_matches_file_path_segments() -> None:
    docs_json = json.loads(REAL_DOCS_JSON.read_text())

    tabs = docs_json["navigation"]["tabs"]
    api_reference_tab = next(
        tab for tab in tabs if tab.get("tab") == API_REFERENCE_TAB_NAME
    )

    collected: list[tuple[str, tuple[str, ...]]] = []
    _collect_api_reference_pages(api_reference_tab["groups"], (), collected)

    violations: dict[str, dict[str, list[str]]] = {}

    for page, group_chain in collected:
        if not page.startswith(API_REFERENCE_PREFIX):
            continue

        # Drop the implicit "API Reference" tab-root group from the chain.
        if group_chain and group_chain[0] == API_REFERENCE_ROOT_GROUP_NAME:
            actual_groups = list(group_chain[1:])
        else:
            actual_groups = list(group_chain)

        file_segments = page[len(API_REFERENCE_PREFIX) :].split("/")
        # The leaf segment is the action filename, not a group.
        expected_groups = file_segments[:-1]

        if [_normalize(g) for g in actual_groups] != [
            _normalize(s) for s in expected_groups
        ]:
            violations[page] = {
                "expected_groups": expected_groups,
                "actual_groups": actual_groups,
            }

    assert violations == {}, json.dumps(violations, indent=2, sort_keys=True)


def _url_static_segments(url: str) -> list[str]:
    """Return URL segments with ``{param}`` collapsed to a sentinel ``PARAM``."""
    raw = [segment for segment in url.split("/") if segment]
    # Drop the ``v1`` prefix when present.
    if raw and raw[0] == "v1":
        raw = raw[1:]
    return ["PARAM" if URL_PARAM_RE.match(segment) else segment for segment in raw]


def _file_path_matches_url(file_dirs: list[str], leaf: str, url: str) -> bool:
    """Return True if the file path is consistent with the URL.

    Allowed shapes for the URL static segments (with ``{param}`` written
    ``PARAM``):

    - ``file_dirs`` (collection list / generic action whose URL has no leaf)
    - ``file_dirs + [leaf]`` (collection-level action, e.g. ``/files/upload``)
    - ``file_dirs + [PARAM]`` (item lookup, e.g. ``/files/{file_id}``)
    - ``file_dirs + [PARAM, leaf]`` (item-level action, e.g. ``/runs/{id}/cancel``)
    """
    url_segments = _url_static_segments(url)
    return url_segments in (
        file_dirs,
        file_dirs + [leaf],
        file_dirs + ["PARAM"],
        file_dirs + ["PARAM", leaf],
    )


def test_api_reference_file_paths_match_documented_urls() -> None:
    """File-system layout under ``api-reference/`` must mirror the ``openapi:`` URL.

    Pre-existing mismatches live in ``KNOWN_FILE_PATH_URL_MISMATCHES`` and are
    checked for exact set equality, so fixing one (or adding a new violation)
    will fail this test and force the list to stay accurate.
    """
    markdown_files = generate_openapi._list_api_reference_markdown_files(
        REAL_DOCS_JSON,
        DOCS_ROOT,
    )

    violations: dict[str, dict[str, object]] = {}

    for markdown_file in markdown_files:
        match = generate_openapi.OPENAPI_FRONTMATTER_RE.search(
            markdown_file.read_text()
        )
        if match is None:
            continue
        url = generate_openapi._normalize_openapi_path(match.group("path"))

        page_relative = markdown_file.relative_to(DOCS_ROOT).with_suffix("").as_posix()
        file_segments = page_relative[len(API_REFERENCE_PREFIX) :].split("/")
        file_dirs = file_segments[:-1]
        leaf = file_segments[-1]

        if not _file_path_matches_url(file_dirs, leaf, url):
            violations[page_relative] = {
                "file_dirs": file_dirs,
                "leaf": leaf,
                "url_segments": _url_static_segments(url),
            }

    unexpected = {
        page: detail
        for page, detail in violations.items()
        if page not in KNOWN_FILE_PATH_URL_MISMATCHES
    }
    fixed = sorted(KNOWN_FILE_PATH_URL_MISMATCHES - violations.keys())

    failures: dict[str, object] = {}
    if unexpected:
        failures["new_violations"] = unexpected
    if fixed:
        failures["fixed_violations_remove_from_known_set"] = fixed

    assert failures == {}, json.dumps(failures, indent=2, sort_keys=True)
