import json
import re
import sys
from copy import deepcopy
from pathlib import Path


LEGACY_DOCUMENT_PATH_PREFIX = "/v1/documents/"
LEGACY_REVIEW_DECISION_PATH_PREFIX = (
    "/v1/workflows/runs/{run_id}/" + "h" + "il-decisions"
)
PRIVATE_PATH_PREFIXES: tuple[str, ...] = (
    "/internal/",
    "/custom/",
)
DIAGNOSTIC_PATH_SUFFIXES: tuple[str, ...] = (
    "/stress-test",
    "/benchmark",
)

LEGACY_EDIT_PATHS: set[str] = {
    "/v1/edit/agent/fill",
    "/v1/edit/agent/edits",
    "/v1/edit/agent/edits/count",
    "/v1/edit/agent/edits/{edit_id}",
    "/v1/edit/templates",
    "/v1/edit/templates/count",
    "/v1/edit/templates/{template_id}",
    "/v1/edit/templates/{template_id}/duplicate",
    "/v1/edit/templates/{template_id}/empty-form",
    "/v1/edit/templates/fill",
    "/v1/edit/templates/generate",
    "/v1/edit/templates/infer_form_bounding_boxes",
    "/v1/edit/templates/edits",
    "/v1/edit/templates/edits/count",
    "/v1/edit/templates/edits/{edit_id}",
}

LEGACY_SCHEMA_NAMES: set[str] = {
    "ClassifyRequest",
    "ClassifyResponse",
    "H" + "IL" + "DecisionResource",
    "Submit" + "H" + "IL" + "DecisionRequest",
    "Submit" + "H" + "IL" + "DecisionResponse",
}
REVIEW_DECISION_STATUS_VALUES = ["pending", "approved", "rejected", "decided", "all"]
OPENAPI_HTTP_METHODS = {
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
}
OPENAPI_FRONTMATTER_RE = re.compile(
    r"^openapi:\s*[\"'](?P<method>[A-Z]+)\s+(?P<path>[^\"']+)[\"']",
    re.MULTILINE,
)


