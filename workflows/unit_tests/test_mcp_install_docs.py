from pathlib import Path


def test_mcp_recommended_install_includes_cli_bootstrap() -> None:
    docs_root = Path(__file__).resolve().parents[2]
    mcp_page = docs_root / "workflows" / "MCP.mdx"
    source = mcp_page.read_text()

    recommended_snippet_start = source.index("```sh Recommended")
    recommended_snippet_end = source.index("```", recommended_snippet_start + 1)
    recommended_snippet = source[recommended_snippet_start:recommended_snippet_end]

    assert "curl -fsSL https://retab.com/install.sh | sh && retab setup" in recommended_snippet
