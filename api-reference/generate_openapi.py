import requests
import json

def generate_openapi() -> None:
    # Fetch OpenAPI specification from local server
    response = requests.get("http://localhost:4000/openapi.json")
    spec = response.json()

    # Update security schemes
    spec["components"]["securitySchemes"] = {
        "API Key": {
            "type": "apiKey",
            "in": "header",
            "name": "Api-Key"
        }
    }

    # Update servers
    spec["servers"] = [
        {
            "url": "https://api.retab.dev"
        }
    ]

    # Write updated spec to file
    with open("openapi.json", "w") as f:
        json.dump(spec, f, indent=2)

if __name__ == "__main__":
    generate_openapi()