def _collect_schema_refs(node: object, refs: set[str]) -> None:
    """Collect component schema refs from an OpenAPI subtree."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            refs.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _collect_schema_refs(value, refs)
    elif isinstance(node, list):
        for item in node:
            _collect_schema_refs(item, refs)


def _prune_unreferenced_schemas(spec: dict[str, object]) -> None:
    components = spec.get("components")
    if not isinstance(components, dict):
        return

    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return

    reachable_schema_names: set[str] = set()

    # Seed reachability from the public API surface and non-schema components.
    _collect_schema_refs(spec.get("paths"), reachable_schema_names)
    for component_name, component_value in components.items():
        if component_name == "schemas":
            continue
        _collect_schema_refs(component_value, reachable_schema_names)

    # Expand transitively through referenced schemas.
    queue = list(reachable_schema_names)
    while queue:
        schema_name = queue.pop()
        schema = schemas.get(schema_name)
        if not isinstance(schema, dict):
            continue
        nested_refs: set[str] = set()
        _collect_schema_refs(schema, nested_refs)
        for nested_ref in nested_refs:
            if nested_ref not in reachable_schema_names:
                reachable_schema_names.add(nested_ref)
                queue.append(nested_ref)

    for schema_name in list(schemas.keys()):
        if schema_name not in reachable_schema_names:
            schemas.pop(schema_name, None)


def _iter_docs_json_pages(node: object) -> list[str]:
    pages: list[str] = []
    if isinstance(node, str):
        pages.append(node)
    elif isinstance(node, list):
        for item in node:
            pages.extend(_iter_docs_json_pages(item))
    elif isinstance(node, dict):
        for value in node.values():
            pages.extend(_iter_docs_json_pages(value))
    return pages


def _list_api_reference_markdown_files(
    docs_json_path: Path,
    docs_root: Path,
) -> list[Path]:
    """List api-reference markdown files that are explicitly present in docs.json."""
    docs_json = json.loads(docs_json_path.read_text())
    seen_pages: set[str] = set()
    markdown_files: list[Path] = []

    for page in _iter_docs_json_pages(docs_json):
        if not page.startswith("api-reference/") or page in seen_pages:
            continue
        seen_pages.add(page)
        markdown_file = docs_root / f"{page}.mdx"
        if not markdown_file.exists():
            raise FileNotFoundError(
                f"docs.json references missing API reference page: {markdown_file}"
            )
        markdown_files.append(markdown_file)

    return markdown_files


def _normalize_openapi_path(path: str) -> str:
    """Normalize a markdown openapi path to the OpenAPI paths-map key."""
    return path.split("?", 1)[0]


def _collect_api_reference_openapi_routes(
    markdown_files: list[Path],
) -> set[tuple[str, str]]:
    """Read each referenced markdown file and collect its `openapi:` route."""
    routes: set[tuple[str, str]] = set()
    for markdown_file in markdown_files:
        match = OPENAPI_FRONTMATTER_RE.search(markdown_file.read_text())
        if match is None:
            continue
        method = match.group("method").lower()
        path = _normalize_openapi_path(match.group("path"))
        if method not in OPENAPI_HTTP_METHODS:
            raise ValueError(
                f"{markdown_file} has unsupported OpenAPI method {method!r}"
            )
        routes.add((method, path))
    return routes


def _strip_routes_not_in_api_reference_markdown(
    spec: dict[str, object],
    docs_json_path: Path,
    docs_root: Path,
) -> None:
    """Keep only operations documented by api-reference markdown in docs.json.

    The docs navigation is the source of truth for the public API reference:
    first list all `api-reference/...` markdown pages from docs.json, then read
    each page's `openapi: "METHOD /v1/path"` field and filter the generated
    spec to those exact method/path pairs.
    """
    allowed_routes = _collect_api_reference_openapi_routes(
        _list_api_reference_markdown_files(docs_json_path, docs_root)
    )
    backend_allowed_routes = set(allowed_routes)
    if (
        "get",
        "/v1/workflows/reviews/versions/{version_id}",
    ) in backend_allowed_routes:
        backend_allowed_routes.add(
            ("get", "/v1/workflows/reviews/versions/{rvr_id}")
        )
    if not allowed_routes:
        raise RuntimeError(
            f"No API reference OpenAPI routes found from docs navigation: {docs_json_path}"
        )

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return

    for path, path_item in list(paths.items()):
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method in list(path_item.keys()):
            if method not in OPENAPI_HTTP_METHODS:
                continue
            if (method, path) not in backend_allowed_routes:
                path_item.pop(method, None)
        if not any(method in OPENAPI_HTTP_METHODS for method in path_item):
            paths.pop(path, None)


def _public_operation_id(operation_id: str) -> str:
    """Convert FastAPI's route-derived id into the public SDK operation id."""
    stable_id = operation_id.split("_v1_", 1)[0]
    for suffix in ("_route", "_flat"):
        if stable_id.endswith(suffix):
            stable_id = stable_id[: -len(suffix)]
    return stable_id


def _normalize_public_operation_ids(spec: dict[str, object]) -> None:
    """Publish stable operationIds, independent of path and HTTP method suffixes."""
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return

    seen: dict[str, str] = {}
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in OPENAPI_HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str):
                continue
            public_operation_id = _public_operation_id(operation_id)
            route_key = f"{method.upper()} {path}"
            previous_route = seen.get(public_operation_id)
            if previous_route is not None:
                raise ValueError(
                    "Duplicate public OpenAPI operationId "
                    f"{public_operation_id!r}: {previous_route} and {route_key}"
                )
            seen[public_operation_id] = route_key
            operation["operationId"] = public_operation_id


def _strip_legacy_from_enums(node: object) -> None:
    """Remove legacy URL entries from any "enum" list deep in the spec."""
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "enum" and isinstance(value, list):
                node[key] = [
                    item
                    for item in value
                    if not (
                        isinstance(item, str)
                        and (
                            item.startswith(LEGACY_DOCUMENT_PATH_PREFIX)
                            or item in LEGACY_EDIT_PATHS
                        )
                    )
                ]
            else:
                _strip_legacy_from_enums(value)
    elif isinstance(node, list):
        for item in node:
            _strip_legacy_from_enums(item)


def _strip_public_workflow_internal_fields(spec: dict[str, object]) -> None:
    """Hide workflow graph implementation fields from the published API docs."""
    schemas = (
        spec.get("components", {}).get("schemas")
        if isinstance(spec.get("components"), dict)
        else None
    )
    if not isinstance(schemas, dict):
        return

    internal_fields_by_schema = {
        "WorkflowBlock": {"draft_version", "field_ref_snapshot", "organization_id"},
        "WorkflowEdge": {"draft_version"},
        "WorkflowConfigBlock": {"field_ref_snapshot"},
    }
    for schema_name, internal_fields in internal_fields_by_schema.items():
        schema = schemas.get(schema_name)
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for field_name in internal_fields:
                properties.pop(field_name, None)
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [
                field_name
                for field_name in required
                if field_name not in internal_fields
            ]


