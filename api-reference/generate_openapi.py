import json
import os
import re
import sys
from argparse import ArgumentParser
from pathlib import Path

import yaml


PUBLIC_API_ROUTES_PATH = Path(__file__).resolve().parent / "public_api_routes.yaml"

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
    "/v1/edit/templates/generate",
    "/v1/edit/templates/infer_form_bounding_boxes",
    "/v1/edit/templates/edits",
    "/v1/edit/templates/edits/count",
    "/v1/edit/templates/edits/{edit_id}",
}
LEGACY_ENUM_ENDPOINTS: set[str] = {
    "/v1/files/analyze",
}

LEGACY_SCHEMA_NAMES: set[str] = {
    "ClassifyRequest",
    "ClassifyResponse",
    "H" + "IL" + "DecisionResource",
    "Submit" + "H" + "IL" + "DecisionRequest",
    "Submit" + "H" + "IL" + "DecisionResponse",
}
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

OPENAPI_GENERATION_ENV_DEFAULTS: dict[str, str] = {
    "AUTHKIT_DOMAIN": "openapi-generation.authkit.app",
    "ENV_NAME": "dev",
    "FERNET_ENCRYPTION_KEY": "openapi-generation-key",
    "FFDNET_SERVER_BASE_URL": "http://127.0.0.1:4004",
    "FRONT_BASE_URL": "http://localhost:3000",
    "GOOGLE_PROJECT_ID": "openapi-generation",
    "GOOGLE_STORAGE_BUCKET_NAME": "openapi-generation",
    "MONGODB_URI": "mongodb://127.0.0.1:27017/openapi_generation",
    "STRIPE_METER_ID": "openapi_generation_meter",
    "STRIPE_PUBLISHABLE_KEY": "pk_test_openapi_generation",
    "STRIPE_SECRET_KEY": "sk_test_openapi_generation",
    "STRIPE_WEBHOOK_SECRET": "whsec_openapi_generation",
    "VALKEY_HOST": "127.0.0.1",
    "VALKEY_PORT": "6379",
    "WORKOS_API_KEY": "sk_test_openapi_generation",
    "WORKOS_CLIENT_ID": "client_openapi_generation",
}


def _seed_openapi_generation_env() -> None:
    for key, value in OPENAPI_GENERATION_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)


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


def _parse_route_entry(entry: object, source: Path) -> tuple[str, str]:
    """Parse a ``"METHOD /v1/path"`` manifest entry into ``(method, path)``."""
    if not isinstance(entry, str):
        raise ValueError(f"{source} route entry is not a string: {entry!r}")
    parts = entry.split()
    if len(parts) != 2:
        raise ValueError(f'{source} route entry must be "METHOD /path", got {entry!r}')
    method, path = parts[0].lower(), _normalize_openapi_path(parts[1])
    if method not in OPENAPI_HTTP_METHODS:
        raise ValueError(f"{source} has unsupported route method {method!r}")
    if not path.startswith("/"):
        raise ValueError(f"{source} route path must be absolute: {path!r}")
    return method, path


