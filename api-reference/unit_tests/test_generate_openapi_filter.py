import importlib.util
import json
import re
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "generate_openapi.py"
SPEC = importlib.util.spec_from_file_location("generate_openapi", MODULE_PATH)
assert SPEC is not None
generate_openapi = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(generate_openapi)

DOCS_ROOT = Path(__file__).resolve().parents[2]
REAL_DOCS_JSON = DOCS_ROOT / "docs.json"
SDK_SNIPPET_LANGUAGES = {
    "python": {"python"},
    "javascript": {"javascript", "typescript", "js", "ts"},
    "go": {"go"},
}
GENERATED_OPENAPI = DOCS_ROOT / "api-reference" / "openapi.json"


def _code_fence_languages(markdown: str) -> set[str]:
    languages: set[str] = set()
    for match in re.finditer(r"^```(?P<label>[^\n`]*)", markdown, re.MULTILINE):
        label = match.group("label").strip().lower()
        if not label:
            continue
        languages.update(label.split())
    return languages


def _write_docs_tree(tmp_path: Path) -> tuple[Path, Path]:
    docs_root = tmp_path / "docs"
    api_reference = docs_root / "api-reference"
    (api_reference / "widgets").mkdir(parents=True)
    (api_reference / "nested").mkdir(parents=True)

    (docs_root / "docs.json").write_text(
        json.dumps(
            {
                "navigation": [
                    {
                        "pages": [
                            "api-reference/introduction",
                            "api-reference/widgets/list",
                            {
                                "group": "Nested",
                                "pages": ["api-reference/nested/create"],
                            },
                            "workflows/Workflows",
                        ]
                    }
                ]
            }
        )
    )
    (api_reference / "introduction.mdx").write_text("# API Reference\n")
    (api_reference / "widgets" / "list.mdx").write_text(
        '---\nopenapi: "GET /v1/widgets?limit={limit}"\n---\n'
    )
    (api_reference / "nested" / "create.mdx").write_text(
        '---\nopenapi: "POST /v1/widgets/{widget_id}/nested"\n---\n'
    )
    return docs_root / "docs.json", docs_root


def test_lists_api_reference_markdown_files_from_docs_json(tmp_path: Path) -> None:
    docs_json_path, docs_root = _write_docs_tree(tmp_path)

    files = generate_openapi._list_api_reference_markdown_files(
        docs_json_path,
        docs_root,
    )

    assert [path.relative_to(docs_root).as_posix() for path in files] == [
        "api-reference/introduction.mdx",
        "api-reference/widgets/list.mdx",
        "api-reference/nested/create.mdx",
    ]


def test_collects_openapi_fields_from_referenced_markdown(tmp_path: Path) -> None:
    docs_json_path, docs_root = _write_docs_tree(tmp_path)

    routes = generate_openapi._collect_api_reference_openapi_routes(
        generate_openapi._list_api_reference_markdown_files(docs_json_path, docs_root)
    )

    assert routes == {
        ("get", "/v1/widgets"),
        ("post", "/v1/widgets/{widget_id}/nested"),
    }


def test_strips_routes_not_listed_in_docs_markdown(tmp_path: Path) -> None:
    docs_json_path, docs_root = _write_docs_tree(tmp_path)
    spec = {
        "paths": {
            "/v1/widgets": {
                "get": {"operationId": "list_widgets"},
                "post": {"operationId": "create_widget"},
            },
            "/v1/widgets/{widget_id}/nested": {
                "post": {"operationId": "create_nested_widget"},
            },
            "/v1/internal-only": {
                "get": {"operationId": "internal_only"},
            },
        }
    }

    generate_openapi._strip_routes_not_in_api_reference_markdown(
        spec,
        docs_json_path,
        docs_root,
    )

    assert spec["paths"] == {
        "/v1/widgets": {"get": {"operationId": "list_widgets"}},
        "/v1/widgets/{widget_id}/nested": {
            "post": {"operationId": "create_nested_widget"}
        },
    }


