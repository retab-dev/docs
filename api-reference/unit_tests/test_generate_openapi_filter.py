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
REQUIRED_SNIPPET_LANGUAGES = {
    "python": {"python"},
    "javascript": {"javascript", "typescript", "js", "ts"},
    "go": {"go"},
    "curl": {"bash", "curl", "sh", "shell"},
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
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())
    schemas = generated_openapi["components"]["schemas"]

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


def test_generated_openapi_uses_dereferenced_workflow_artifact_schema() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())
    artifact_get = generated_openapi["paths"][
        "/v1/workflows/artifacts/{artifact_id}"
    ]["get"]
    response_schema = artifact_get["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert response_schema["discriminator"]["propertyName"] == "operation"
    assert {
        "conditional_evaluation",
        "function_invocation",
        "extraction",
    }.issubset(response_schema["discriminator"]["mapping"])
    assert {"$ref": "#/components/schemas/ConditionalEvaluationWorkflowArtifact"} in (
        response_schema["oneOf"]
    )


def test_public_list_get_responses_are_typed() -> None:
    spec = {
        "paths": {
            "/v1/classifications": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaginatedList"
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/v1/workflows": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaginatedList"
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/v1/workflows/runs": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaginatedList"
                                    }
                                }
                            }
                        }
                    }
                }
            },
        },
        "components": {
            "schemas": {
                "ListMetadata": {},
                "Classification": {},
                "Workflow": {},
                "WorkflowRun": {},
            }
        },
    }

    generate_openapi._normalize_workflow_read_docs(spec)

    assert spec["paths"]["/v1/classifications"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ClassificationList"
    }
    assert spec["components"]["schemas"]["ClassificationList"]["properties"]["data"][
        "items"
    ] == {"$ref": "#/components/schemas/Classification"}
    assert spec["paths"]["/v1/workflows"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/WorkflowList"}
    assert spec["components"]["schemas"]["WorkflowList"]["properties"]["data"][
        "items"
    ] == {"$ref": "#/components/schemas/Workflow"}
    assert spec["paths"]["/v1/workflows/runs"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WorkflowRunList"
    }
    assert spec["components"]["schemas"]["WorkflowRunList"]["properties"]["data"][
        "items"
    ] == {"$ref": "#/components/schemas/WorkflowRun"}


def test_review_version_route_uses_semantic_version_id_parameter() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())

    assert "/v1/workflows/reviews/versions/{rvr_id}" not in generated_openapi["paths"]
    route = generated_openapi["paths"][
        "/v1/workflows/reviews/versions/{version_id}"
    ]["get"]
    assert "rvr_id" not in route["operationId"]
    assert route["parameters"][0]["name"] == "version_id"


def test_normalize_public_operation_ids_strips_fastapi_route_suffixes() -> None:
    spec = {
        "paths": {
            "/v1/workflows/reviews": {
                "get": {
                    "operationId": "list_reviews_route_v1_workflows_reviews_get"
                }
            },
            "/v1/workflows/experiments/runs": {
                "post": {
                    "operationId": (
                        "create_experiment_run_flat_v1_workflows_experiments_runs_post"
                    )
                }
            },
            "/v1/workflows/reviews/{review_id}": {
                "get": {
                    "operationId": (
                        "get_review_route_v1_workflows_reviews__review_id__get"
                    )
                }
            },
        }
    }

    generate_openapi._normalize_public_operation_ids(spec)

    assert spec["paths"]["/v1/workflows/reviews"]["get"]["operationId"] == (
        "list_reviews"
    )
    assert spec["paths"]["/v1/workflows/experiments/runs"]["post"][
        "operationId"
    ] == "create_experiment_run"
    assert spec["paths"]["/v1/workflows/reviews/{review_id}"]["get"][
        "operationId"
    ] == "get_review"