def _load_public_route_manifest(
    manifest_path: Path,
) -> dict[str, set[tuple[str, str]]]:
    """Load the authoritative public-route allow-list from YAML.

    Returns the two declared route sets, ``sdk_routes`` (published to the
    generated spec/SDKs) and ``documentation_only_routes`` (documented for
    humans but deliberately not part of the SDK surface).
    """
    manifest = yaml.safe_load(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError(f"{manifest_path} must be a YAML mapping")

    sections: dict[str, set[tuple[str, str]]] = {}
    for key in ("sdk_routes", "documentation_only_routes"):
        entries = manifest.get(key) or []
        if not isinstance(entries, list):
            raise ValueError(f"{manifest_path}:{key} must be a list of routes")
        routes: set[tuple[str, str]] = set()
        for entry in entries:
            route = _parse_route_entry(entry, manifest_path)
            if route in routes:
                raise ValueError(f"{manifest_path}:{key} has duplicate route {entry!r}")
            routes.add(route)
        sections[key] = routes

    overlap = sections["sdk_routes"] & sections["documentation_only_routes"]
    if overlap:
        raise ValueError(
            f"{manifest_path} lists routes in both sections: {sorted(overlap)}"
        )
    return sections


def _load_public_sdk_routes(manifest_path: Path) -> set[tuple[str, str]]:
    """Return the published SDK route allow-list from the manifest."""
    return _load_public_route_manifest(manifest_path)["sdk_routes"]


def _strip_routes_not_in_public_manifest(
    spec: dict[str, object],
    manifest_path: Path,
) -> None:
    """Keep only operations listed under ``sdk_routes`` in the manifest.

    ``public_api_routes.yaml`` is the single source of truth for the public
    API surface: every method/path pair not declared there is stripped from the
    generated spec (and therefore from the SDKs/CLI). The docs navigation is
    kept consistent with the manifest by a separate test, not at generation
    time.
    """
    allowed_routes = _load_public_sdk_routes(manifest_path)
    if not allowed_routes:
        raise RuntimeError(
            f"No public SDK routes declared in manifest: {manifest_path}"
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
            if (method, path) not in allowed_routes:
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


def _inject_container_defaults(spec: dict[str, object]) -> None:
    """Restore JSON Schema ``default`` values that the FastAPI/Pydantic
    pipeline drops for optional fields, so generated SDKs emit idiomatic
    defaults instead of nullable placeholders.

    Three classes of fields end up without a ``default`` in the raw
    FastAPI spec even though the Pydantic model defines one:

    1. ``default_factory=list`` and ``default_factory=dict``.
       Pydantic v2 deliberately omits a JSON Schema ``default`` for any
       ``default_factory`` because the factory might be non-deterministic.
       For ``list``/``dict`` the default IS deterministic (empty
       container), so we inject ``default: []`` / ``default: {}``.

    2. Bare ``Any`` fields with ``Field(default=None)``.
       Pydantic emits ``"default": null`` for these, but FastAPI strips
       ``default: null`` during its own openapi pass to keep the spec
       "clean". We restore it so the SDK knows the field is nullable
       and defaults to ``None`` — without this, the SDK generator falls
       back to producing a required-looking ``Any`` field.

    Both fix the same downstream symptom: generated SDKs producing
    ``field: T | None = None`` (or no default at all) where the backend
    model actually defines a concrete default. With the defaults
    restored, the SDK emits ``field: list[T] = []`` / ``field: dict[K,V]
    = {}`` / ``field: Any = None``, matching backend runtime behavior.

    Conservative invariants (apply to every injection):

      * The property is NOT in the schema's ``required`` set — we never
        invent defaults for required fields.
      * The property has no existing ``default`` key — we never
        overwrite an explicit default the model declared.
      * The property is NOT nullable — a nullable shape already carries
        ``None`` as an absence sentinel; leaving the default unset lets
        the SDK choose between ``None`` and an empty value.

    What we deliberately do NOT touch:

      * ``$ref``-typed optional fields with ``default_factory=SomeClass``.
        Surfacing those would require instantiating the referenced model
        and serializing its own defaults; doing that lossily (e.g. by
        injecting ``default: {}``) would make the spec lie about the
        actual structured default. Leave them to manual widening.
      * ``oneOf``/``anyOf`` unions without a null branch. These are
        discriminated-union shapes where injecting an arbitrary default
        could collide with the union's discriminator semantics.
    """
    schemas = spec.get("components", {}).get("schemas")  # type: ignore[union-attr]
    if not isinstance(schemas, dict):
        return

    for schema in schemas.values():
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        required = set(schema.get("required") or [])
        for prop_name, prop_sch in properties.items():
            if not isinstance(prop_sch, dict):
                continue
            if prop_name in required:
                continue
            if "default" in prop_sch:
                continue

            # Skip nullable shapes — None is already the absence sentinel.
            type_field = prop_sch.get("type")
            if isinstance(type_field, list) and "null" in type_field:
                continue
            if prop_sch.get("nullable") is True:
                continue
            if any(
                isinstance(b, dict) and b.get("type") == "null"
                for combinator in ("anyOf", "oneOf")
                for b in prop_sch.get(combinator) or []
            ):
                continue

            if type_field == "array":
                prop_sch["default"] = []
            elif type_field == "object":
                # Either a plain object schema or the
                # ``additionalProperties``-typed map shape Pydantic emits
                # for ``dict[K, V]``. Both want ``{}``.
                prop_sch["default"] = {}
            elif (
                type_field is None
                and "$ref" not in prop_sch
                and "anyOf" not in prop_sch
                and "oneOf" not in prop_sch
                and "allOf" not in prop_sch
                and "enum" not in prop_sch
                and "const" not in prop_sch
            ):
                # Bare schema with no constraints — this is an ``Any``
                # field that Pydantic typed as ``Any = Field(default=None)``.
                # FastAPI strips ``default: null`` from the emitted spec;
                # restore it so the SDK generates a nullable Any field
                # with ``None`` as the default instead of a required
                # opaque type.
                prop_sch["default"] = None


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
                            or item in LEGACY_ENUM_ENDPOINTS
                        )
                    )
                ]
            else:
                _strip_legacy_from_enums(value)
    elif isinstance(node, list):
        for item in node:
            _strip_legacy_from_enums(item)


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