def _strip_update_workflow_email_trigger_docs(spec: dict[str, object]) -> None:
    """Hide private workflow email policy fields from the public update docs."""
    schemas = (
        spec.get("components", {}).get("schemas")
        if isinstance(spec.get("components"), dict)
        else None
    )
    if not isinstance(schemas, dict):
        return

    patch_workflow_schema = schemas.get("UpdateWorkflowRequest")
    if not isinstance(patch_workflow_schema, dict):
        patch_workflow_schema = schemas.get("PatchWorkflowRequest")
    if not isinstance(patch_workflow_schema, dict):
        return

    properties = patch_workflow_schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("email_trigger", None)

    required = patch_workflow_schema.get("required")
    if isinstance(required, list):
        patch_workflow_schema["required"] = [
            field_name for field_name in required if field_name != "email_trigger"
        ]


def _strip_stale_workflow_entities_reference(spec: dict[str, object]) -> None:
    """Avoid publishing stale internal workflow graph route guidance."""
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return

    workflow_path = paths.get("/v1/workflows/{workflow_id}")
    if not isinstance(workflow_path, dict):
        return

    get_operation = workflow_path.get("get")
    if not isinstance(get_operation, dict):
        return

    description = get_operation.get("description")
    if not isinstance(description, str):
        return

    stale_entities_guidance = (
        "\nUse GET /{workflow_id}/"
        + "entities for the live draft graph stored in\n"
        "separate block and edge collections."
    )
    get_operation["description"] = description.replace(
        stale_entities_guidance,
        "\nUse the Blocks and Edges endpoints for the current draft graph.",
    )


def _hard_cutover_workflow_step_lifecycle(spec: dict[str, object]) -> None:
    """Publish workflow steps with lifecycle, not status + terminal.

    The backend service can lag the SDK/docs public contract during the hard
    cutover. Keep the generated public OpenAPI aligned with the SDK surface.
    """
    schemas = (
        spec.get("components", {}).get("schemas")
        if isinstance(spec.get("components"), dict)
        else None
    )
    if not isinstance(schemas, dict):
        return

    lifecycle_variants: dict[str, dict[str, object]] = {
        "PendingStepLifecycle": {
            "properties": {
                "status": {"const": "pending", "title": "Status", "default": "pending"}
            },
            "type": "object",
            "title": "PendingStepLifecycle",
        },
        "QueuedStepLifecycle": {
            "properties": {
                "status": {"const": "queued", "title": "Status", "default": "queued"}
            },
            "type": "object",
            "title": "QueuedStepLifecycle",
        },
        "RunningStepLifecycle": {
            "properties": {
                "status": {"const": "running", "title": "Status", "default": "running"}
            },
            "type": "object",
            "title": "RunningStepLifecycle",
        },
        "CompletedStepLifecycle": {
            "properties": {
                "status": {
                    "const": "completed",
                    "title": "Status",
                    "default": "completed",
                }
            },
            "type": "object",
            "title": "CompletedStepLifecycle",
        },
        "AwaitingReviewStepLifecycle": {
            "properties": {
                "status": {
                    "const": "awaiting_review",
                    "title": "Status",
                    "default": "awaiting_review",
                }
            },
            "type": "object",
            "title": "AwaitingReviewStepLifecycle",
        },
        "ErrorStepLifecycle": {
            "properties": {
                "status": {"const": "error", "title": "Status", "default": "error"},
                "message": {
                    "type": "string",
                    "title": "Message",
                    "description": "Human-readable error message",
                },
                "stage": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "title": "Stage",
                    "description": "Which execution stage failed",
                },
                "category": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "title": "Category",
                    "description": "Category of error for retry decisions",
                },
                "details": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/ErrorDetails"},
                        {"type": "null"},
                    ],
                    "description": "Structured error context",
                },
            },
            "type": "object",
            "required": ["message"],
            "title": "ErrorStepLifecycle",
        },
        "SkippedStepLifecycle": {
            "properties": {
                "status": {"const": "skipped", "title": "Status", "default": "skipped"},
                "reason": {
                    "type": "string",
                    "title": "Reason",
                    "description": "Reason the step was skipped",
                },
            },
            "type": "object",
            "required": ["reason"],
            "title": "SkippedStepLifecycle",
        },
        "CancelledStepLifecycle": {
            "properties": {
                "status": {
                    "const": "cancelled",
                    "title": "Status",
                    "default": "cancelled",
                },
                "reason": {
                    "type": "string",
                    "title": "Reason",
                    "description": "Reason the step was cancelled",
                },
            },
            "type": "object",
            "required": ["reason"],
            "title": "CancelledStepLifecycle",
        },
    }
    schemas.update(lifecycle_variants)

    variant_names = list(lifecycle_variants)
    lifecycle_schema = {
        "oneOf": [
            {"$ref": f"#/components/schemas/{schema_name}"}
            for schema_name in variant_names
        ],
        "discriminator": {
            "propertyName": "status",
            "mapping": {
                "pending": "#/components/schemas/PendingStepLifecycle",
                "queued": "#/components/schemas/QueuedStepLifecycle",
                "running": "#/components/schemas/RunningStepLifecycle",
                "completed": "#/components/schemas/CompletedStepLifecycle",
                "awaiting_review": "#/components/schemas/AwaitingReviewStepLifecycle",
                "error": "#/components/schemas/ErrorStepLifecycle",
                "skipped": "#/components/schemas/SkippedStepLifecycle",
                "cancelled": "#/components/schemas/CancelledStepLifecycle",
            },
        },
        "title": "Lifecycle",
        "description": "Current lifecycle state",
    }

    for schema_name in ("StepResponse", "WorkflowStep"):
        schema = schemas.get(schema_name)
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if isinstance(properties, dict):
            properties.pop("status", None)
            properties.pop("terminal", None)
            properties["lifecycle"] = lifecycle_schema
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [
                "lifecycle" if item == "status" else item for item in required
            ]
        description = schema.get("description")
        if isinstance(description, str):
            schema["description"] = description.replace("step status", "step lifecycle")

    step_path = (
        spec.get("paths", {}).get("/v1/workflows/steps/{step_id}")
        if isinstance(spec.get("paths"), dict)
        else None
    )
    if isinstance(step_path, dict):
        get_operation = step_path.get("get")
        if isinstance(get_operation, dict):
            description = get_operation.get("description")
            if isinstance(description, str):
                get_operation["description"] = description.replace(
                    "Get step status", "Get step lifecycle"
                )