def test_normalize_public_operation_ids_rejects_duplicates() -> None:
    spec = {
        "paths": {
            "/v1/widgets": {"get": {"operationId": "list_widgets_v1_widgets_get"}},
            "/v1/other-widgets": {
                "get": {"operationId": "list_widgets_v1_other_widgets_get"}
            },
        }
    }

    with pytest.raises(ValueError, match="Duplicate public OpenAPI operationId"):
        generate_openapi._normalize_public_operation_ids(spec)


def test_normalize_public_schema_names_renames_patch_workflow_requests() -> None:
    spec = {
        "paths": {
            "/v1/workflows/{workflow_id}": {
                "patch": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/PatchWorkflowRequest"
                                }
                            }
                        }
                    }
                }
            },
            "/v1/workflows/blocks/{block_id}": {
                "patch": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/PatchBlockRequest"
                                }
                            }
                        }
                    }
                }
            },
        },
        "components": {
            "schemas": {
                "PatchWorkflowRequest": {"title": "PatchWorkflowRequest"},
                "PatchBlockRequest": {"title": "PatchBlockRequest"},
            }
        },
    }

    generate_openapi._normalize_public_schema_names(spec)

    schemas = spec["components"]["schemas"]
    assert "PatchWorkflowRequest" not in schemas
    assert "PatchBlockRequest" not in schemas
    assert schemas["UpdateWorkflowRequest"]["title"] == "UpdateWorkflowRequest"
    assert schemas["UpdateWorkflowBlockRequest"]["title"] == (
        "UpdateWorkflowBlockRequest"
    )
    assert spec["paths"]["/v1/workflows/{workflow_id}"]["patch"]["requestBody"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UpdateWorkflowRequest"
    }
    assert spec["paths"]["/v1/workflows/blocks/{block_id}"]["patch"]["requestBody"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UpdateWorkflowBlockRequest"
    }


def test_api_reference_pages_have_all_code_snippets() -> None:
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
            snippet_name
            for snippet_name, aliases in REQUIRED_SNIPPET_LANGUAGES.items()
            if languages.isdisjoint(aliases)
        ]
        if missing:
            page = markdown_file.relative_to(DOCS_ROOT).with_suffix("").as_posix()
            missing_by_page[page] = missing

    assert missing_by_page == {}, json.dumps(missing_by_page, indent=2, sort_keys=True)


def test_api_reference_openapi_frontmatter_omits_query_strings() -> None:
    offenders: dict[str, str] = {}

    for markdown_file in generate_openapi._list_api_reference_markdown_files(
        REAL_DOCS_JSON,
        DOCS_ROOT,
    ):
        match = generate_openapi.OPENAPI_FRONTMATTER_RE.search(
            markdown_file.read_text()
        )
        if match is None:
            continue
        path = match.group("path")
        if "?" in path:
            page = markdown_file.relative_to(DOCS_ROOT).with_suffix("").as_posix()
            offenders[page] = path

    assert offenders == {}, json.dumps(offenders, indent=2, sort_keys=True)


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


def test_generated_workflow_lists_use_typed_paginated_schemas() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())

    workflow_list = generated_openapi["paths"]["/v1/workflows"]["get"]
    run_list = generated_openapi["paths"]["/v1/workflows/runs"]["get"]

    assert workflow_list["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/WorkflowList"}
    assert run_list["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/WorkflowRunList"}
    assert generated_openapi["components"]["schemas"]["WorkflowList"]["properties"][
        "data"
    ]["items"] == {"$ref": "#/components/schemas/Workflow"}
    assert generated_openapi["components"]["schemas"]["WorkflowRunList"]["properties"][
        "data"
    ]["items"] == {
        "$ref": "#/components/schemas/WorkflowRun"
    }


