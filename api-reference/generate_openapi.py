import json
import sys
from pathlib import Path


LEGACY_DOCUMENT_PATH_PREFIX = "/v1/documents/"
PRIVATE_PATH_PREFIXES: tuple[str, ...] = (
    "/internal/",
    "/custom/",
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
}


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


def _strip_public_organization_id(node: object) -> None:
    """Remove tenant-scoping internals from the public API reference."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            properties.pop("organization_id", None)

        required = node.get("required")
        if isinstance(required, list):
            node["required"] = [item for item in required if item != "organization_id"]

        for key, value in list(node.items()):
            if key == "parameters" and isinstance(value, list):
                node[key] = [
                    parameter
                    for parameter in value
                    if not (
                        isinstance(parameter, dict)
                        and parameter.get("name") == "organization_id"
                    )
                ]
            else:
                _strip_public_organization_id(value)
    elif isinstance(node, list):
        for item in node:
            _strip_public_organization_id(item)


def _scrub_public_organization_id_text(node: object) -> None:
    """Remove tenant-scoping implementation terms from public descriptions."""
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if isinstance(value, str):
                node[key] = value.replace("organization_id", "tenant scope")
            else:
                _scrub_public_organization_id_text(value)
    elif isinstance(node, list):
        for item in node:
            _scrub_public_organization_id_text(item)


def _strip_public_workflow_internal_fields(spec: dict[str, object]) -> None:
    """Hide workflow graph implementation fields from the published API docs."""
    schemas = spec.get("components", {}).get("schemas") if isinstance(spec.get("components"), dict) else None
    if not isinstance(schemas, dict):
        return

    internal_fields_by_schema = {
        "WorkflowBlockObject": {"draft_version", "field_ref_snapshot"},
        "WorkflowEdgeObject": {"draft_version"},
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


def _hard_cutover_workflow_step_lifecycle(spec: dict[str, object]) -> None:
    """Publish workflow steps with lifecycle, not status + terminal.

    The backend service can lag the SDK/docs public contract during the hard
    cutover. Keep the generated public OpenAPI aligned with the SDK surface.
    """
    schemas = spec.get("components", {}).get("schemas") if isinstance(spec.get("components"), dict) else None
    if not isinstance(schemas, dict):
        return

    lifecycle_variants: dict[str, dict[str, object]] = {
        "PendingStepLifecycle": {
            "properties": {"status": {"const": "pending", "title": "Status", "default": "pending"}},
            "type": "object",
            "title": "PendingStepLifecycle",
        },
        "QueuedStepLifecycle": {
            "properties": {"status": {"const": "queued", "title": "Status", "default": "queued"}},
            "type": "object",
            "title": "QueuedStepLifecycle",
        },
        "RunningStepLifecycle": {
            "properties": {"status": {"const": "running", "title": "Status", "default": "running"}},
            "type": "object",
            "title": "RunningStepLifecycle",
        },
        "CompletedStepLifecycle": {
            "properties": {"status": {"const": "completed", "title": "Status", "default": "completed"}},
            "type": "object",
            "title": "CompletedStepLifecycle",
        },
        "WaitingForHumanStepLifecycle": {
            "properties": {
                "status": {
                    "const": "waiting_for_human",
                    "title": "Status",
                    "default": "waiting_for_human",
                }
            },
            "type": "object",
            "title": "WaitingForHumanStepLifecycle",
        },
        "ErrorStepLifecycle": {
            "properties": {
                "status": {"const": "error", "title": "Status", "default": "error"},
                "message": {"type": "string", "title": "Message", "description": "Human-readable error message"},
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
                    "anyOf": [{"$ref": "#/components/schemas/ErrorDetails"}, {"type": "null"}],
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
                "reason": {"type": "string", "title": "Reason", "description": "Reason the step was skipped"},
            },
            "type": "object",
            "required": ["reason"],
            "title": "SkippedStepLifecycle",
        },
        "CancelledStepLifecycle": {
            "properties": {
                "status": {"const": "cancelled", "title": "Status", "default": "cancelled"},
                "reason": {"type": "string", "title": "Reason", "description": "Reason the step was cancelled"},
            },
            "type": "object",
            "required": ["reason"],
            "title": "CancelledStepLifecycle",
        },
    }
    schemas.update(lifecycle_variants)

    variant_names = list(lifecycle_variants)
    lifecycle_schema = {
        "oneOf": [{"$ref": f"#/components/schemas/{schema_name}"} for schema_name in variant_names],
        "discriminator": {
            "propertyName": "status",
            "mapping": {
                "pending": "#/components/schemas/PendingStepLifecycle",
                "queued": "#/components/schemas/QueuedStepLifecycle",
                "running": "#/components/schemas/RunningStepLifecycle",
                "completed": "#/components/schemas/CompletedStepLifecycle",
                "waiting_for_human": "#/components/schemas/WaitingForHumanStepLifecycle",
                "error": "#/components/schemas/ErrorStepLifecycle",
                "skipped": "#/components/schemas/SkippedStepLifecycle",
                "cancelled": "#/components/schemas/CancelledStepLifecycle",
            },
        },
        "title": "Lifecycle",
        "description": "Current lifecycle state",
    }

    for schema_name in ("StepResponse", "StepStatusObject"):
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
            schema["required"] = ["lifecycle" if item == "status" else item for item in required]
        description = schema.get("description")
        if isinstance(description, str):
            schema["description"] = description.replace("step status", "step lifecycle")

    step_path = (
        spec.get("paths", {}).get("/v1/workflows/runs/{run_id}/steps/{block_id}")
        if isinstance(spec.get("paths"), dict)
        else None
    )
    if isinstance(step_path, dict):
        get_operation = step_path.get("get")
        if isinstance(get_operation, dict):
            description = get_operation.get("description")
            if isinstance(description, str):
                get_operation["description"] = description.replace("Get step status", "Get step lifecycle")


def generate_openapi() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    backend_main_server = repo_root / "backend" / "main_server"
    if str(backend_main_server) not in sys.path:
        sys.path.insert(0, str(backend_main_server))

    from main_server.main import app

    spec = app.openapi()

    # Update security schemes
    spec["components"]["securitySchemes"] = {"API Key": {"type": "apiKey", "in": "header", "name": "Api-Key"}}

    # Update servers
    spec["servers"] = [{"url": "https://api.retab.com"}]

    # Strip every legacy/private path from the paths map
    for path in list(spec["paths"].keys()):
        if (
            path.startswith(LEGACY_DOCUMENT_PATH_PREFIX)
            or path.startswith(PRIVATE_PATH_PREFIXES)
            or path in LEGACY_EDIT_PATHS
        ):
            spec["paths"].pop(path, None)

    # Strip legacy URLs from any enum lists (e.g. Jobs endpoint enum)
    _strip_legacy_from_enums(spec)

    _strip_public_organization_id(spec)
    _scrub_public_organization_id_text(spec)
    _strip_public_workflow_internal_fields(spec)
    _hard_cutover_workflow_step_lifecycle(spec)

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
        json.dump(spec, f, indent=2)


if __name__ == "__main__":
    generate_openapi()
