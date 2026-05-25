import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[2] / ".scripts" / "lint_snippets.py"
SPEC = importlib.util.spec_from_file_location("lint_snippets", MODULE_PATH)
assert SPEC is not None
lint_snippets = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = lint_snippets
SPEC.loader.exec_module(lint_snippets)


def _snippet(
    language: str,
    code: str,
    raw_language: str | None = None,
    title: str = "",
    source: Path | None = None,
) -> lint_snippets.Snippet:
    return lint_snippets.Snippet(
        source=source or lint_snippets.DOCS_ROOT / "example.mdx",
        index=0,
        start_line=1,
        raw_language=raw_language or language,
        language=language,
        title=title,
        code=code,
    )


def test_required_sdk_group_languages_cover_every_public_sdk() -> None:
    assert lint_snippets.REQUIRED_SDK_GROUP_LANGUAGES == (
        "python",
        "typescript",
        "go",
        "rust",
        "dotnet",
        "php",
        "ruby",
        "java",
    )


def test_code_group_coverage_requires_all_sdk_languages() -> None:
    group = lint_snippets.CodeGroup(
        source=lint_snippets.DOCS_ROOT / "example.mdx",
        component="CodeGroup",
        start_line=1,
        end_line=20,
        snippets=(
            _snippet("python", "from retab import Retab\n", title="Python"),
            _snippet("typescript", 'import { Retab } from "@retab/node";\n', title="TypeScript"),
            _snippet("go", 'import retab "github.com/retab-dev/retab/clients/go"\n', title="Go"),
        ),
    )

    issues = lint_snippets.check_code_group_coverage([group])

    assert len(issues) == 1
    assert issues[0].checker == "coverage"
    assert "Rust" in issues[0].message
    assert ".NET" in issues[0].message
    assert "PHP" in issues[0].message
    assert "Ruby" in issues[0].message
    assert "Java" in issues[0].message


def test_javascript_fence_does_not_satisfy_typescript_coverage() -> None:
    group = lint_snippets.CodeGroup(
        source=lint_snippets.DOCS_ROOT / "example.mdx",
        component="CodeGroup",
        start_line=1,
        end_line=20,
        snippets=(
            _snippet("python", "from retab import Retab\n", title="Python"),
            _snippet("javascript", 'import { Retab } from "@retab/node";\n', raw_language="javascript", title="JavaScript"),
            _snippet("go", 'import retab "github.com/retab-dev/retab/clients/go"\n', title="Go"),
            _snippet("rust", "use retab::Retab;\n", title="Rust"),
            _snippet("dotnet", "using Retab;\n", raw_language="csharp", title="C#"),
            _snippet("php", "use Retab\\Client;\n", title="PHP"),
            _snippet("ruby", "require 'retab'\n", title="Ruby"),
            _snippet("java", "import com.retab.RetabClient;\n", title="Java"),
        ),
    )

    issues = lint_snippets.check_code_group_coverage([group])

    assert len(issues) == 1
    assert "TypeScript" in issues[0].message
    assert "JavaScript tabs do not satisfy TypeScript coverage" in issues[0].message


def test_diagnose_graph_direct_http_sdk_tabs_are_allowed_when_no_sdk_method_exists() -> None:
    source = lint_snippets.DOCS_ROOT / "api-reference/workflows/diagnose-graph.mdx"
    group = lint_snippets.CodeGroup(
        source=source,
        component="CodeGroup",
        start_line=1,
        end_line=20,
        snippets=(
            _snippet(
                "rust",
                'use reqwest::Client;\nClient::new().post("https://api.retab.com/v1/workflows/wf_abc123/diagnose-graph");\n',
                title="Rust",
                source=source,
            ),
        ),
    )

    assert lint_snippets.check_placeholder_sdk_tabs([group]) == []


def test_go_snippet_with_leading_comment_before_package_is_not_wrapped() -> None:
    code = """// Call the API directly.
package main

func main() {}
"""

    assert lint_snippets.normalise_go_snippet(code) == code


def test_go_contextual_fragment_without_import_is_not_linted_as_standalone() -> None:
    snippet = _snippet(
        "go",
        "result, err := client.Extractions.Create(ctx, retab.ExtractionCreateRequest{})\n",
        title="Go",
    )

    assert not lint_snippets.is_self_contained_go(snippet)
    assert lint_snippets.is_contextual_sdk_snippet(snippet)


def test_dotnet_contextual_fragment_without_client_declaration_is_not_standalone() -> None:
    snippet = _snippet(
        "dotnet",
        "using Retab;\n\nvar result = await client.Extractions.CreateAsync(new ExtractionsCreateOptions());\n",
        raw_language="csharp",
        title="C#",
    )

    assert not lint_snippets.is_self_contained_dotnet(snippet)
    assert lint_snippets.is_contextual_sdk_snippet(snippet)


def test_java_fragments_are_checked_as_standalone_snippets() -> None:
    snippet = _snippet(
        "java",
        'var extraction = client.extractions().get("extraction_abc123");\n',
        title="Java",
    )

    assert lint_snippets.is_self_contained_java(snippet)
    assert not lint_snippets.is_contextual_sdk_snippet(snippet)


def test_java_snippet_is_wrapped_in_class() -> None:
    code = "import com.retab.RetabClient;\n\nRetabClient client = new RetabClient(\"test\");\n"

    wrapped = lint_snippets.normalise_java_snippet(code)

    assert "final class Snippet" in wrapped
    assert "public static void main(String[] args) throws Exception" in wrapped
    assert "RetabClient client = new RetabClient" in wrapped


def test_java_public_final_class_is_demoted_for_generated_filename() -> None:
    code = "public final class Example {\n  public static void main(String[] args) {}\n}\n"

    assert lint_snippets.normalise_java_snippet(code).startswith("final class Example")


def test_php_snippet_gets_open_tag_for_syntax_lint() -> None:
    assert lint_snippets.normalise_php_snippet("$client = new Client();\n").startswith(
        "<?php\n"
    )


def test_rust_expression_snippet_is_wrapped_in_async_function() -> None:
    code = "use retab::Retab;\n\nlet client = Retab::new(\"test\");\n"

    wrapped = lint_snippets.normalise_rust_snippet(code)

    assert "async fn snippet()" in wrapped
    assert "let client = Retab::new" in wrapped


def test_workflow_spec_dotnet_examples_pass_required_yaml_definition() -> None:
    for relative_path, option_name in (
        ("api-reference/workflows/spec/validate.mdx", "WorkflowSpecValidateOptions"),
        ("api-reference/workflows/spec/plan.mdx", "WorkflowSpecPlanOptions"),
        ("api-reference/workflows/spec/apply.mdx", "WorkflowSpecApplyOptions"),
    ):
        text = (lint_snippets.DOCS_ROOT / relative_path).read_text()

        assert f"new {option_name}())" not in text
        assert f"new {option_name} {{ YamlDefinition = yamlDefinition }}" in text
        assert "client.WorkflowSpecs." not in text


def test_workflow_yaml_examples_include_metadata_id() -> None:
    for relative_path in (
        "api-reference/workflows/spec/validate.mdx",
        "api-reference/workflows/spec/plan.mdx",
        "api-reference/workflows/spec/apply.mdx",
        "workflows/Workflows.mdx",
    ):
        text = (lint_snippets.DOCS_ROOT / relative_path).read_text()

        assert '"name: invoice workflow\\n"' not in text
        assert "metadata:\n  id:" in text


def test_structural_only_skips_language_toolchains() -> None:
    with patch.object(sys, "argv", ["lint_snippets.py", "--structural-only"]):
        assert lint_snippets.main() == 0