def test_missing_docs_json_api_reference_page_fails(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    (docs_root / "docs.json").write_text(
        json.dumps({"navigation": ["api-reference/missing"]})
    )

    with pytest.raises(FileNotFoundError, match="api-reference/missing.mdx"):
        generate_openapi._list_api_reference_markdown_files(
            docs_root / "docs.json",
            docs_root,
        )


def test_workflow_create_request_docs_publish_shape_variants() -> None:
    spec = {
        "components": {
            "schemas": {
                "CreateWorkflowRunRequest": {
                    "properties": {
                        "workflow_id": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "title": "Workflow Id",
                        },
                        "documents": {"type": "array", "items": {"type": "object"}},
                        "json_inputs": {"type": "object"},
                        "version": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                        "restart_of": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                        "config_source": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                        "command_id": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                    }
                },
                "CreateWorkflowTestRunRequest": {
                    "properties": {
                        "workflow_id": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                        "test_id": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                        "target": {
                            "anyOf": [
                                {"$ref": "#/components/schemas/WorkflowTestTarget"},
                                {"type": "null"},
                            ]
                        },
                        "n_consensus": {
                            "anyOf": [{"type": "integer"}, {"type": "null"}]
                        },
                    }
                },
            }
        }
    }

    generate_openapi._hard_cutover_workflow_create_request_shapes(spec)

    schemas = spec["components"]["schemas"]
    assert schemas["CreateWorkflowRunRequest"]["oneOf"] == [
        {"$ref": "#/components/schemas/CreateFreshWorkflowRunRequest"},
        {"$ref": "#/components/schemas/CreateRestartWorkflowRunRequest"},
    ]
    assert schemas["CreateFreshWorkflowRunRequest"]["required"] == ["workflow_id"]
    assert schemas["CreateFreshWorkflowRunRequest"]["properties"]["workflow_id"] == {
        "type": "string",
        "title": "Workflow Id",
        "description": "Workflow id for the fresh run.",
    }
    assert schemas["CreateRestartWorkflowRunRequest"]["required"] == [
        "restart_of",
        "config_source",
    ]
    assert schemas["CreateWorkflowTestRunRequest"]["oneOf"] == [
        {"$ref": "#/components/schemas/CreateWorkflowTestRunForTestRequest"},
        {"$ref": "#/components/schemas/CreateWorkflowTestRunForTargetRequest"},
        {"$ref": "#/components/schemas/CreateWorkflowTestRunAllRequest"},
    ]
    assert schemas["CreateWorkflowTestRunForTestRequest"]["required"] == ["test_id"]
    assert schemas["CreateWorkflowTestRunForTargetRequest"]["required"] == [
        "workflow_id",
        "target",
    ]
    assert schemas["CreateWorkflowTestRunAllRequest"]["required"] == ["workflow_id"]


def test_generated_openapi_uses_named_workflow_artifact_record_schema() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())
    artifact_get = generated_openapi["paths"][
        "/v1/workflows/artifacts/{artifact_id}"
    ]["get"]
    response_schema = artifact_get["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert response_schema == {"$ref": "#/components/schemas/WorkflowArtifactRecord"}
    artifact_schema = generated_openapi["components"]["schemas"][
        "WorkflowArtifactRecord"
    ]
    assert artifact_schema["required"] == ["operation", "id"]
    assert artifact_schema["properties"]["operation"]["enum"]
    assert artifact_schema["additionalProperties"] is True


def test_api_reference_pages_have_all_sdk_snippets() -> None:
    missing_by_page: dict[str, list[str]] = {}

    for markdown_file in generate_openapi._list_api_reference_markdown_files(
        REAL_DOCS_JSON,
        DOCS_ROOT,
    ):
        markdown = markdown_file.read_text()
        if generate_openapi.OPENAPI_FRONTMATTER_RE.search(markdown) is None:
            continue

        languages = _code_fence_languages(markdown)
        missing = [
            sdk_name
            for sdk_name, aliases in SDK_SNIPPET_LANGUAGES.items()
            if languages.isdisjoint(aliases)
        ]
        if missing:
            page = markdown_file.relative_to(DOCS_ROOT).with_suffix("").as_posix()
            missing_by_page[page] = missing

    assert missing_by_page == {}, json.dumps(missing_by_page, indent=2, sort_keys=True)


def test_generated_openapi_routes_match_docs_json_markdown_openapi_fields() -> None:
    markdown_files = generate_openapi._list_api_reference_markdown_files(
        REAL_DOCS_JSON,
        DOCS_ROOT,
    )
    docs_routes = generate_openapi._collect_api_reference_openapi_routes(markdown_files)
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())

    spec_routes: set[tuple[str, str]] = set()
    for path, path_item in generated_openapi["paths"].items():
        for method in path_item:
            if method in generate_openapi.OPENAPI_HTTP_METHODS:
                spec_routes.add((method, path))

    assert spec_routes == docs_routes