def test_generated_list_responses_use_public_schema_names() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())
    schemas = generated_openapi["components"]["schemas"]
    expected_lists = {
        "/v1/classifications": (
            "ClassificationList",
            "Classification",
        ),
        "/v1/edits": ("EditList", "Edit"),
        "/v1/edits/templates": ("EditTemplateList", "EditTemplate"),
        "/v1/extractions": ("ExtractionList", "Extraction"),
        "/v1/files": ("FileList", "File"),
        "/v1/jobs": ("JobList", "JobListItem"),
        "/v1/parses": ("ParseList", "Parse"),
        "/v1/partitions": ("PartitionList", "Partition"),
        "/v1/splits": ("SplitList", "Split"),
        "/v1/workflows": ("WorkflowList", "Workflow"),
        "/v1/workflows/artifacts": ("WorkflowArtifactList", "WorkflowArtifact"),
        "/v1/workflows/blocks": ("WorkflowBlockList", "WorkflowBlock"),
        "/v1/workflows/edges": ("WorkflowEdgeList", "WorkflowEdge"),
        "/v1/workflows/experiments": ("WorkflowExperimentList", "WorkflowExperiment"),
        (
            "/v1/workflows/experiments/results"
        ): ("WorkflowExperimentResultList", "WorkflowExperimentResult"),
        (
            "/v1/workflows/experiments/runs"
        ): ("WorkflowExperimentRunList", "WorkflowExperimentRun"),
        "/v1/workflows/reviews": ("WorkflowReviewList", "WorkflowReview"),
        (
            "/v1/workflows/reviews/versions"
        ): ("WorkflowReviewVersionList", "WorkflowReviewVersion"),
        "/v1/workflows/runs": ("WorkflowRunList", "WorkflowRun"),
        "/v1/workflows/blocks/executions": (
            "BlockExecutionList",
            "BlockExecution",
        ),
        "/v1/workflows/steps": ("WorkflowStepList", "WorkflowStep"),
        "/v1/workflows/tests": ("WorkflowTestList", "WorkflowTest"),
        "/v1/workflows/tests/results": ("WorkflowTestResultList", "WorkflowTestResult"),
        "/v1/workflows/tests/runs": ("WorkflowTestRunList", "WorkflowTestRun"),
    }

    for path, (list_schema_name, item_schema_name) in expected_lists.items():
        response_schema = generated_openapi["paths"][path]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        assert response_schema == {
            "$ref": f"#/components/schemas/{list_schema_name}"
        }
        assert schemas[list_schema_name]["properties"]["data"]["items"] == {
            "$ref": f"#/components/schemas/{item_schema_name}"
        }

    assert "PaginatedList" not in schemas
    assert not any(name.startswith("PaginatedList_") for name in schemas)
    assert "JobListResponse" not in schemas


def test_generated_review_version_docs_use_public_version_id_and_actor() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())

    assert "/v1/workflows/reviews/versions/{rvr_id}" not in generated_openapi["paths"]
    review_version_get = generated_openapi["paths"][
        "/v1/workflows/reviews/versions/{version_id}"
    ]["get"]
    assert review_version_get["parameters"][0]["name"] == "version_id"
    assert review_version_get["parameters"][0]["schema"]["title"] == "Version Id"
    assert generated_openapi["components"]["schemas"]["WorkflowReviewVersion"][
        "properties"
    ]["author"] == {
        "$ref": "#/components/schemas/Actor",
        "description": "Actor that created the version.",
    }


def test_generated_review_docs_use_public_review_id_path_parameter() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())

    assert "/v1/workflows/reviews/{id}" not in generated_openapi["paths"]
    assert "/v1/workflows/reviews/{id}/approve" not in generated_openapi["paths"]
    assert "/v1/workflows/reviews/{id}/reject" not in generated_openapi["paths"]

    review_get = generated_openapi["paths"]["/v1/workflows/reviews/{review_id}"][
        "get"
    ]
    assert review_get["parameters"][0]["name"] == "review_id"
    assert review_get["parameters"][0]["schema"]["title"] == "Review Id"


