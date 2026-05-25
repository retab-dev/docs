from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / ".scripts" / "check_docs_graph.py"
SPEC = importlib.util.spec_from_file_location("check_docs_graph", MODULE_PATH)
assert SPEC is not None
check_docs_graph = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = check_docs_graph
SPEC.loader.exec_module(check_docs_graph)


def test_navigation_page_collection_walks_nested_groups() -> None:
    config = {
        "tabs": [
            {
                "groups": [
                    {
                        "pages": [
                            "overview/introduction",
                            {
                                "group": "Nested",
                                "pages": ["api-reference/extractions/create"],
                            },
                        ]
                    }
                ]
            }
        ]
    }

    assert check_docs_graph.iter_navigation_pages(config) == [
        "overview/introduction",
        "api-reference/extractions/create",
    ]


def test_normalized_local_href_resolves_relative_docs_links() -> None:
    source = check_docs_graph.DOCS_ROOT / "overview" / "SDK.mdx"

    assert (
        check_docs_graph.normalized_local_href("../api-reference/introduction", source)
        == "api-reference/introduction"
    )


def test_external_and_anchor_links_are_ignored() -> None:
    source = check_docs_graph.DOCS_ROOT / "overview" / "SDK.mdx"

    assert check_docs_graph.normalized_local_href("https://retab.com", source) is None
    assert check_docs_graph.normalized_local_href("#install", source) is None
    assert check_docs_graph.normalized_local_href("mailto:support@retab.com", source) is None


def test_code_fence_markdown_is_not_treated_as_docs_links(tmp_path: Path) -> None:
    source = tmp_path / "example.mdx"
    source.write_text(
        """# Example

```go
func verify(body []byte, signature string, secret string) {}
```

[Intro](../api-reference/introduction)
"""
    )

    assert check_docs_graph.iter_local_hrefs(source) == ["../api-reference/introduction"]


def test_current_docs_graph_is_consistent() -> None:
    assert [issue.display() for issue in check_docs_graph.collect_issues()] == []
