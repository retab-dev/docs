import json
import sys
from pathlib import Path


LEGACY_CLASSIFICATION_PATHS = {
    "/v1/documents/classify",
    "/v1/documents/classifications",
    "/v1/documents/classifications/count",
    "/v1/documents/classifications/{classification_id}",
}

LEGACY_PARSE_PATHS = {
    "/v1/documents/parse",
    "/v1/documents/parses",
    "/v1/documents/parses/count",
    "/v1/documents/parses/{parsing_id}",
}


def remove_legacy_classification_docs(node: object) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "enum" and isinstance(value, list):
                node[key] = [item for item in value if item not in LEGACY_CLASSIFICATION_PATHS]
            else:
                remove_legacy_classification_docs(value)
    elif isinstance(node, list):
        for item in node:
            remove_legacy_classification_docs(item)


def remove_legacy_parse_docs(node: object) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "enum" and isinstance(value, list):
                node[key] = [item for item in value if item not in LEGACY_PARSE_PATHS]
            else:
                remove_legacy_parse_docs(value)
    elif isinstance(node, list):
        for item in node:
            remove_legacy_parse_docs(item)


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

    for legacy_path in LEGACY_CLASSIFICATION_PATHS:
        spec["paths"].pop(legacy_path, None)
    remove_legacy_classification_docs(spec)

    for legacy_path in LEGACY_PARSE_PATHS:
        spec["paths"].pop(legacy_path, None)
    remove_legacy_parse_docs(spec)

    # Write updated spec to file
    output_path = Path(__file__).resolve().parent / "openapi.json"
    with output_path.open("w") as f:
        json.dump(spec, f, indent=2)


if __name__ == "__main__":
    generate_openapi()
