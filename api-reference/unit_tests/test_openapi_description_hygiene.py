"""Guardrail: public OpenAPI descriptions must not leak internal details.

Every ``description``/``summary`` string in the generated public spec is
authored in backend Python source (FastAPI route docstrings, Pydantic model
docstrings, ``Field(description=...)``), and surfaces verbatim in the Mintlify
API reference. This test fails if any of that prose contains a token that only
makes sense to someone who has read our repo — internal service names, storage
model names, design-doc references, or RST/Sphinx syntax that Markdown does not
render.

When this fails, fix the prose at the source in ``backend/main_server`` and
regenerate ``openapi.json`` (see ``.notes/openapi-description-rules.md``). Do
not edit ``openapi.json`` directly, and do not weaken the banned list to silence
a failure — the failure means a description needs cleaning, not that the rule is
wrong.
"""

import json
import re
from pathlib import Path


GENERATED_OPENAPI = (
    Path(__file__).resolve().parents[1] / "openapi.json"
)

# Each entry is (human-readable reason, compiled pattern). A description/summary
# is a violation if it matches any pattern. Keep reasons specific so the failure
# message tells the author what to remove.
BANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Internal infrastructure / service names.
    ("names Temporal", re.compile(r"\bTemporal\b")),
    ("names the orchestrator service", re.compile(r"orchestrator", re.IGNORECASE)),
    (
        "names a backend service module",
        re.compile(r"\b(main_server|llm_server|preprocessing_server)\b"),
    ),
    (
        "names the datastore (Mongo/Atlas/BSON)",
        re.compile(r"\b(mongo|mongodb|atlas|bson)\b", re.IGNORECASE),
    ),
    ("names the cache (Redis/Valkey)", re.compile(r"\b(redis|valkey)\b", re.IGNORECASE)),
    (
        "leaks the backing collection",
        re.compile(r"\bcollections?\b", re.IGNORECASE),
    ),
    # Internal storage models / persistence concerns.
    ("names an internal Stored* model", re.compile(r"\bStored[A-Z]\w+")),
    (
        "contrasts against the internal storage model",
        re.compile(r"internal storage model", re.IGNORECASE),
    ),
    ("mentions persistence-only fields", re.compile(r"persistence-only", re.IGNORECASE)),
    (
        "leaks internal storage row / sidecar mechanics",
        re.compile(
            r"\b(storage row|store path|step row|queue handle|storage model|sidecar)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "describes a field/model as internal instead of documenting it",
        re.compile(r"\binternal(ly)?\b", re.IGNORECASE),
    ),
    ("mentions tenant isolation", re.compile(r"tenant isolation", re.IGNORECASE)),
    (
        "explains a temporary compatibility hack",
        re.compile(r"retained temporarily", re.IGNORECASE),
    ),
    (
        "leaks read-time backfill mechanics",
        re.compile(r"backfilled at read time", re.IGNORECASE),
    ),
    # Internal design-doc references / rationale jargon.
    ("references the meta-pattern blueprint", re.compile(r"meta-pattern", re.IGNORECASE)),
    ("references an internal blueprint", re.compile(r"blueprint", re.IGNORECASE)),
    ("uses a blueprint section sign", re.compile(r"§")),
    ("uses design-rationale jargon", re.compile(r"flat-resource", re.IGNORECASE)),
    ("uses design-rationale jargon", re.compile(r"first-class", re.IGNORECASE)),
    (
        "uses design-rationale jargon",
        re.compile(r"action-(verb|endpoint)", re.IGNORECASE),
    ),
    ("uses security-design jargon", re.compile(r"confused-deputy", re.IGNORECASE)),
    ("leaks pipeline enrichment internals", re.compile(r"enrichment pass", re.IGNORECASE)),
    ("references an internal design doc", re.compile(r"API_DESIGN", re.IGNORECASE)),
    (
        "leaks the id-generation scheme",
        re.compile(r"<nanoid>|\bnanoid\b", re.IGNORECASE),
    ),
    ("uses design-rationale jargon", re.compile(r"denormalized", re.IGNORECASE)),
    # Pydantic/FastAPI implementation terms.
    (
        "names Pydantic/FastAPI internals",
        re.compile(r"\b(pydantic|fastapi|default_factory|model_validate)\b", re.IGNORECASE),
    ),
    # Meta descriptions that restate the HTTP method/path instead of saying what
    # the endpoint/body does. "Body for POST /v1/..." reads as boilerplate in the
    # rendered docs; describe the action instead.
    (
        "restates the route instead of describing the action",
        re.compile(
            r"\b(request )?body for\b.*\b(POST|GET|PATCH|PUT|DELETE)\b"
            r"|^\s*(POST|GET|PATCH|PUT|DELETE)\s+/",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    # RST/Sphinx syntax that Markdown (Mintlify) does not render.
    (
        "uses an RST cross-reference role",
        re.compile(r":(class|func|meth|mod|py|attr|obj|exc|data|const):"),
    ),
    ("uses RST double backticks (use single)", re.compile(r"``")),
)


def _iter_description_strings(node: object, location: str):
    """Yield (location, text) for every description/summary in the spec.

    Discriminator ``mapping`` objects are skipped: their keys are
    discriminator *values* (e.g. ``summary``, ``by_document``) and their
    values are ``$ref`` strings, not prose — a key that happens to be named
    ``summary`` there is not a documentation string.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "mapping":
                continue
            if key in ("description", "summary") and isinstance(value, str):
                yield (f"{location}/{key}", value)
            else:
                yield from _iter_description_strings(value, f"{location}/{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _iter_description_strings(item, f"{location}/{index}")


def test_public_openapi_descriptions_have_no_internal_leaks() -> None:
    spec = json.loads(GENERATED_OPENAPI.read_text())

    violations: list[str] = []
    for location, text in _iter_description_strings(spec, ""):
        reasons = sorted(
            {reason for reason, pattern in BANNED_PATTERNS if pattern.search(text)}
        )
        if reasons:
            snippet = re.sub(r"\s+", " ", text).strip()[:160]
            violations.append(
                f"  {location}\n"
                f"    reasons: {', '.join(reasons)}\n"
                f"    text:    {snippet!r}"
            )

    assert not violations, (
        "Public OpenAPI description/summary prose leaks internal details. Fix the "
        "source docstrings/Field descriptions in backend/main_server and regenerate "
        "openapi.json (see .notes/openapi-description-rules.md):\n\n"
        + "\n".join(sorted(violations))
    )