def _without_null_variant(schema: object) -> dict[str, object]:
    if not isinstance(schema, dict):
        return {}

    copied_schema = deepcopy(schema)
    any_of = copied_schema.get("anyOf")
    if not isinstance(any_of, list):
        return copied_schema

    non_null_variants = [
        variant
        for variant in any_of
        if not (isinstance(variant, dict) and variant.get("type") == "null")
    ]
    if len(non_null_variants) != 1 or len(non_null_variants) == len(any_of):
        return copied_schema

    replacement = deepcopy(non_null_variants[0])
    if not isinstance(replacement, dict):
        return copied_schema

    for metadata_key in ("title", "description", "default", "examples"):
        if metadata_key in copied_schema and metadata_key not in replacement:
            replacement[metadata_key] = copied_schema[metadata_key]
    return replacement


def _schema_property(
    properties: dict[str, object],
    name: str,
    *,
    nullable: bool = False,
) -> dict[str, object]:
    property_schema = properties.get(name)
    if nullable:
        return deepcopy(property_schema) if isinstance(property_schema, dict) else {}
    return _without_null_variant(property_schema)


def _schema_property_with_description(
    properties: dict[str, object],
    name: str,
    description: str,
) -> dict[str, object]:
    property_schema = _schema_property(properties, name)
    property_schema["description"] = description
    return property_schema