def test_generated_public_operation_ids_are_stable() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())

    operation_ids: list[str] = []
    for path_item in generated_openapi["paths"].values():
        for method, operation in path_item.items():
            if method not in generate_openapi.OPENAPI_HTTP_METHODS:
                continue
            operation_ids.append(operation["operationId"])

    assert len(operation_ids) == len(set(operation_ids))
    assert [
        operation_id
        for operation_id in operation_ids
        if "_v1_" in operation_id
        or "__" in operation_id
        or operation_id.endswith("_route")
        or operation_id.endswith("_flat")
    ] == []


def test_generated_paginated_list_routes_keep_pagination_query_params() -> None:
    """Regression: paginated GET routes must publish their cursor params.

    The post-processor previously stripped ``before``/``after``/``limit``/
    ``order`` from a handful of list endpoints (e.g. GET
    ``/v1/workflows/experiments``) even though FastAPI declared them on the
    route. The published spec then advertised a single positional filter and
    no pagination knobs, which broke SDK pagination generation downstream.
    """
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())

    expected_params_by_path = {
        "/v1/workflows/experiments": {
            "workflow_id",
            "before",
            "after",
            "limit",
            "order",
        },
        "/v1/workflows/blocks/executions": {
            "run_id",
            "block_id",
            "before",
            "after",
            "limit",
            "order",
        },
        "/v1/workflows/tests": {
            "workflow_id",
            "target_block_id",
            "before",
            "after",
            "limit",
            "order",
        },
    }

    missing: dict[str, list[str]] = {}
    for path, expected in expected_params_by_path.items():
        op = generated_openapi["paths"][path]["get"]
        actual = {param["name"] for param in op.get("parameters", [])}
        missing_here = sorted(expected - actual)
        if missing_here:
            missing[path] = missing_here

    assert missing == {}, json.dumps(missing, indent=2, sort_keys=True)


def test_generated_experiment_metrics_use_kind_discriminator_and_public_flows() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())

    metrics_schema = generated_openapi["paths"][
        "/v1/workflows/experiments/metrics"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

    assert metrics_schema["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "summary": "#/components/schemas/ExperimentSummaryMetricsResponse",
            "by_document": "#/components/schemas/ExperimentByDocumentMetricsResponse",
            "by_target": "#/components/schemas/ExperimentByTargetMetricsResponse",
            "votes": "#/components/schemas/ExperimentVotesMetricsResponse",
            "stale_metrics": "#/components/schemas/ExperimentMetricsStaleError",
            "no_metrics": "#/components/schemas/ExperimentMetricsMissingError",
        },
    }
    schemas = generated_openapi["components"]["schemas"]
    for schema_name, kind in {
        "ExperimentSummaryMetricsResponse": "summary",
        "ExperimentByDocumentMetricsResponse": "by_document",
        "ExperimentByTargetMetricsResponse": "by_target",
        "ExperimentVotesMetricsResponse": "votes",
        "ExperimentMetricsStaleError": "stale_metrics",
        "ExperimentMetricsMissingError": "no_metrics",
    }.items():
        assert schemas[schema_name]["properties"]["kind"]["const"] == kind

    flow_schema = schemas["ExperimentConfusionFlowMetric"]
    assert flow_schema["required"] == ["source", "target", "score"]
    assert set(flow_schema["properties"]) == {"source", "target", "score"}


def test_generated_workflow_assertion_component_names_are_language_neutral() -> None:
    generated_openapi = json.loads(GENERATED_OPENAPI.read_text())
    schemas = generated_openapi["components"]["schemas"]
    serialized_spec = json.dumps(generated_openapi)

    for schema_name in (
        "AssertionSpec-Input",
        "AssertionSpec-Output",
        "AllItemsMatchCondition-Input",
        "AllItemsMatchCondition-Output",
        "AnyItemMatchesCondition-Input",
        "AnyItemMatchesCondition-Output",
    ):
        assert schema_name not in schemas
        assert f"#/components/schemas/{schema_name}" not in serialized_spec

    for schema_name in (
        "AssertionSpec",
        "AllItemsMatchCondition",
        "AnyItemMatchesCondition",
    ):
        assert schema_name in schemas
