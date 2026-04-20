import json
import sys
from pathlib import Path


LEGACY_DOCUMENT_PATH_PREFIX = "/v1/documents/"

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

    # Strip every legacy path from the paths map
    for path in list(spec["paths"].keys()):
        if path.startswith(LEGACY_DOCUMENT_PATH_PREFIX) or path in LEGACY_EDIT_PATHS:
            spec["paths"].pop(path, None)

    # Strip legacy URLs from any enum lists (e.g. Jobs endpoint enum)
    _strip_legacy_from_enums(spec)

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