def _hard_cutover_workflow_create_request_shapes(spec: dict[str, object]) -> None:
    """Publish workflow create requests as shape-enforced public contracts."""
    schemas = (
        spec.get("components", {}).get("schemas")
        if isinstance(spec.get("components"), dict)
        else None
    )
    if not isinstance(schemas, dict):
        return

    run_schema = schemas.get("CreateWorkflowRunRequest")
    if isinstance(run_schema, dict):
        properties = run_schema.get("properties")
        if isinstance(properties, dict):
            schemas["CreateFreshWorkflowRunRequest"] = {
                "properties": {
                    "workflow_id": _schema_property_with_description(
                        properties,
                        "workflow_id",
                        "Workflow id for the fresh run.",
                    ),
                    "documents": _schema_property(
                        properties, "documents", nullable=True
                    ),
                    "json_inputs": _schema_property(
                        properties, "json_inputs", nullable=True
                    ),
                    "version": _schema_property(properties, "version"),
                },
                "type": "object",
                "required": ["workflow_id"],
                "additionalProperties": False,
                "title": "CreateFreshWorkflowRunRequest",
                "description": (
                    "Create a fresh workflow run from a workflow id, optional "
                    "draft/version selector, and optional inputs."
                ),
            }
            schemas["CreateRestartWorkflowRunRequest"] = {
                "properties": {
                    "restart_of": _schema_property(properties, "restart_of"),
                    "config_source": _schema_property(properties, "config_source"),
                    "command_id": _schema_property(properties, "command_id"),
                    "workflow_id": _schema_property_with_description(
                        properties,
                        "workflow_id",
                        (
                            "Optional workflow id when the client already has "
                            "it; otherwise inferred from restart_of."
                        ),
                    ),
                },
                "type": "object",
                "required": ["restart_of", "config_source"],
                "additionalProperties": False,
                "title": "CreateRestartWorkflowRunRequest",
                "description": (
                    "Restart a workflow run from a previous run id. workflow_id "
                    "may be supplied when the client already has it."
                ),
            }
            schemas["CreateWorkflowRunRequest"] = {
                "oneOf": [
                    {"$ref": "#/components/schemas/CreateFreshWorkflowRunRequest"},
                    {"$ref": "#/components/schemas/CreateRestartWorkflowRunRequest"},
                ],
                "title": "CreateWorkflowRunRequest",
                "description": (
                    "Request body for POST /v1/workflows/runs. Use the fresh-run "
                    "shape or the restart shape."
                ),
            }

    test_run_schema = schemas.get("CreateWorkflowTestRunRequest")
    if not isinstance(test_run_schema, dict):
        return

    properties = test_run_schema.get("properties")
    if not isinstance(properties, dict):
        return

    schemas["CreateWorkflowTestRunForTestRequest"] = {
        "properties": {
            "test_id": _schema_property(properties, "test_id"),
            "workflow_id": _schema_property(properties, "workflow_id"),
            "n_consensus": _schema_property(properties, "n_consensus"),
        },
        "type": "object",
        "required": ["test_id"],
        "additionalProperties": False,
        "title": "CreateWorkflowTestRunForTestRequest",
        "description": "Run one saved workflow test by test id.",
    }
    schemas["CreateWorkflowTestRunForTargetRequest"] = {
        "properties": {
            "workflow_id": _schema_property(properties, "workflow_id"),
            "target": _schema_property(properties, "target"),
            "n_consensus": _schema_property(properties, "n_consensus"),
        },
        "type": "object",
        "required": ["workflow_id", "target"],
        "additionalProperties": False,
        "title": "CreateWorkflowTestRunForTargetRequest",
        "description": "Run every workflow test for one target in a workflow.",
    }
    schemas["CreateWorkflowTestRunAllRequest"] = {
        "properties": {
            "workflow_id": _schema_property(properties, "workflow_id"),
            "n_consensus": _schema_property(properties, "n_consensus"),
        },
        "type": "object",
        "required": ["workflow_id"],
        "additionalProperties": False,
        "title": "CreateWorkflowTestRunAllRequest",
        "description": "Run every saved test in a workflow.",
    }
    schemas["CreateWorkflowTestRunRequest"] = {
        "oneOf": [
            {"$ref": "#/components/schemas/CreateWorkflowTestRunForTestRequest"},
            {"$ref": "#/components/schemas/CreateWorkflowTestRunForTargetRequest"},
            {"$ref": "#/components/schemas/CreateWorkflowTestRunAllRequest"},
        ],
        "title": "CreateWorkflowTestRunRequest",
        "description": (
            "Request body for POST /v1/workflows/tests/runs. Use exactly one "
            "of the single-test, target, or all-tests shapes."
        ),
    }


def _replace_schema_ref(
    node: object, old_schema_name: str, new_schema_name: str
) -> None:
    """Replace component schema refs deep in an OpenAPI subtree."""
    old_ref = f"#/components/schemas/{old_schema_name}"
    new_ref = f"#/components/schemas/{new_schema_name}"
    if isinstance(node, dict):
        if node.get("$ref") == old_ref:
            node["$ref"] = new_ref
        for key, value in list(node.items()):
            if isinstance(value, str) and value == old_ref:
                node[key] = new_ref
                continue
            _replace_schema_ref(value, old_schema_name, new_schema_name)
    elif isinstance(node, list):
        for item in node:
            _replace_schema_ref(item, old_schema_name, new_schema_name)