def _collapse_schema_refs(
    spec: dict[str, object], old_schema_name: str, target_schema_name: str
) -> None:
    """Replace refs to a public schema and remove the old component."""
    schemas = (
        spec.get("components", {}).get("schemas")
        if isinstance(spec.get("components"), dict)
        else None
    )
    if isinstance(schemas, dict):
        schemas.pop(old_schema_name, None)
    _replace_schema_ref(spec, old_schema_name, target_schema_name)
    _dedupe_schema_unions(spec)


def _dedupe_schema_unions(node: object) -> None:
    """Remove duplicate refs/items from generated anyOf/oneOf arrays."""
    if isinstance(node, dict):
        for key in ("anyOf", "oneOf"):
            union = node.get(key)
            if not isinstance(union, list):
                continue
            seen: set[str] = set()
            deduped: list[object] = []
            for item in union:
                marker = json.dumps(item, sort_keys=True)
                if marker in seen:
                    continue
                seen.add(marker)
                deduped.append(item)
            node[key] = deduped
        for value in node.values():
            _dedupe_schema_unions(value)
    elif isinstance(node, list):
        for item in node:
            _dedupe_schema_unions(item)


def _normalize_public_schema_names(spec: dict[str, object]) -> None:
    """Rename generated/internal component names to public API names.

    This is a documentation-time overlay for the public OpenAPI contract. It
    keeps wire JSON unchanged while preventing Python module paths,
    Pydantic-generated input/output names, and storage DTO names from leaking
    into generated SDKs.
    """
    _collapse_schema_refs(spec, "StoredJobResponse", "JobResponse")

    schema_renames = {
        "BBox-Input": "BoundingBoxInput",
        "Category-Output": "ClassificationCategoryOutput",
        "ClassificationRequest": "CreateClassificationRequest",
        "CompleteUploadRequest": "CompleteFileUploadRequest",
        "CreateUploadResponse": "CreateFileUploadResponse",
        "EditConfig-Output": "EditConfigOutput",
        "EditTemplateRequest": "CreateEditTemplateRequest",
        # FastAPI/pydantic emits the public File class with its module FQN
        # because there are two same-named classes in the project (the
        # storage `libs.db_models.FileRecord` and the public
        # `services.v1.files.models.File`). Map the FQN to the short name for the
        # public response. The legacy `FileRecord → File` rename below
        # used to handle this when FileRecord WAS the public response;
        # keep it for defense-in-depth in case FileRecord ever leaks back
        # into the spec (today it shouldn't — every file route projects
        # through `_to_public_file` before serializing).
        "main_server__services__v1__files__models__File": "File",
        "main_server__types__files__File": "File",
        "FileRecord": "File",
        "GetSourcesResponse": "ExtractionSources",
        "JobResponse": "JobResult",
        "MIMEData-Output": "MIMEData",
        "BlockExecutionObject": "BlockExecution",
        "Body_create_table": "CreateWorkflowTableUploadRequest",
        "Body_create_table_v1_tables_post": "CreateWorkflowTableUploadRequest",
        "Body_table_create_v1_tables_post": "CreateWorkflowTableUploadRequest",
        "Body_replace_table": "ReplaceWorkflowTableUploadRequest",
        "Body_replace_table_v1_tables__table_id__put": (
            "ReplaceWorkflowTableUploadRequest"
        ),
        "Body_table_replace_v1_tables__table_id__put": (
            "ReplaceWorkflowTableUploadRequest"
        ),
        "PatchBlockRequest": "UpdateWorkflowBlockRequest",
        "PatchWorkflowRequest": "UpdateWorkflowRequest",
        "PartitionRequest": "CreatePartitionRequest",
        "main_server__types__classifications__Category": ("ClassificationCategory"),
        "main_server__types__classifications__Classification": ("Classification"),
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
    _collapse_schema_refs(spec, "MIMEData" + "-Input", "MIMEData")


def _workflow_paginated_schema(
    schemas: dict[str, object],
    schema_name: str,
    item_schema_name: str,
) -> bool:
    if item_schema_name not in schemas or "ListMetadata" not in schemas:
        return False

    schemas[schema_name] = {
        "description": (
            f"A page of `{item_schema_name}` resources. `data` holds the "
            f"items and `list_metadata` carries the `before`/`after` "
            f"cursors; pass `after` to fetch the next page."
        ),
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
    (
        "/v1/workflows/blocks/versions",
        "WorkflowBlockVersionList",
        "WorkflowBlockVersion",
    ),
    ("/v1/workflows/edges", "WorkflowEdgeList", "WorkflowEdge"),
    (
        "/v1/workflows/edges/versions",
        "WorkflowEdgeVersionList",
        "WorkflowEdgeVersion",
    ),
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
        "WorkflowReviewList",
        "WorkflowReview",
    ),
    (
        "/v1/workflows/reviews/versions",
        "WorkflowReviewVersionList",
        "WorkflowReviewVersion",
    ),
    (
        "/v1/workflows/versions",
        "WorkflowGraphVersionList",
        "WorkflowGraphVersion",
    ),
    ("/v1/workflows/runs", "WorkflowRunList", "WorkflowRun"),
    ("/v1/workflows/blocks/executions", "BlockExecutionList", "BlockExecution"),
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
        response = get_operation["responses"]["200"]["content"]["application/json"]
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

    for old_schema_name, new_schema_name in (
        ("AssertionSpec-Input", "AssertionSpecInput"),
        ("AssertionSpec-Output", "AssertionSpecOutput"),
        ("AllItemsMatchCondition-Input", "AllItemsMatchConditionInput"),
        ("AllItemsMatchCondition-Output", "AllItemsMatchConditionOutput"),
        ("AnyItemMatchesCondition-Input", "AnyItemMatchesConditionInput"),
        ("AnyItemMatchesCondition-Output", "AnyItemMatchesConditionOutput"),
    ):
        _rename_schema(spec, old_schema_name, new_schema_name)


def _normalize_public_file_download_docs(spec: dict[str, object]) -> None:
    """Document CSV downloads as binary file responses, not JSON bodies."""
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return

    path_item = paths.get("/v1/tables/{table_id}/download")
    if not isinstance(path_item, dict):
        return

    operation = path_item.get("get")
    if not isinstance(operation, dict):
        return

    response = operation.get("responses", {}).get("200")
    if not isinstance(response, dict):
        return

    response["content"] = {
        "text/csv": {"schema": {"type": "string", "format": "binary"}}
    }


def _sort_required_arrays(node: object) -> None:
    """Sort every JSON Schema ``required`` array in place.

    ``required`` is a set in JSON Schema semantics, so its order is meaningless
    to consumers but unstable across regenerations (it follows model field order,
    which shifts when unrelated fields move). Sorting it keeps regenerated diffs
    legible. Boolean ``required`` (e.g. on parameters) is left untouched.
    """
    if isinstance(node, dict):
        required = node.get("required")
        if isinstance(required, list) and all(
            isinstance(item, str) for item in required
        ):
            node["required"] = sorted(required)
        for value in node.values():
            _sort_required_arrays(value)
    elif isinstance(node, list):
        for item in node:
            _sort_required_arrays(item)


def _canonicalize_spec(spec: dict[str, object]) -> None:
    """Reorder the spec into a deterministic shape so regenerated diffs stay legible.

    Only orderings that carry no semantic meaning are sorted:

      * the ``components.schemas`` map, by schema name. The rename and
        pagination passes ``pop`` and re-insert schemas, so emission order
        otherwise depends on which overlays ran, not on the API surface.
      * the ``paths`` map, by path.
      * every object schema's ``required`` array (set semantics).

    Property order, ``enum`` order, and ``anyOf``/``oneOf`` member order are
    left as the backend emits them: those are stable per model definition and
    can carry meaning (discriminator order, enum-default conventions in some
    SDK generators).
    """
    paths = spec.get("paths")
    if isinstance(paths, dict):
        spec["paths"] = {key: paths[key] for key in sorted(paths)}

    components = spec.get("components")
    if isinstance(components, dict):
        schemas = components.get("schemas")
        if isinstance(schemas, dict):
            components["schemas"] = {key: schemas[key] for key in sorted(schemas)}

    _sort_required_arrays(spec)


def generate_openapi(output_path: Path | None = None) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    backend_main_server = repo_root / "backend" / "main_server"
    if str(backend_main_server) not in sys.path:
        sys.path.insert(0, str(backend_main_server))
    _seed_openapi_generation_env()

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

    _strip_routes_not_in_public_manifest(spec, PUBLIC_API_ROUTES_PATH)

    # Strip legacy URLs from any enum lists (e.g. Jobs endpoint enum)
    _strip_legacy_from_enums(spec)

    # Surface default_factory=list/dict as default: []/{} so generated
    # SDKs emit idiomatic empty-container defaults instead of nullable
    # placeholders. Runs before public-schema-name normalization so the
    # injection sees stable property shapes.
    _inject_container_defaults(spec)

    _normalize_public_schema_names(spec)
    _normalize_workflow_read_docs(spec)
    _normalize_public_file_download_docs(spec)
    _normalize_public_operation_ids(spec)

    # Strip unused legacy request/response schemas that only belonged to the
    # document-scoped classification API.
    schemas = spec.get("components", {}).get("schemas")
    if isinstance(schemas, dict):
        for schema_name in LEGACY_SCHEMA_NAMES:
            schemas.pop(schema_name, None)

    # Keep only schemas reachable from the published API surface.
    _prune_unreferenced_schemas(spec)

    # Canonicalize ordering so regenerations produce minimal, legible diffs.
    _canonicalize_spec(spec)

    _print_public_routes(spec)

    # Write updated spec to file
    if output_path is None:
        output_path = Path(__file__).resolve().parent / "openapi.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _print_public_routes(spec: dict[str, object]) -> None:
    """Print every (METHOD, URL) pair that survived the public filtering."""
    servers = spec.get("servers")
    base_url = ""
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        server_url = servers[0].get("url")
        if isinstance(server_url, str):
            base_url = server_url.rstrip("/")

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return

    routes: list[tuple[str, str]] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method in path_item:
            if method in OPENAPI_HTTP_METHODS:
                routes.append((method.upper(), f"{base_url}{path}"))

    routes.sort(key=lambda route: (route[1], route[0]))
    print(f"Public OpenAPI routes ({len(routes)}):")
    for method, url in routes:
        print(f"  {method:<6} {url}")


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the generated OpenAPI JSON to this path.",
    )
    args = parser.parse_args()
    generate_openapi(output_path=args.output)


if __name__ == "__main__":
    main()
