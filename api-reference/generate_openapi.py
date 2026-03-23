import json
import sys
from pathlib import Path


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

    # Write updated spec to file
    output_path = Path(__file__).resolve().parent / "openapi.json"
    with output_path.open("w") as f:
        json.dump(spec, f, indent=2)


if __name__ == "__main__":
    generate_openapi()