def _rename_schema(
    spec: dict[str, object], old_schema_name: str, new_schema_name: str
) -> None:
    """Rename a schema component and update all public refs to it."""
    schemas = (
        spec.get("components", {}).get("schemas")
        if isinstance(spec.get("components"), dict)
        else None
    )
    if not isinstance(schemas, dict):
        return

    schema = schemas.pop(old_schema_name, None)
    if not isinstance(schema, dict):
        return

    schema["title"] = new_schema_name
    schemas[new_schema_name] = schema
    _replace_schema_ref(spec, old_schema_name, new_schema_name)


def _normalize_public_schema_names(spec: dict[str, object]) -> None:
    """Rename generated/internal component names to public API names.

    This is a documentation-time overlay for the public OpenAPI contract. It
    keeps wire JSON unchanged while preventing Python module paths,
    Pydantic-generated input/output names, and storage DTO names from leaking
    into generated SDKs.
    """
    schema_renames = {
        "BBox-Input": "BoundingBoxInput",
        "Category-Output": "ClassificationCategoryOutput",
        "ClassificationRequest": "CreateClassificationRequest",
        "CompleteUploadRequest": "CompleteFileUploadRequest",
        "CreateUploadResponse": "CreateFileUploadResponse",
        "EditConfig-Output": "EditConfigOutput",
        "EditTemplateRequest": "CreateEditTemplateRequest",
        "FileRecord": "File",
        "GetSourcesResponse": "ExtractionSources",
        "MIMEData-Input": "MIMEDataInput",
        "MIMEData-Output": "MIMEData",
        "BlockSimulationObject": "WorkflowSimulation",
        "PatchBlockRequest": "UpdateWorkflowBlockRequest",
        "PatchWorkflowRequest": "UpdateWorkflowRequest",
        "PartitionRequest": "CreatePartitionRequest",
        "main_server__types__classifications__Category": (
            "ClassificationCategory"
        ),
        "main_server__types__classifications__Classification": (
            "Classification"
        ),
        "main_server__types__edits__BBox": "BoundingBox",
        "main_server__types__edits__EditConfig": "EditConfig",
        "main_server__types__edits__EditRequest": "CreateEditRequest",
        "main_server__types__edits__FormField-Input": "EditFormFieldInput",
        "main_server__types__edits__FormField-Output": "EditFormFieldOutput",
        "main_server__types__mime__OCR": "OCR",
        "main_server__types__mime__Page": "Page",
        "main_server__types__parses__Parse": "Parse",
        "main_server__types__parses__ParseRequest": "CreateParseRequest",
        "main_server__types__partitions__Partition": "Partition",
        "main_server__types__splits__SplitConsensus": "SplitConsensus",
        "main_server__types__splits__SplitResult": "SplitResult",
        "main_server__types__splits__SplitSubdocumentLikelihood": (
            "SplitSubdocumentLikelihood"
        ),
        "main_server__types__splits__Subdocument": "SplitSubdocument",
    }

    for old_schema_name, new_schema_name in schema_renames.items():
        _rename_schema(spec, old_schema_name, new_schema_name)


def _hard_cutover_review_overlay_docs(spec: dict[str, object]) -> None:
    """Normalize review overlay docs to the public hard-cutover contract."""
    schemas = (
        spec.get("components", {}).get("schemas")
        if isinstance(spec.get("components"), dict)
        else None
    )
    if not isinstance(schemas, dict):
        return

    _rename_schema(spec, "Submit" + "DecisionResponse", "WorkflowReviewDecisionResponse")

    output_version_schema = schemas.get("OutputVersion")
    if isinstance(output_version_schema, dict):
        description = output_version_schema.get("description")
        if isinstance(description, str):
            output_version_schema["description"] = description.replace(
                "ReviewOverlay.versions", "WorkflowReview.versions"
            )

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return

    reviews_list_path = paths.get("/v1/workflows/reviews")
    if isinstance(reviews_list_path, dict):
        get_operation = reviews_list_path.get("get")
        if isinstance(get_operation, dict):
            parameters = get_operation.get("parameters")
            if isinstance(parameters, list):
                for parameter in parameters:
                    if (
                        isinstance(parameter, dict)
                        and parameter.get("name") == "decision_status"
                    ):
                        schema = parameter.get("schema")
                        if isinstance(schema, dict):
                            schema.pop("pattern", None)
                            schema["enum"] = REVIEW_DECISION_STATUS_VALUES
                            schema["description"] = (
                                "Filter by decision state: pending, approved, "
                                "rejected, decided, or all."
                            )
                        parameter["description"] = (
                            "Filter by decision state: pending, approved, "
                            "rejected, decided, or all."
                        )

    for path, operations in paths.items():
        if not isinstance(path, str) or not path.startswith("/v1/workflows/reviews"):
            continue
        if not isinstance(operations, dict):
            continue
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            parameters = operation.get("parameters")
            if not isinstance(parameters, list):
                continue
            for parameter in parameters:
                if isinstance(parameter, dict) and parameter.get("name") == "id":
                    parameter["description"] = "Opaque review id."
                    schema = parameter.get("schema")
                    if isinstance(schema, dict):
                        schema["description"] = "Opaque review id."


