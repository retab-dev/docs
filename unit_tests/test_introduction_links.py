import json
import re
from pathlib import Path
from urllib.parse import urlparse


DOCS_ROOT = Path(__file__).resolve().parents[1]
LEGACY_API_REFERENCE_REDIRECTS = {
    "/api-reference/documents/parse": "https://docs.retab.com/api-reference/parses/create",
    "/api-reference/documents/extract": "https://docs.retab.com/api-reference/extractions/create",
    "/api-reference/documents/edit": "https://docs.retab.com/api-reference/edits/create",
    "/api-reference/edit/agent/fill": "https://docs.retab.com/api-reference/edits/create",
    "/api-reference/documents/split": "https://docs.retab.com/api-reference/splits/create",
    "/api-reference/documents/partition": "https://docs.retab.com/api-reference/partitions/create",
    "/api-reference/documents/classify": "https://docs.retab.com/api-reference/classifications/create",
}


def _docs_page_exists(link: str, source_file: Path) -> bool:
    parsed = urlparse(link)
    if parsed.scheme or parsed.netloc or link.startswith("#") or link.startswith("mailto:"):
        return True

    link_path = parsed.path
    if link_path.startswith("/"):
        candidate = DOCS_ROOT / link_path.lstrip("/")
    else:
        candidate = source_file.parent / link_path

    candidate = candidate.resolve()
    if candidate.is_file():
        return True

    if candidate.suffix:
        return False

    return any(candidate.with_suffix(suffix).is_file() for suffix in (".mdx", ".md"))


def test_introduction_internal_href_links_resolve_to_docs_pages() -> None:
    source_file = DOCS_ROOT / "overview" / "introduction.mdx"
    content = source_file.read_text()
    hrefs = re.findall(r'href="([^"]+)"', content)

    broken_links = [href for href in hrefs if not _docs_page_exists(href, source_file)]

    assert broken_links == []


def test_legacy_api_reference_links_redirect_to_current_create_pages() -> None:
    docs_config = json.loads((DOCS_ROOT / "docs.json").read_text())
    redirects = {
        redirect["source"]: redirect["destination"]
        for redirect in docs_config["redirects"]
    }

    for source, destination in LEGACY_API_REFERENCE_REDIRECTS.items():
        assert redirects[source] == destination
        assert _docs_page_exists(urlparse(destination).path, DOCS_ROOT / "docs.json")