def _workflow_paginated_schema(
    schemas: dict[str, object],
    schema_name: str,
    item_schema_name: str,
) -> bool:
    if item_schema_name not in schemas or "ListMetadata" not in schemas:
        return False

    schemas[schema_name] = {
        "properties": {
            "data": {
                "items": {"$ref": f"#/components/schemas/{item_schema_name}"},
                "type": "array",
                "title": "Data",
            },
            "list_metadata": {"$ref": "#/components/schemas/ListMetadata"},
        },
        "type": "object",
        "required": ["data", "list_metadata"],
        "title": schema_name,
    }
    return True


PUBLIC_PAGINATED_LIST_ROUTES: tuple[tuple[str, str, str], ...] = (
    (
        "/v1/classifications",
        "ClassificationList",
        "Classification",
    ),
    ("/v1/edits", "EditList", "Edit"),
    ("/v1/edits/templates", "EditTemplateList", "EditTemplate"),
    ("/v1/extractions", "ExtractionList", "Extraction"),
    ("/v1/parses", "ParseList", "Parse"),
    ("/v1/partitions", "PartitionList", "Partition"),
    ("/v1/splits", "SplitList", "Split"),
    ("/v1/workflows", "WorkflowList", "Workflow"),
    ("/v1/workflows/artifacts", "WorkflowArtifactList", "WorkflowArtifact"),
    ("/v1/workflows/blocks", "WorkflowBlockList", "WorkflowBlock"),
    ("/v1/workflows/edges", "WorkflowEdgeList", "WorkflowEdge"),
    ("/v1/workflows/experiments", "WorkflowExperimentList", "WorkflowExperiment"),
    (
        "/v1/workflows/experiments/results",
        "WorkflowExperimentResultList",
        "WorkflowExperimentResult",
    ),
    (
        "/v1/workflows/experiments/runs",
        "WorkflowExperimentRunList",
        "WorkflowExperimentRun",
    ),
    (
        "/v1/workflows/reviews",
        "WorkflowReviewSummaryList",
        "WorkflowReviewSummary",
    ),
    (
        "/v1/workflows/reviews/versions",
        "WorkflowReviewVersionList",
        "WorkflowReviewVersion",
    ),
    ("/v1/workflows/runs", "WorkflowRunList", "WorkflowRun"),
    ("/v1/workflows/simulations", "WorkflowSimulationList", "WorkflowSimulation"),
    ("/v1/workflows/steps", "WorkflowStepList", "WorkflowStep"),
    ("/v1/workflows/tests", "WorkflowTestList", "WorkflowTest"),
    (
        "/v1/workflows/tests/results",
        "WorkflowTestResultList",
        "WorkflowTestResult",
    ),
    ("/v1/workflows/tests/runs", "WorkflowTestRunList", "WorkflowTestRun"),
)


def _set_get_response_schema(
    paths: dict[str, object],
    path: str,
    schema_name: str,
) -> None:
    path_item = paths.get(path)
    if not isinstance(path_item, dict):
        return

    get_operation = path_item.get("get")
    if not isinstance(get_operation, dict):
        return

    try:
        response = get_operation["responses"]["200"]["content"][
            "application/json"
        ]
    except KeyError:
        return
    if isinstance(response, dict):
        response["schema"] = {"$ref": f"#/components/schemas/{schema_name}"}


def _normalize_public_list_response_docs(spec: dict[str, object]) -> None:
    """Publish typed, public names for list response envelopes."""
    components = spec.get("components")
    if not isinstance(components, dict):
        return
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return

    for path, schema_name, item_schema_name in PUBLIC_PAGINATED_LIST_ROUTES:
        if _workflow_paginated_schema(schemas, schema_name, item_schema_name):
            _set_get_response_schema(paths, path, schema_name)

    _rename_schema(spec, "JobListResponse", "JobList")
    job_list_schema = schemas.get("JobList")
    if isinstance(job_list_schema, dict):
        job_list_schema["description"] = "List response for GET /v1/jobs."


def _normalize_workflow_read_docs(spec: dict[str, object]) -> None:
    """Apply public workflow read-model documentation overlays."""
    components = spec.get("components")
    if not isinstance(components, dict):
        return
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return

    _normalize_public_list_response_docs(spec)

    review_version_schema = schemas.get("WorkflowReviewVersion")
    if isinstance(review_version_schema, dict):
        properties = review_version_schema.get("properties")
        if isinstance(properties, dict) and "Actor" in schemas:
            properties["author"] = {
                "$ref": "#/components/schemas/Actor",
                "description": "Actor that created the version.",
            }

    rvr_path = paths.pop("/v1/workflows/reviews/versions/{rvr_id}", None)
    if isinstance(rvr_path, dict):
        paths["/v1/workflows/reviews/versions/{version_id}"] = rvr_path
        get_operation = rvr_path.get("get")
        if isinstance(get_operation, dict):
            operation_id = get_operation.get("operationId")
            if isinstance(operation_id, str):
                get_operation["operationId"] = operation_id.replace(
                    "rvr_id", "version_id"
                )
            parameters = get_operation.get("parameters")
            if isinstance(parameters, list):
                for parameter in parameters:
                    if (
                        isinstance(parameter, dict)
                        and parameter.get("name") == "rvr_id"
                    ):
                        parameter["name"] = "version_id"
                        parameter["description"] = "Opaque review version id."
                        schema = parameter.get("schema")
                        if isinstance(schema, dict):
                            schema["title"] = "Version Id"
                            schema["description"] = "Opaque review version id."

    for old_schema_name, new_schema_name in (
        ("AssertionSpec-Input", "AssertionSpecInput"),
        ("AssertionSpec-Output", "AssertionSpecOutput"),
        ("AllItemsMatchCondition-Input", "AllItemsMatchConditionInput"),
        ("AllItemsMatchCondition-Output", "AllItemsMatchConditionOutput"),
        ("AnyItemMatchesCondition-Input", "AnyItemMatchesConditionInput"),
        ("AnyItemMatchesCondition-Output", "AnyItemMatchesConditionOutput"),
    ):
        _rename_schema(spec, old_schema_name, new_schema_name)


def generate_openapi() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    backend_main_server = repo_root / "backend" / "main_server"
    if str(backend_main_server) not in sys.path:
        sys.path.insert(0, str(backend_main_server))

    from main_server.main import app

    spec = app.openapi()

    # Update security schemes
    spec["components"]["securitySchemes"] = {
        "API Key": {"type": "apiKey", "in": "header", "name": "Api-Key"}
    }

    # Update servers
    spec["servers"] = [{"url": "https://api.retab.com"}]

    # Strip every legacy/private path from the paths map
    for path in list(spec["paths"].keys()):
        if (
            path.startswith(LEGACY_DOCUMENT_PATH_PREFIX)
            or path.startswith(LEGACY_REVIEW_DECISION_PATH_PREFIX)
            or path.startswith(PRIVATE_PATH_PREFIXES)
            or any(suffix in path for suffix in DIAGNOSTIC_PATH_SUFFIXES)
            or path in LEGACY_EDIT_PATHS
        ):
            spec["paths"].pop(path, None)

    docs_root = repo_root / "open-source" / "docs"
    _strip_routes_not_in_api_reference_markdown(
        spec,
        docs_json_path=docs_root / "docs.json",
        docs_root=docs_root,
    )

    # Strip legacy URLs from any enum lists (e.g. Jobs endpoint enum)
    _strip_legacy_from_enums(spec)

    _strip_public_workflow_internal_fields(spec)
    _strip_update_workflow_email_trigger_docs(spec)
    _strip_stale_workflow_entities_reference(spec)
    _hard_cutover_workflow_step_lifecycle(spec)
    _hard_cutover_workflow_create_request_shapes(spec)
    _hard_cutover_review_overlay_docs(spec)
    _normalize_public_schema_names(spec)
    _normalize_workflow_read_docs(spec)
    _normalize_public_operation_ids(spec)

    # Strip unused legacy request/response schemas that only belonged to the
    # document-scoped classification API.
    schemas = spec.get("components", {}).get("schemas")
    if isinstance(schemas, dict):
        for schema_name in LEGACY_SCHEMA_NAMES:
            schemas.pop(schema_name, None)

    # Keep only schemas reachable from the published API surface.
    _prune_unreferenced_schemas(spec)

    # Write updated spec to file
    output_path = Path(__file__).resolve().parent / "openapi.json"
    with output_path.open("w") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    generate_openapi()
