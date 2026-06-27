#!/usr/bin/env python3
"""Extract fenced code blocks from ``public/docs/**/*.{md,mdx}`` and lint
them against the local Retab SDKs.

The script keeps drift between the docs and the generated SDKs honest. For each
fenced block tagged with a language we know how to check we:

  1. Write the block to a temporary file under ``public/docs/.snippets/``.
  2. Run a language-appropriate checker:
       * Python   -> ``py_compile`` (syntax) + ``ruff check`` (undefined names)
                      + ``pyright`` if available (full type-check against the
                      installed ``retab`` package).
       * TypeScript -> ``tsc --noEmit`` against the local
                      ``@retab/node`` SDK via a synthetic ``tsconfig.json``.
       * Go       -> ``go test`` in a synthetic module that replaces the
                      public SDK import with the local Go SDK.
       * Rust     -> ``cargo check`` in a synthetic crate that depends on the
                      local Rust SDK.
       * PHP      -> ``php -l`` syntax checks.
       * Ruby     -> ``ruby -c`` syntax checks.
       * .NET     -> Roslyn ``csc`` through ``dotnet`` against the local .NET
                      SDK assembly.
       * Java     -> ``javac`` against the local Java SDK classes.
  3. Check docs structure:
       * Node snippets must use TypeScript fences, not JavaScript fences.
       * SDK example groups must include Python, TypeScript, Go, Rust, .NET,
         PHP, Ruby, and Java variants.
  4. Aggregate results and print a per-source-file punch list.

Exit code is non-zero when any snippet fails. The script is intended as a
local guardrail / CI hook — production builds do not depend on it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
from collections.abc import Callable
from collections import Counter
import hashlib
import json
import os
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_REPO_ROOT = Path(os.environ.get("RETAB_REPO_ROOT", REPO_ROOT)).resolve()
DOCS_ROOT = REPO_ROOT / "open-source" / "docs"
PY_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "python"
PY_SDK_FOR_SNIPPETS = Path(os.environ.get("RETAB_PYTHON_SDK_ROOT", PY_SDK)).resolve()
NODE_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "node"
NODE_SDK_FOR_SNIPPETS = Path(os.environ.get("RETAB_NODE_SDK_ROOT", NODE_SDK))
GO_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "go"
GO_SDK_FOR_SNIPPETS = Path(os.environ.get("RETAB_GO_SDK_ROOT", GO_SDK))
RUST_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "rust"
RUST_SDK_FOR_SNIPPETS = Path(os.environ.get("RETAB_RUST_SDK_ROOT", RUST_SDK))
JAVA_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "java"
JAVA_SDK_FOR_SNIPPETS = Path(os.environ.get("RETAB_JAVA_SDK_ROOT", JAVA_SDK))
DOTNET_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "dotnet"
DOTNET_SDK_FOR_SNIPPETS = Path(os.environ.get("RETAB_DOTNET_SDK_ROOT", DOTNET_SDK))
PHP_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "php"
PHP_SDK_FOR_SNIPPETS = Path(os.environ.get("RETAB_PHP_SDK_ROOT", PHP_SDK))
RUBY_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "ruby"
RUBY_SDK_FOR_SNIPPETS = Path(os.environ.get("RETAB_RUBY_SDK_ROOT", RUBY_SDK))
PY_VENV = REPO_ROOT / "backend" / "main_server" / ".venv"

RUFF = PY_VENV / "bin" / "ruff"
PYTHON = PY_VENV / "bin" / "python"
PYRIGHT = PY_VENV / "bin" / "pyright"
TSC = NODE_SDK / "node_modules" / "typescript" / "bin" / "tsc"

NODE = shutil.which("node")
GO = shutil.which("go")
CARGO = shutil.which("cargo")
MAVEN = shutil.which("mvn")
PHP = shutil.which("php")
RUBY = shutil.which("ruby")
DOTNET = shutil.which("dotnet")
JAVAC = shutil.which("javac")

SNIPPET_ROOT = DOCS_ROOT / ".snippets"
SNIPPET_DIR = SNIPPET_ROOT / f"run_{os.getpid()}_{uuid.uuid4().hex[:12]}"

os.environ.setdefault(
    "NUGET_PACKAGES",
    str(REPO_ROOT / ".cache" / "bazel-local-home" / "nuget-packages"),
)
os.environ.setdefault(
    "DOTNET_CLI_HOME",
    str(REPO_ROOT / ".cache" / "bazel-local-home" / "dotnet"),
)
os.environ.setdefault("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1")
os.environ.setdefault("DOTNET_NOLOGO", "1")
# The Bazel `code_snippet_lint` target runs no-sandbox but with a scrubbed
# environment (HOME is not propagated), so `go test` cannot derive
# GOPATH/GOMODCACHE/GOCACHE and fails with "module cache not found". Mirror the
# NUGET/DOTNET cache wiring above and point go at writable repo-local caches;
# the single external dep (go-querystring) is fetched once and reused (the
# target is no-sandbox, so network access is available).
os.environ.setdefault(
    "GOMODCACHE",
    str(REPO_ROOT / ".cache" / "bazel-local-home" / "go" / "pkg" / "mod"),
)
os.environ.setdefault(
    "GOCACHE",
    str(REPO_ROOT / ".cache" / "bazel-local-home" / "go-build"),
)
NUGET_PACKAGES = Path(os.environ["NUGET_PACKAGES"])
_RUST_SNIPPET_TARGET_DIR = os.environ.get("RETAB_RUST_SNIPPET_TARGET_DIR") or os.environ.get(
    "CARGO_TARGET_DIR"
)
RUST_SNIPPET_TARGET_DIR = (
    Path(_RUST_SNIPPET_TARGET_DIR).resolve()
    if _RUST_SNIPPET_TARGET_DIR
    else None
)
RUST_SNIPPET_WORKSPACE_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_RUST_SNIPPET_WORKSPACE_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-rust-workspaces"),
    )
).resolve()
RUST_SNIPPET_SDK_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_RUST_SNIPPET_SDK_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-rust-sdk"),
    )
).resolve()
PYTHON_SNIPPET_SUCCESS_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_PYTHON_SNIPPET_SUCCESS_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-python-success"),
    )
).resolve()
GO_SNIPPET_WORKSPACE_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_GO_SNIPPET_WORKSPACE_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-go-workspaces"),
    )
).resolve()
DOTNET_SNIPPET_SDK_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_DOTNET_SNIPPET_SDK_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-dotnet-sdk"),
    )
).resolve()
DOTNET_SNIPPET_COMPILE_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_DOTNET_SNIPPET_COMPILE_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-dotnet-compile"),
    )
).resolve()
JAVA_SNIPPET_SDK_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_JAVA_SNIPPET_SDK_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-java-sdk"),
    )
).resolve()
JAVA_SNIPPET_COMPILE_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_JAVA_SNIPPET_COMPILE_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-java-compile"),
    )
).resolve()
TS_SNIPPET_WORKSPACE_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_TS_SNIPPET_WORKSPACE_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-ts-workspaces"),
    )
).resolve()
PHP_SNIPPET_SUCCESS_CACHE_DIR = Path(
    os.environ.get(
        "RETAB_PHP_SNIPPET_SUCCESS_CACHE_DIR",
        str(CACHE_REPO_ROOT / ".cache" / "docs-snippet-php-success"),
    )
).resolve()

LANG_ALIASES: dict[str, str] = {
    "python": "python",
    "py": "python",
    "javascript": "javascript",
    "js": "javascript",
    "typescript": "typescript",
    "ts": "typescript",
    "tsx": "typescript",
    "go": "go",
    "golang": "go",
    "rust": "rust",
    "rs": "rust",
    "java": "java",
    "dotnet": "dotnet",
    ".net": "dotnet",
    "net": "dotnet",
    "csharp": "dotnet",
    "c#": "dotnet",
    "cs": "dotnet",
    "ruby": "ruby",
    "rb": "ruby",
    "php": "php",
    "sh": "shell",
    "shell": "shell",
    "bash": "shell",
    "zsh": "shell",
}

COVERAGE_LANG_ALIASES: dict[str, str] = {
    "python": "python",
    "py": "python",
    "typescript": "typescript",
    "ts": "typescript",
    "tsx": "typescript",
    "javascript": "javascript",
    "js": "javascript",
    "node": "typescript",
    "node.js": "typescript",
    "go": "go",
    "golang": "go",
    "rust": "rust",
    "rs": "rust",
    "java": "java",
    "dotnet": "dotnet",
    ".net": "dotnet",
    "net": "dotnet",
    "csharp": "dotnet",
    "c#": "dotnet",
    "cs": "dotnet",
    "ruby": "ruby",
    "rb": "ruby",
    "php": "php",
}

REQUIRED_SDK_GROUP_LANGUAGES = (
    "python",
    "typescript",
    "go",
    "rust",
    "dotnet",
    "php",
    "ruby",
    "java",
)

PACKAGE_MANAGER_TABS = {
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "pip",
    "poetry",
    "cargo",
    "gem",
    "composer",
}

NON_SDK_TABS = {
    "curl",
    "json",
    "response",
    "output",
    "bash",
    "shell",
    "sh",
    "zsh",
    "fish",
    "powershell",
}

LANGUAGE_DISPLAY_NAMES = {
    "python": "Python",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "go": "Go",
    "rust": "Rust",
    "dotnet": ".NET",
    "php": "PHP",
    "ruby": "Ruby",
    "java": "Java",
}

GROUP_COMPONENTS = ("CodeGroup", "RequestExample")
LINT_LANGUAGES = (
    "python",
    "typescript",
    "go",
    "rust",
    "dotnet",
    "php",
    "ruby",
    "java",
)

# Files / paths we never lint (vendored caches, etc.).
PATH_BLOCKLIST = (
    ".pytest_cache",
    ".snippets",
    ".scripts",
)

FENCE_RE = re.compile(r"^(?P<fence>`{3,})(?P<info>[^\n]*)$")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snippet:
    """A single fenced code block extracted from a doc file."""

    source: Path
    index: int  # 0-based ordinal within source
    start_line: int  # 1-based line of opening fence
    raw_language: str
    language: str
    title: str
    code: str

    @property
    def display(self) -> str:
        rel = self.source.relative_to(DOCS_ROOT)
        return f"{rel}:{self.start_line} [{self.language}]"


@dataclass(frozen=True)
class CodeGroup:
    """A docs component that groups snippets into language tabs."""

    source: Path
    component: str
    start_line: int
    end_line: int
    snippets: tuple[Snippet, ...]


SnippetManifest = tuple[list[Path], list[Snippet], list[CodeGroup]]
SNIPPET_MANIFEST_VERSION = 1


def _info_to_language(info: str) -> tuple[str | None, str, str]:
    info = info.strip()
    if not info:
        return None, "", ""
    parts = info.split(None, 1)
    raw_lang = parts[0].lower().strip("`")
    title = parts[1] if len(parts) > 1 else ""
    return LANG_ALIASES.get(raw_lang), title, raw_lang


def extract_snippets(path: Path) -> list[Snippet]:
    """Parse fenced blocks. Closing fence must match length of opening fence.

    MDX in this repo occasionally mismatches fences (e.g. opens with ``` and
    closes with ````). To minimise silent skips we treat any line that is
    *only* backticks (length >= opening fence) as a valid closer.
    """
    snippets: list[Snippet] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    i = 0
    ordinal = 0
    while i < len(lines):
        line = lines[i].rstrip()
        m = FENCE_RE.match(line)
        if not m:
            i += 1
            continue
        fence = m.group("fence")
        info = m.group("info")
        language, title, raw_language = _info_to_language(info)
        start = i
        # Walk forward until we hit a closer.
        i += 1
        body: list[str] = []
        while i < len(lines):
            inner = lines[i].rstrip()
            # A bare run of >= len(fence) backticks (no info) closes the block.
            if re.fullmatch(r"`{%d,}" % len(fence), inner):
                break
            body.append(lines[i])
            i += 1
        # Consume the closing fence (or EOF).
        if i < len(lines):
            i += 1
        if language is None:
            continue
        snippets.append(
            Snippet(
                source=path,
                index=ordinal,
                start_line=start + 1,
                raw_language=raw_language,
                language=language,
                title=title,
                code="\n".join(body) + ("\n" if body else ""),
            )
        )
        ordinal += 1
    return snippets


def extract_code_groups(path: Path, snippets: list[Snippet]) -> list[CodeGroup]:
    """Find snippet-grouping components and attach the contained snippets."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    groups: list[CodeGroup] = []
    stack: list[tuple[str, int]] = []
    open_re = re.compile(r"<(?P<name>%s)\b" % "|".join(GROUP_COMPONENTS))
    close_re = re.compile(r"</(?P<name>%s)>" % "|".join(GROUP_COMPONENTS))

    for idx, line in enumerate(lines, start=1):
        for match in open_re.finditer(line):
            stack.append((match.group("name"), idx))
        close_match = close_re.search(line)
        if close_match is None:
            continue
        component = close_match.group("name")
        for stack_idx in range(len(stack) - 1, -1, -1):
            open_component, start_line = stack[stack_idx]
            if open_component != component:
                continue
            del stack[stack_idx:]
            contained = tuple(
                snippet
                for snippet in snippets
                if start_line < snippet.start_line < idx
            )
            groups.append(
                CodeGroup(
                    source=path,
                    component=component,
                    start_line=start_line,
                    end_line=idx,
                    snippets=contained,
                )
            )
            break
    return groups


def iter_doc_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".md", ".mdx"}:
            continue
        rel = path.relative_to(root)
        if any(part in PATH_BLOCKLIST for part in rel.parts):
            continue
        out.append(path)
    return out


def extract_manifest_from_docs(docs: list[Path]) -> SnippetManifest:
    all_snippets: list[Snippet] = []
    all_groups: list[CodeGroup] = []
    for path in docs:
        snippets = extract_snippets(path)
        all_snippets.extend(snippets)
        all_groups.extend(extract_code_groups(path, snippets))
    return docs, all_snippets, all_groups


def _source_to_manifest_path(source: Path) -> str:
    return source.relative_to(DOCS_ROOT).as_posix()


def _manifest_path_to_source(value: str) -> Path:
    return DOCS_ROOT / value


def _snippet_manifest_key(snippet: Snippet) -> tuple[str, int, int]:
    return (
        _source_to_manifest_path(snippet.source),
        snippet.index,
        snippet.start_line,
    )


def write_snippet_manifest(path: Path, manifest: SnippetManifest) -> None:
    docs, snippets, groups = manifest
    snippet_index_by_key = {
        _snippet_manifest_key(snippet): index
        for index, snippet in enumerate(snippets)
    }
    payload = {
        "version": SNIPPET_MANIFEST_VERSION,
        "docs": [_source_to_manifest_path(doc) for doc in docs],
        "snippets": [
            {
                "source": _source_to_manifest_path(snippet.source),
                "index": snippet.index,
                "start_line": snippet.start_line,
                "raw_language": snippet.raw_language,
                "language": snippet.language,
                "title": snippet.title,
                "code": snippet.code,
            }
            for snippet in snippets
        ],
        "groups": [
            {
                "source": _source_to_manifest_path(group.source),
                "component": group.component,
                "start_line": group.start_line,
                "end_line": group.end_line,
                "snippet_indices": [
                    snippet_index_by_key[_snippet_manifest_key(snippet)]
                    for snippet in group.snippets
                ],
            }
            for group in groups
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")


def load_snippet_manifest(path: Path) -> SnippetManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != SNIPPET_MANIFEST_VERSION:
        raise ValueError(
            f"unsupported snippet manifest version: {payload.get('version')!r}"
        )
    docs = [_manifest_path_to_source(item) for item in payload["docs"]]
    snippets = [
        Snippet(
            source=_manifest_path_to_source(item["source"]),
            index=item["index"],
            start_line=item["start_line"],
            raw_language=item["raw_language"],
            language=item["language"],
            title=item["title"],
            code=item["code"],
        )
        for item in payload["snippets"]
    ]
    groups = [
        CodeGroup(
            source=_manifest_path_to_source(item["source"]),
            component=item["component"],
            start_line=item["start_line"],
            end_line=item["end_line"],
            snippets=tuple(snippets[index] for index in item["snippet_indices"]),
        )
        for item in payload["groups"]
    ]
    return docs, snippets, groups


def _matches_source_filter(source: Path, filter_value: str) -> bool:
    if not filter_value:
        return True
    rel = _source_to_manifest_path(source)
    return (
        filter_value in str(source)
        or filter_value in rel
        or filter_value in f"public/docs/{rel}"
    )


def filter_snippet_manifest(
    manifest: SnippetManifest,
    filter_value: str,
) -> SnippetManifest:
    if not filter_value:
        return manifest
    docs, snippets, groups = manifest
    filtered_docs = [doc for doc in docs if _matches_source_filter(doc, filter_value)]
    filtered_snippets = [
        snippet
        for snippet in snippets
        if _matches_source_filter(snippet.source, filter_value)
    ]
    included_keys = {_snippet_manifest_key(snippet) for snippet in filtered_snippets}
    filtered_groups = [
        CodeGroup(
            source=group.source,
            component=group.component,
            start_line=group.start_line,
            end_line=group.end_line,
            snippets=tuple(
                snippet
                for snippet in group.snippets
                if _snippet_manifest_key(snippet) in included_keys
            ),
        )
        for group in groups
        if _matches_source_filter(group.source, filter_value)
    ]
    return filtered_docs, filtered_snippets, filtered_groups


# ---------------------------------------------------------------------------
# Snippet filtering — only lint snippets that *touch* the SDK surface. A
# snippet that only declares ``client = Retab()`` or builds a Pydantic model
# is fair game; one that is purely shell-style demonstration of unrelated
# tooling (e.g. ``import { useState }`` for a React example) is not, and
# would just generate noise.
# ---------------------------------------------------------------------------


def is_self_contained_python(snippet: Snippet) -> bool:
    return "from retab" in snippet.code or "import retab" in snippet.code


def is_self_contained_ts(snippet: Snippet) -> bool:
    return "@retab/node" in snippet.code


def is_self_contained_go(snippet: Snippet) -> bool:
    return (
        "github.com/retab-dev/retab/clients/go" in snippet.code
        or re.search(r"(?m)^\s*package\s+\w+", snippet.code) is not None
    )


def is_self_contained_rust(snippet: Snippet) -> bool:
    return (
        "use retab" in snippet.code
        or "retab::" in snippet.code
        or re.search(r"\bRetab::", snippet.code) is not None
    )


def is_self_contained_php(snippet: Snippet) -> bool:
    return (
        "use Retab\\" in snippet.code
        or "\\Retab\\" in snippet.code
        or "new Client(" in snippet.code
        or "Retab\\Client" in snippet.code
    )


def is_self_contained_ruby(snippet: Snippet) -> bool:
    return (
        "require 'retab'" in snippet.code
        or 'require "retab"' in snippet.code
        or "Retab::" in snippet.code
    )


def is_self_contained_java(snippet: Snippet) -> bool:
    return bool(snippet.code.strip())


def _dotnet_identifier_is_declared(code: str, identifier: str) -> bool:
    declaration = rf"\b(?:var|string|object|dynamic|[A-Z][A-Za-z0-9_<>,?\[\]\s]*)\s+{re.escape(identifier)}\s*="
    return re.search(declaration, code) is not None


def is_self_contained_dotnet(snippet: Snippet) -> bool:
    code = snippet.code
    uses_retab = "using Retab" in code or re.search(r"\bnew Retab(?:Client)?\(", code) is not None
    if not uses_retab:
        return False
    if re.search(r"\bclient\.", code) is not None and not _dotnet_identifier_is_declared(code, "client"):
        return False
    for identifier in (
        "document",
        "experiment",
        "job",
        "mySchema",
        "run",
        "schema",
        "yamlDefinition",
    ):
        if re.search(rf"\b{re.escape(identifier)}\b", code) is not None and not _dotnet_identifier_is_declared(
            code, identifier
        ):
            return False
    return True


def is_contextual_python_sdk_snippet(snippet: Snippet) -> bool:
    return (
        snippet.language == "python"
        and not is_self_contained_python(snippet)
        and re.search(r"\bclient\.", snippet.code) is not None
    )


def is_contextual_ts_sdk_snippet(snippet: Snippet) -> bool:
    return (
        snippet.language in {"typescript", "javascript"}
        and not is_self_contained_ts(snippet)
        and re.search(r"\bclient\.", snippet.code) is not None
    )


def is_contextual_sdk_snippet(snippet: Snippet) -> bool:
    if snippet.language == "python":
        return is_contextual_python_sdk_snippet(snippet)
    if snippet.language in {"typescript", "javascript"}:
        return is_contextual_ts_sdk_snippet(snippet)
    if snippet.language == "go":
        return not is_self_contained_go(snippet) and re.search(r"\bclient\.", snippet.code) is not None
    if snippet.language == "rust":
        return not is_self_contained_rust(snippet) and re.search(r"\bclient\.", snippet.code) is not None
    if snippet.language == "java":
        return not is_self_contained_java(snippet) and re.search(r"\bclient\.", snippet.code) is not None
    if snippet.language == "php":
        return not is_self_contained_php(snippet) and "$client->" in snippet.code
    if snippet.language == "ruby":
        return not is_self_contained_ruby(snippet) and re.search(r"\bclient\.", snippet.code) is not None
    if snippet.language == "dotnet":
        return not is_self_contained_dotnet(snippet) and re.search(r"\bclient\.", snippet.code) is not None
    return False


# ---------------------------------------------------------------------------
# Structural docs checks
# ---------------------------------------------------------------------------


def _normalise_tab_token(value: str) -> str:
    return value.lower().strip().strip("`[]{}(),:")


def _first_title_token(title: str) -> str:
    if not title:
        return ""
    return _normalise_tab_token(title.split()[0])


def snippet_coverage_language(snippet: Snippet) -> str | None:
    """Return the SDK language represented by a group tab.

    Prefer the fence language when it is a programming SDK language. Fall back
    to the first title token so install snippets such as `````sh Python`````
    still count toward the Python SDK tab.
    """
    raw = _normalise_tab_token(snippet.raw_language)
    raw_coverage = COVERAGE_LANG_ALIASES.get(raw)
    if raw_coverage is not None and raw not in {"sh", "shell", "bash", "zsh"}:
        return raw_coverage
    title_token = _first_title_token(snippet.title)
    return COVERAGE_LANG_ALIASES.get(title_token)


def should_require_sdk_coverage(group: CodeGroup) -> bool:
    """Return whether a grouped example must show every SDK language.

    Some CodeGroups are package-manager install tabs, JSON output tabs, or
    shell/cURL examples. Language CodeGroups must include every public SDK
    language, regardless of whether the example calls Retab directly.
    """
    if not group.snippets:
        return False

    languages = {
        language
        for snippet in group.snippets
        if (language := snippet_coverage_language(snippet)) is not None
    }
    if not languages:
        return False

    titles = {_first_title_token(snippet.title) for snippet in group.snippets}
    raw_languages = {
        _normalise_tab_token(snippet.raw_language) for snippet in group.snippets
    }
    shell_languages = {"sh", "shell", "bash", "zsh"}

    if raw_languages <= shell_languages:
        return False
    if titles and titles <= PACKAGE_MANAGER_TABS | NON_SDK_TABS:
        return False

    return bool(languages & set(REQUIRED_SDK_GROUP_LANGUAGES))


def _display_languages(languages: set[str]) -> str:
    return ", ".join(
        LANGUAGE_DISPLAY_NAMES.get(language, language)
        for language in REQUIRED_SDK_GROUP_LANGUAGES
        if language in languages
    )


def check_javascript_fences(snippets: list[Snippet]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for snippet in snippets:
        raw = _normalise_tab_token(snippet.raw_language)
        title = _first_title_token(snippet.title)
        if raw in {"javascript", "js"}:
            issues.append(
                LintIssue.for_snippet(
                    snippet,
                    "language",
                    "Use a TypeScript fence (```typescript) for code snippets; "
                    f"found ```{snippet.raw_language}.",
                )
            )
            continue
        if raw in {"typescript", "ts", "tsx"} and title in {"javascript", "js"}:
            issues.append(
                LintIssue.for_snippet(
                    snippet,
                    "language",
                    "Use a TypeScript tab label for TypeScript snippets; "
                    f"found title '{snippet.title}'.",
                )
            )
    return issues


def check_code_group_coverage(groups: list[CodeGroup]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    required = set(REQUIRED_SDK_GROUP_LANGUAGES)
    for group in groups:
        if not should_require_sdk_coverage(group):
            continue
        present: set[str] = set()
        has_javascript_tab = False
        for snippet in group.snippets:
            language = snippet_coverage_language(snippet)
            if language is None:
                continue
            if language == "javascript":
                has_javascript_tab = True
                continue
            if language in required:
                present.add(language)
        if not present and not has_javascript_tab:
            continue
        missing = required - present
        if not missing:
            continue
        missing_display = _display_languages(missing)
        present_display = _display_languages(present) or "none"
        extra = (
            " JavaScript tabs do not satisfy TypeScript coverage."
            if has_javascript_tab and "typescript" in missing
            else ""
        )
        issues.append(
            LintIssue.for_group(
                group,
                "coverage",
                f"{group.component} is missing SDK snippets for: {missing_display}. "
                f"Present: {present_display}.{extra}",
            )
        )
    return issues


def check_placeholder_sdk_tabs(groups: list[CodeGroup]) -> list[LintIssue]:
    """Fail SDK language tabs that are obvious generic HTTP placeholders."""
    issues: list[LintIssue] = []
    required = set(REQUIRED_SDK_GROUP_LANGUAGES)
    placeholder_patterns = (
        "https://api.retab.com/v1/workflows",
        'bytes.NewBufferString(`{}`)',
        "serde_json::json!({})",
        "PostAsJsonAsync(\"https://api.retab.com/v1/workflows\", new { })",
        "fetch(\"https://api.retab.com/v1/extractions\"",
        "reqwest::Client::new()\n    .post(\"https://api.retab.com/v1/extractions\")",
        "curl_init('https://api.retab.com/v1/extractions')",
    )
    for group in groups:
        if not should_require_sdk_coverage(group):
            continue
        for snippet in group.snippets:
            language = snippet_coverage_language(snippet)
            if language not in required:
                continue
            raw = _normalise_tab_token(snippet.raw_language)
            if raw in {"sh", "shell", "bash", "zsh"}:
                continue
            if (
                snippet.source.as_posix().endswith("api-reference/workflows/diagnose-graph.mdx")
                and "diagnose-graph" in snippet.code
            ):
                continue
            if any(pattern in snippet.code for pattern in placeholder_patterns):
                issues.append(
                    LintIssue.for_snippet(
                        snippet,
                        "placeholder",
                        "SDK language tab looks like a generic raw-HTTP placeholder; "
                        "replace it with a real SDK example or remove this tab.",
                    )
                )
    return issues


# Workflow / experiment / edit-template sub-resources are NESTED on the PHP and
# Ruby clients (e.g. ``$client->workflows()->blocks()``), never exposed as flat
# top-level accessors. The flat forms below DO NOT EXIST, but PHP (``php -l``)
# and Ruby (``ruby -c``) are syntax-only checks, so a call to a non-existent
# method still "passes". This structural check catches them. Each entry maps the
# bare sub-resource stem to its nested accessor chain; every nested target was
# verified against the local PHP/Ruby SDK source. The regex anchors on the full
# stem, so map order does not matter.
FLAT_ACCESSOR_NESTED_CHAINS: dict[str, tuple[str, ...]] = {
    "edit_templates": ("edits", "templates"),
    "workflow_artifacts": ("workflows", "artifacts"),
    "workflow_block_executions": ("workflows", "blocks", "executions"),
    "workflow_blocks": ("workflows", "blocks"),
    "workflow_edges": ("workflows", "edges"),
    "workflow_experiments": ("workflows", "experiments"),
    "workflow_review_versions": ("workflows", "reviews", "versions"),
    "workflow_reviews": ("workflows", "reviews"),
    "workflow_runs": ("workflows", "runs"),
    "workflow_spec": ("workflows", "spec"),
    "workflow_steps": ("workflows", "steps"),
    "workflow_eval_run_results": ("workflows", "evals", "results"),
    "workflow_eval_runs": ("workflows", "evals", "runs"),
    "workflow_evals": ("workflows", "evals"),
    "experiment_run_metrics": ("workflows", "experiments", "metrics"),
    "experiment_run_results": ("workflows", "experiments", "results"),
    "experiment_runs": ("workflows", "experiments", "runs"),
}


def _snake_to_camel(stem: str) -> str:
    head, *rest = stem.split("_")
    return head + "".join(word.capitalize() for word in rest)


# (compiled regex, suggested-nested-call) for each language, derived from the map.
_PHP_FLAT_ACCESSORS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (
        re.compile(r"\$client->" + re.escape(_snake_to_camel(stem)) + r"\(\)"),
        "$client->" + "()->".join(chain) + "()",
    )
    for stem, chain in FLAT_ACCESSOR_NESTED_CHAINS.items()
)
_RUBY_FLAT_ACCESSORS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (
        re.compile(r"\bclient\." + re.escape(stem) + r"(?!\w)"),
        "client." + ".".join(chain),
    )
    for stem, chain in FLAT_ACCESSOR_NESTED_CHAINS.items()
)


def check_flat_workflow_accessors(snippets: list[Snippet]) -> list[LintIssue]:
    """Flag PHP/Ruby snippets that call non-existent flat client accessors.

    PHP and Ruby are syntax-checked only, so a call like ``$client->workflowRuns()``
    or ``client.workflow_runs`` "passes" even though the accessor does not exist
    (the real client only exposes nested ``$client->workflows()->runs()`` /
    ``client.workflows.runs``). This is the only guard against that class of bug.
    """
    issues: list[LintIssue] = []
    for snippet in snippets:
        if snippet.language == "php":
            checks = _PHP_FLAT_ACCESSORS
        elif snippet.language == "ruby":
            checks = _RUBY_FLAT_ACCESSORS
        else:
            continue
        for pattern, suggestion in checks:
            if pattern.search(snippet.code):
                issues.append(
                    LintIssue.for_snippet(
                        snippet,
                        "accessor",
                        f"'{pattern.pattern}' is not a real client accessor "
                        "(PHP/Ruby are syntax-checked only, so this is not caught "
                        f"by compilation). Use the nested form '{suggestion}' instead.",
                    )
                )
    return issues


# ---------------------------------------------------------------------------
# Linters
# ---------------------------------------------------------------------------


@dataclass
class LintIssue:
    checker: str
    source: Path
    start_line: int
    language: str
    message: str

    @classmethod
    def for_snippet(cls, snippet: Snippet, checker: str, message: str) -> "LintIssue":
        return cls(
            checker=checker,
            source=snippet.source,
            start_line=snippet.start_line,
            language=snippet.language,
            message=message,
        )

    @classmethod
    def for_group(cls, group: CodeGroup, checker: str, message: str) -> "LintIssue":
        return cls(
            checker=checker,
            source=group.source,
            start_line=group.start_line,
            language=group.component,
            message=message,
        )


def write_snippet(snippet: Snippet) -> Path:
    rel = snippet.source.relative_to(DOCS_ROOT)
    safe = str(rel).replace(os.sep, "__").replace(".", "_")
    ext = {
        "python": ".py",
        "javascript": ".mjs",
        "typescript": ".mts",
        "go": ".go",
        "rust": ".rs",
        "java": ".java",
        "dotnet": ".cs",
        "php": ".php",
        "ruby": ".rb",
    }[snippet.language]
    out_dir = SNIPPET_DIR / snippet.language
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{safe}__{snippet.index:03d}{ext}"
    out.write_text(snippet.code, encoding="utf-8")
    return out


def _write_text_if_changed(path: Path, content: str) -> None:
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_tree_user_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    _make_tree_user_writable(path)
    shutil.rmtree(path, ignore_errors=True)


def _update_hash_with_files(
    digest,
    root: Path,
    patterns: tuple[str, ...],
) -> None:
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")


def hash_files(root: Path, patterns: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    _update_hash_with_files(digest, root, patterns)
    return digest.hexdigest()[:24]


def hash_snippets(
    snippet_files: list[tuple[Snippet, Path]],
    normalise: Callable[[Snippet, Path], str],
) -> str:
    digest = hashlib.sha256()
    for snippet, src in snippet_files:
        digest.update(src.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalise(snippet, src).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:24]


@contextmanager
def cache_lock(cache_dir: Path, label: str, timeout_seconds: float = 300):
    lock_dir = cache_dir.with_name(cache_dir.name + ".lock")
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    while True:
        try:
            lock_dir.mkdir()
            (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
            break
        except FileExistsError:
            try:
                lock_pid = int((lock_dir / "pid").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                lock_pid = 0
            if lock_pid and not _process_exists(lock_pid):
                _remove_tree(lock_dir)
                continue
            if time.monotonic() - start > timeout_seconds:
                raise RuntimeError(f"timed out waiting for {label} lock: {lock_dir}")
            time.sleep(0.1)
    try:
        yield
    finally:
        _remove_tree(lock_dir)


def cached_success(
    cache_dir: Path,
    label: str,
    work: Callable[[], list[LintIssue]],
) -> list[LintIssue]:
    success_marker = cache_dir / "success"
    if success_marker.exists():
        return []
    with cache_lock(cache_dir, label):
        if success_marker.exists():
            return []
        issues = work()
        if issues:
            success_marker.unlink(missing_ok=True)
            return issues
        _write_text_if_changed(success_marker, "ok\n")
        return []


def cached_tree(
    cache_dir: Path,
    is_ready: Callable[[Path], bool],
    populate: Callable[[Path], None],
    label: str,
) -> Path:
    if is_ready(cache_dir):
        return cache_dir
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    with cache_lock(cache_dir, label):
        if is_ready(cache_dir):
            return cache_dir
        for stale_tmp_dir in cache_dir.parent.glob(f".{cache_dir.name}.*.tmp"):
            _remove_tree(stale_tmp_dir)
        tmp_dir = cache_dir.parent / f".{cache_dir.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        _remove_tree(tmp_dir)
        try:
            populate(tmp_dir)
            _make_tree_user_writable(tmp_dir)
            try:
                tmp_dir.rename(cache_dir)
            except OSError:
                if not is_ready(cache_dir):
                    shutil.copytree(tmp_dir, cache_dir, symlinks=True)
                    _make_tree_user_writable(cache_dir)
                _remove_tree(tmp_dir)
        except Exception:
            _remove_tree(tmp_dir)
            raise
    return cache_dir


def _lint_python_syntax(snippet: Snippet, file_path: Path) -> list[LintIssue]:
    try:
        py_compile.compile(str(file_path), doraise=True)
    except py_compile.PyCompileError as exc:
        return [LintIssue.for_snippet(snippet, "py_compile", str(exc).strip())]
    return []


def _lint_python_ruff_batch(
    snippet_files: list[tuple[Snippet, Path]],
) -> list[LintIssue]:
    if not snippet_files or not RUFF.exists():
        return []
    ruff_result = subprocess.run(
        [
            str(RUFF),
            "check",
            "--no-cache",
            "--output-format=json",
            # E9 = syntax/runtime errors; F = pyflakes (undefined names,
            # unused/duplicate imports). Skip stylistic rules.
            "--select=E9,F",
            # The doc snippets are illustrative and routinely reference
            # placeholders that are defined "by context" (e.g.
            # `my_schema`, `email_attachments`, `process_invoice`). Those
            # F821 hits are pedagogical, not SDK drift. Snippets also
            # shadow common names (`client`, `result`) on purpose, so
            # squash the related redefinition/unused-var noise.
            "--ignore=F401,F811,F841,F821",
            *(str(file_path) for _, file_path in snippet_files),
        ],
        capture_output=True,
        text=True,
    )
    if not ruff_result.stdout.strip():
        return []
    try:
        findings = json.loads(ruff_result.stdout)
    except json.JSONDecodeError:
        return []
    snippets_by_path = {str(file_path.resolve()): snippet for snippet, file_path in snippet_files}
    issues: list[LintIssue] = []
    for finding in findings:
        snippet = snippets_by_path.get(str(Path(finding["filename"]).resolve()))
        if snippet is None:
            continue
        issues.append(
            LintIssue.for_snippet(
                snippet,
                "ruff",
                f"{finding['code']} {finding['message']} (line {finding['location']['row']})",
            )
        )
    return issues


def lint_python_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    ruff_files: list[tuple[Snippet, Path]] = []
    for snippet, file_path in snippet_files:
        syntax_issues = _lint_python_syntax(snippet, file_path)
        if syntax_issues:
            issues.extend(syntax_issues)
        else:
            ruff_files.append((snippet, file_path))
    issues.extend(_lint_python_ruff_batch(ruff_files))
    return issues


def lint_python(snippet: Snippet, file_path: Path) -> list[LintIssue]:
    return lint_python_batch([(snippet, file_path)])


def _python_sdk_fingerprint(python_sdk: Path) -> str:
    return hash_files(
        python_sdk,
        (
            "pyproject.toml",
            "requirements.txt",
            "retab/**/*.py",
        ),
    )


def _python_success_cache_key(
    snippet_files: list[tuple[Snippet, Path]],
    run_pyright: bool,
) -> str:
    digest = hashlib.sha256()
    digest.update(_python_sdk_fingerprint(PY_SDK_FOR_SNIPPETS).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(RUFF).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(PYRIGHT if run_pyright else "no-pyright").encode("utf-8"))
    digest.update(b"\0")
    digest.update(hash_snippets(snippet_files, lambda snippet, _: snippet.code).encode("utf-8"))
    return digest.hexdigest()[:24]


def lint_python_cached_batch(
    snippet_files: list[tuple[Snippet, Path]],
    run_pyright: bool,
) -> list[LintIssue]:
    if not snippet_files:
        return []
    cache_dir = PYTHON_SNIPPET_SUCCESS_CACHE_DIR / _python_success_cache_key(
        snippet_files,
        run_pyright,
    )

    def work() -> list[LintIssue]:
        issues = lint_python_batch(snippet_files)
        if not issues and run_pyright:
            issues.extend(lint_python_batch_pyright(snippet_files))
        return issues

    return cached_success(cache_dir, "Python snippet lint", work)


def lint_python_batch_pyright(
    snippet_files: list[tuple[Snippet, Path]],
) -> list[LintIssue]:
    """Run pyright once across every Python snippet. Pyright startup is heavy
    (Node + bundled type stubs) so batching matters."""

    if not PYRIGHT.exists():
        return []
    if not snippet_files:
        return []
    if not (PY_SDK_FOR_SNIPPETS / "retab").is_dir():
        return [
            LintIssue.for_snippet(
                snippet_files[0][0],
                "python sdk",
                f"SDK root is missing retab package: {PY_SDK_FOR_SNIPPETS}",
            )
        ]

    # Pyright doesn't follow editable installs reliably. Pin the SDK source
    # via `extraPaths` so `from retab import ...` resolves to the SDK tree
    # under test. Bazel points this at the declared generated artifact; direct
    # script usage falls back to the checked-in SDK.
    py_dir = SNIPPET_DIR / "python"
    pyright_cfg = py_dir / "pyrightconfig.json"
    pyright_cfg.write_text(
        json.dumps(
            {
                "pythonVersion": "3.12",
                # Snippets routinely import third-party libs we don't
                # install in the lint env (pydantic, openai, dotenv,
                # chonkie, fastapi, django, flask, ...). The signal we
                # care about — drift against `from retab import ...` —
                # is covered by reportAttributeAccessIssue /
                # reportCallIssue, so leave imports as warnings.
                "reportMissingImports": "warning",
                "reportMissingTypeStubs": "none",
                "reportPrivateImportUsage": "none",
                # Doc snippets are illustrative and elide context (a
                # variable defined in a prior block, a placeholder
                # `schema = {...}`). Treat undefined-name and other
                # context-dependent diagnostics as warnings so the
                # punch list highlights real SDK drift.
                "reportUndefinedVariable": "warning",
                "reportGeneralTypeIssues": "warning",
                "reportOptionalMemberAccess": "warning",
                "reportOptionalIterable": "warning",
                "reportArgumentType": "warning",
                # The two diagnostics that DO surface real SDK drift —
                # attribute access on a typed SDK return value and
                # call-shape mismatches — stay at error level.
                "reportAttributeAccessIssue": "error",
                "reportCallIssue": "error",
                "extraPaths": [str(PY_SDK_FOR_SNIPPETS)],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    files = [str(p) for _, p in snippet_files]
    by_path = {str(p.resolve()): s for s, p in snippet_files}
    result = subprocess.run(
        [
            str(PYRIGHT),
            "--outputjson",
            "--project",
            str(pyright_cfg),
            *files,
        ],
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    issues: list[LintIssue] = []
    for diag in payload.get("generalDiagnostics", []):
        if diag.get("severity") != "error":
            continue
        snippet = by_path.get(str(Path(diag["file"]).resolve()))
        if snippet is None:
            continue
        # Suppress attribute access on `EllipsisType`. Snippets routinely
        # write `obj = ...  # filled in by your code` and then dereference
        # `.foo` on the placeholder. Pyright correctly notes the type
        # mismatch, but it's a known pedagogical pattern, not SDK drift.
        message = diag.get("message", "")
        if 'for class "EllipsisType"' in message:
            continue
        line = diag.get("range", {}).get("start", {}).get("line", 0) + 1
        rule = diag.get("rule", "type")
        issues.append(
            LintIssue.for_snippet(
                snippet,
                "pyright",
                f"{rule}: {message} (line {line})",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# TypeScript / JavaScript
# ---------------------------------------------------------------------------


def _tsconfig_template(node_sdk_root: Path) -> dict[str, object]:
    if (node_sdk_root / "dist" / "index.d.ts").exists():
        node_entrypoint = "./node-sdk/dist/index.d.ts"
        node_wildcard = "./node-sdk/dist/*"
    else:
        node_entrypoint = "./node-sdk/src/index.ts"
        node_wildcard = "./node-sdk/src/*"
    return {
    "compilerOptions": {
        "target": "ES2022",
        "module": "ESNext",
        "moduleResolution": "Bundler",
        "allowJs": True,
        "checkJs": True,
        "noEmit": True,
        "strict": False,
        # The local SDK source uses `.js` import specifiers (ESM convention);
        # bundler resolution handles them transparently.
        "skipLibCheck": True,
        # The doc snippets are deliberately tiny and frequently use top-level
        # await, optional chaining, etc.
        "esModuleInterop": True,
        "isolatedModules": False,
        "resolveJsonModule": True,
        # Lib selection: minimum needed for fetch / console.
        "lib": ["ES2022", "DOM"],
        # Pull in Node ambient types so snippets that touch
        # `process.env`, `Buffer`, etc. don't error on the env, only on
        # real SDK drift.
        "types": ["node"],
        "typeRoots": [
            "./node-deps/node_modules/@types",
        ],
        "baseUrl": ".",
        "paths": {
            "@retab/node": [node_entrypoint],
            "@retab/node/*": [node_wildcard],
            # Allow snippets that import `zod` directly to resolve from
            # the SDK's installed copy (the SDK depends on zod itself).
            "zod": ["./node-deps/node_modules/zod/index.d.ts"],
        },
    },
    "include": ["snippets/**/*"],
    }


def _node_sdk_fingerprint(node_sdk: Path) -> str:
    return hash_files(
        node_sdk,
        (
            "package.json",
            "tsconfig.json",
            "src/**/*.ts",
            "dist/**/*.d.ts",
        ),
    )


def _ts_workspace_cache_key(snippet_files: list[tuple[Snippet, Path]]) -> str:
    digest = hashlib.sha256()
    digest.update(_node_sdk_fingerprint(NODE_SDK_FOR_SNIPPETS).encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        hash_files(
            NODE_SDK,
            (
                "package-lock.json",
                "node_modules/typescript/package.json",
                "node_modules/@types/node/package.json",
                "node_modules/zod/package.json",
            ),
        ).encode("utf-8")
    )
    digest.update(hash_snippets(snippet_files, lambda snippet, _: snippet.code).encode("utf-8"))
    return digest.hexdigest()[:24]


def _ts_workspace_for_snippets(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    return TS_SNIPPET_WORKSPACE_CACHE_DIR / _ts_workspace_cache_key(snippet_files)


def _node_sdk_for_ts_snippets() -> Path:
    fingerprint = _node_sdk_fingerprint(NODE_SDK_FOR_SNIPPETS)
    cache_dir = TS_SNIPPET_WORKSPACE_CACHE_DIR / "node-sdk-cache" / fingerprint

    def populate(tmp_dir: Path) -> None:
        shutil.copytree(
            NODE_SDK_FOR_SNIPPETS,
            tmp_dir,
            symlinks=True,
            ignore=shutil.ignore_patterns("node_modules"),
        )

    return cached_tree(
        cache_dir,
        lambda path: (path / "package.json").exists(),
        populate,
        "TypeScript SDK cache",
    )


def _prepare_ts_workspace(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    """Create an isolated workspace that mirrors `@retab/node` so the SDK
    surface is type-checked end-to-end without polluting the doc tree."""

    ws = _ts_workspace_for_snippets(snippet_files)
    ws.mkdir(parents=True, exist_ok=True)
    node_sdk = _node_sdk_for_ts_snippets()
    # Symlink the SDK under test. Bazel may point this at a declared generated
    # tree, while direct script usage falls back to the checked-in SDK.
    sdk_link = ws / "node-sdk"
    if sdk_link.exists() or sdk_link.is_symlink():
        sdk_link.unlink()
    sdk_link.symlink_to(node_sdk)
    deps_link = ws / "node-deps"
    if deps_link.exists() or deps_link.is_symlink():
        deps_link.unlink()
    deps_link.symlink_to(NODE_SDK)
    snippets_dir = ws / "snippets"
    snippets_dir.mkdir(exist_ok=True)
    for snippet, src in snippet_files:
        dst = snippets_dir / src.name
        _write_text_if_changed(dst, snippet.code)
    tsconfig = ws / "tsconfig.json"
    _write_text_if_changed(
        tsconfig,
        json.dumps(_tsconfig_template(node_sdk), indent=2),
    )
    return ws


def lint_typescript_batch(
    snippet_files: list[tuple[Snippet, Path]],
) -> list[LintIssue]:
    if not snippet_files:
        return []
    if not TSC.exists() or not NODE:
        return []
    ws = _ts_workspace_for_snippets(snippet_files)
    with cache_lock(ws, "TypeScript snippet workspace"):
        ws = _prepare_ts_workspace(snippet_files)
        result = subprocess.run(
            [
                NODE,
                str(TSC),
                "-p",
                str(ws / "tsconfig.json"),
                "--pretty",
                "false",
                "--incremental",
                "--tsBuildInfoFile",
                str(ws / "tsconfig.tsbuildinfo"),
            ],
            capture_output=True,
            text=True,
            cwd=str(ws),
        )
    # `tsc` emits diagnostics to stdout when --pretty=false and writes nothing
    # on success.
    output = (result.stdout or "") + (result.stderr or "")
    if not output.strip():
        return []
    # tsc emits lines like:
    #   snippets/foo.mts(12,4): error TS2304: Cannot find name 'bar'.
    issues: list[LintIssue] = []
    by_name = {src.name: s for s, src in snippet_files}
    diag_re = re.compile(
        r"^(?P<file>[^:()]+?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
        r"(?P<sev>error|warning)\s+(?P<code>TS\d+):\s+(?P<msg>.+)$"
    )
    # Mirror the pyright noise filter: TS codes that fire on the
    # context-dependent / placeholder shape of doc snippets aren't drift
    # — they're a property of writing illustrative code. Suppress them
    # by default; real SDK-shape mismatches (TS2339 unknown property,
    # TS2353 unknown property in object literal, TS2554 wrong arg
    # count, TS2561 typo'd property, TS2322 type mismatch on a value
    # constrained by the SDK, ...) still surface.
    SUPPRESSED_TS_CODES = {
        "TS2304",  # Cannot find name 'X' — usually a placeholder
        "TS2307",  # Cannot find module 'X' — third-party libs not in lint env
    }
    for raw in output.splitlines():
        m = diag_re.match(raw.strip())
        if not m:
            continue
        if m.group("sev") != "error":
            continue
        if m.group("code") in SUPPRESSED_TS_CODES:
            continue
        name = Path(m.group("file")).name
        snippet = by_name.get(name)
        if snippet is None:
            continue
        issues.append(
            LintIssue.for_snippet(
                snippet,
                "tsc",
                f"{m.group('code')} {m.group('msg')} (line {m.group('line')})",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def _split_leading_go_imports(code: str) -> tuple[str, str]:
    lines = code.splitlines()
    imports: list[str] = []
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].lstrip().startswith("import "):
        imports.append(lines[idx])
        if lines[idx].strip() == "import (":
            idx += 1
            while idx < len(lines):
                imports.append(lines[idx])
                if lines[idx].strip() == ")":
                    idx += 1
                    break
                idx += 1
        else:
            idx += 1
    return "\n".join(imports), "\n".join(lines[idx:])


def normalise_go_snippet(code: str) -> str:
    if re.search(r"(?m)^\s*package\s+\w+", code) is not None:
        return code
    imports, body = _split_leading_go_imports(code)
    indented = "\n".join(f"\t{line}" if line else "" for line in body.splitlines())
    return f"package main\n\n{imports}\n\nfunc main() {{\n{indented}\n}}\n"


def _go_sdk_fingerprint(go_sdk: Path) -> str:
    return hash_files(go_sdk, ("go.mod", "go.sum", "*.go"))


def _go_workspace_cache_key(snippet_files: list[tuple[Snippet, Path]]) -> str:
    digest = hashlib.sha256()
    digest.update(_go_sdk_fingerprint(GO_SDK_FOR_SNIPPETS).encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        hash_snippets(
            snippet_files,
            lambda snippet, _: normalise_go_snippet(snippet.code),
        ).encode("utf-8")
    )
    return digest.hexdigest()[:24]


def _go_workspace_for_snippets(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    return GO_SNIPPET_WORKSPACE_CACHE_DIR / _go_workspace_cache_key(snippet_files)


def _prepare_go_workspace(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    ws = _go_workspace_for_snippets(snippet_files)
    ws.mkdir(parents=True, exist_ok=True)
    _write_text_if_changed(
        ws / "go.mod",
        "\n".join(
            [
                "module retab_docs_snippets",
                "",
                "go 1.23",
                "",
                "require github.com/retab-dev/retab/clients/go v0.0.0",
                f"replace github.com/retab-dev/retab/clients/go => {GO_SDK_FOR_SNIPPETS}",
                "",
            ]
        ),
    )
    sdk_go_sum = GO_SDK_FOR_SNIPPETS / "go.sum"
    if sdk_go_sum.exists():
        shutil.copyfile(sdk_go_sum, ws / "go.sum")
    for index, (snippet, src) in enumerate(snippet_files):
        snippet_dir = ws / "snippets" / f"{src.stem}_{index:03d}"
        snippet_dir.mkdir(parents=True, exist_ok=True)
        _write_text_if_changed(
            snippet_dir / "main.go",
            normalise_go_snippet(snippet.code),
        )
    return ws


def lint_go_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files or GO is None:
        return []
    ws = _go_workspace_for_snippets(snippet_files)

    def work() -> list[LintIssue]:
        ws = _prepare_go_workspace(snippet_files)
        result = subprocess.run(
            [GO, "test", "-mod=mod", "./..."],
            capture_output=True,
            text=True,
            cwd=str(ws),
            env={**os.environ, "GOWORK": "off"},
        )
        if result.returncode == 0:
            return []
        output = (result.stdout or "") + (result.stderr or "")
        by_name = {
            f"{src.stem}_{index:03d}": snippet
            for index, (snippet, src) in enumerate(snippet_files)
        }
        issues: list[LintIssue] = []
        diag_re = re.compile(
            r"^(?:# .+|(?P<path>snippets/(?P<name>[^/]+)/main\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+))$"
        )
        for raw in output.splitlines():
            m = diag_re.match(raw.strip())
            if m is None or m.group("name") is None:
                continue
            snippet = by_name.get(m.group("name"))
            if snippet is None:
                continue
            issues.append(
                LintIssue.for_snippet(
                    snippet,
                    "go",
                    f"{m.group('msg')} (line {m.group('line')})",
                )
            )
        if not issues and output.strip():
            snippet = snippet_files[0][0]
            issues.append(LintIssue.for_snippet(snippet, "go", output.strip()))
        return issues

    return cached_success(ws, "Go snippet workspace", work)


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


def _split_leading_rust_uses(code: str) -> tuple[str, str]:
    lines = code.splitlines()
    uses: list[str] = []
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    while idx < len(lines) and lines[idx].lstrip().startswith("use "):
        while idx < len(lines):
            uses.append(lines[idx])
            done = lines[idx].rstrip().endswith(";")
            idx += 1
            if done:
                break
        while idx < len(lines) and not lines[idx].strip():
            uses.append(lines[idx])
            idx += 1
    return "\n".join(uses), "\n".join(lines[idx:])


def normalise_rust_snippet(code: str) -> str:
    if re.search(r"\bfn\s+main\s*\(", code) is not None:
        return code
    uses, body = _split_leading_rust_uses(code)
    indented = "\n".join(f"    {line}" if line else "" for line in body.splitlines())
    return (
        f"{uses}\n\n"
        "async fn snippet() -> Result<(), Box<dyn std::error::Error>> {\n"
        f"{indented}\n"
        "    Ok(())\n"
        "}\n\n"
        "fn main() {}\n"
    )


def _rust_workspace_for_snippets() -> Path:
    return RUST_SNIPPET_WORKSPACE_CACHE_DIR / "workspace"


def _rust_sdk_fingerprint(rust_sdk: Path) -> str:
    return hash_files(rust_sdk, ("Cargo.toml", "Cargo.lock", "src/**/*.rs"))


def _rust_sdk_for_snippets() -> Path:
    fingerprint = _rust_sdk_fingerprint(RUST_SDK_FOR_SNIPPETS)
    cache_dir = RUST_SNIPPET_SDK_CACHE_DIR / fingerprint

    def populate(tmp_dir: Path) -> None:
        shutil.copytree(
            RUST_SDK_FOR_SNIPPETS,
            tmp_dir,
            symlinks=True,
            ignore=shutil.ignore_patterns("target"),
        )

    return cached_tree(
        cache_dir,
        lambda path: (path / "Cargo.toml").exists(),
        populate,
        "Rust SDK cache",
    )


def _prepare_rust_workspace(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    ws = _rust_workspace_for_snippets()
    rust_sdk = _rust_sdk_for_snippets()
    snippets_dir = ws / "src" / "snippets"
    snippets_dir.mkdir(parents=True, exist_ok=True)
    _write_text_if_changed(
        ws / "Cargo.toml",
        "\n".join(
            [
                "[package]",
                'name = "retab_docs_snippets"',
                'version = "0.0.0"',
                'edition = "2021"',
                "",
                "[dependencies]",
                f'retab = {{ path = "{rust_sdk}" }}',
                'tokio = { version = "1", features = ["rt-multi-thread", "macros"] }',
                'reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls"] }',
                'serde_json = "1"',
                'base64 = "0.22"',
                "",
            ]
        ),
    )
    module_lines = [
        "#![allow(dead_code, unused_assignments, unused_variables)]",
        "",
        "fn main() {}",
        "",
    ]
    expected_snippet_names: set[str] = set()
    for index, (snippet, src) in enumerate(snippet_files):
        module_name = f"snippet_{index:04d}"
        module_lines.append(f'#[path = "snippets/{src.name}"]')
        module_lines.append(f"mod {module_name};")
        expected_snippet_names.add(src.name)
        _write_text_if_changed(
            snippets_dir / src.name,
            normalise_rust_snippet(snippet.code),
        )
    for stale_path in snippets_dir.glob("*.rs"):
        if stale_path.name not in expected_snippet_names:
            stale_path.unlink()
    _write_text_if_changed(ws / "src" / "main.rs", "\n".join(module_lines) + "\n")
    return ws


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lint_rust_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files or CARGO is None:
        return []
    ws = _rust_workspace_for_snippets()
    with cache_lock(ws, "Rust snippet workspace", timeout_seconds=600):
        ws = _prepare_rust_workspace(snippet_files)
        command = [CARGO, "check", "--message-format", "short"]
        if RUST_SNIPPET_TARGET_DIR is not None:
            RUST_SNIPPET_TARGET_DIR.mkdir(parents=True, exist_ok=True)
            command.extend(["--target-dir", str(RUST_SNIPPET_TARGET_DIR)])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=str(ws),
        )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return []
    by_name = {src.name: snippet for snippet, src in snippet_files}
    issues: list[LintIssue] = []
    diag_re = re.compile(
        r"^src/snippets/(?P<name>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*"
        r"(?P<level>error|warning)(?:\[[^\]]+\])?:\s*(?P<msg>.+)$"
    )
    for raw in output.splitlines():
        m = diag_re.match(raw.strip())
        if m is None or m.group("level") != "error":
            continue
        snippet = by_name.get(m.group("name"))
        if snippet is None:
            continue
        issues.append(
            LintIssue.for_snippet(
                snippet,
                "cargo",
                f"{m.group('msg')} (line {m.group('line')})",
            )
        )
    if not issues and output.strip():
        snippet = snippet_files[0][0]
        issues.append(LintIssue.for_snippet(snippet, "cargo", output.strip()))
    return issues


# ---------------------------------------------------------------------------
# PHP / Ruby
# ---------------------------------------------------------------------------


def normalise_php_snippet(code: str) -> str:
    if code.lstrip().startswith("<?php"):
        return code
    return "<?php\n" + code


def _sdk_root_issue(
    snippet: Snippet,
    tool: str,
    sdk_root: Path,
    required_files: tuple[str, ...],
) -> LintIssue | None:
    if not sdk_root.exists():
        return LintIssue.for_snippet(
            snippet,
            tool,
            f"SDK root does not exist: {sdk_root}",
        )
    for required_file in required_files:
        if not (sdk_root / required_file).exists():
            return LintIssue.for_snippet(
                snippet,
                tool,
                f"SDK root is missing {required_file}: {sdk_root}",
            )
    return None


def _run_parallel_snippet_lints(
    snippet_files: list[tuple[Snippet, Path]],
    lint_one: Callable[[Snippet, Path], list[LintIssue]],
) -> list[LintIssue]:
    if not snippet_files:
        return []
    max_workers = min(8, max(1, os.cpu_count() or 1), len(snippet_files))
    issues: list[LintIssue] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(lint_one, snippet, file_path)
            for snippet, file_path in snippet_files
        ]
        for future in concurrent.futures.as_completed(futures):
            issues.extend(future.result())
    return issues


def lint_php(snippet: Snippet, file_path: Path) -> list[LintIssue]:
    sdk_issue = _sdk_root_issue(
        snippet,
        "php sdk",
        PHP_SDK_FOR_SNIPPETS,
        ("composer.json", "lib/Client.php"),
    )
    if sdk_issue is not None:
        return [sdk_issue]
    if PHP is None:
        return []
    return _lint_php_syntax(snippet, file_path)


def _lint_php_syntax(snippet: Snippet, file_path: Path) -> list[LintIssue]:
    _write_text_if_changed(file_path, normalise_php_snippet(snippet.code))
    result = subprocess.run(
        [PHP, "-l", str(file_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    output = ((result.stderr or "") + (result.stdout or "")).strip()
    return [LintIssue.for_snippet(snippet, "php -l", output)]


def lint_php_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files:
        return []
    sdk_issue = _sdk_root_issue(
        snippet_files[0][0],
        "php sdk",
        PHP_SDK_FOR_SNIPPETS,
        ("composer.json", "lib/Client.php"),
    )
    if sdk_issue is not None:
        return [sdk_issue]
    if PHP is None:
        return []
    return _run_parallel_snippet_lints(snippet_files, _lint_php_syntax)


def _php_sdk_fingerprint(php_sdk: Path) -> str:
    return hash_files(
        php_sdk,
        (
            "composer.json",
            "composer.lock",
            "lib/**/*.php",
        ),
    )


def _php_success_cache_key(snippet_files: list[tuple[Snippet, Path]]) -> str:
    digest = hashlib.sha256()
    digest.update(_php_sdk_fingerprint(PHP_SDK_FOR_SNIPPETS).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(PHP or "no-php").encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        hash_snippets(
            snippet_files,
            lambda snippet, _: normalise_php_snippet(snippet.code),
        ).encode("utf-8")
    )
    return digest.hexdigest()[:24]


def lint_php_cached_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files:
        return []
    cache_dir = PHP_SNIPPET_SUCCESS_CACHE_DIR / _php_success_cache_key(snippet_files)
    return cached_success(cache_dir, "PHP snippet lint", lambda: lint_php_batch(snippet_files))


def lint_ruby(snippet: Snippet, file_path: Path) -> list[LintIssue]:
    sdk_issue = _sdk_root_issue(
        snippet,
        "ruby sdk",
        RUBY_SDK_FOR_SNIPPETS,
        ("retab.gemspec", "lib/retab.rb"),
    )
    if sdk_issue is not None:
        return [sdk_issue]
    if RUBY is None:
        return []
    file_path.write_text(snippet.code, encoding="utf-8")
    result = subprocess.run(
        [RUBY, "-c", str(file_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    output = ((result.stderr or "") + (result.stdout or "")).strip()
    return [LintIssue.for_snippet(snippet, "ruby -c", output)]


def lint_ruby_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files:
        return []
    sdk_issue = _sdk_root_issue(
        snippet_files[0][0],
        "ruby sdk",
        RUBY_SDK_FOR_SNIPPETS,
        ("retab.gemspec", "lib/retab.rb"),
    )
    if sdk_issue is not None:
        return [sdk_issue]
    if RUBY is None:
        return []
    for snippet, file_path in snippet_files:
        file_path.write_text(snippet.code, encoding="utf-8")
    script = """
require "json"

ok = true
ARGV.each do |path|
  begin
    RubyVM::InstructionSequence.compile_file(path)
  rescue Exception => e
    ok = false
    puts JSON.generate({ file: path, error: "#{e.class}: #{e.message}" })
  end
end
exit(ok ? 0 : 1)
"""
    result = subprocess.run(
        [RUBY, "-e", script, "--", *(str(path) for _, path in snippet_files)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    by_path = {str(path.resolve()): snippet for snippet, path in snippet_files}
    issues: list[LintIssue] = []
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        snippet = by_path.get(str(Path(payload.get("file", "")).resolve()))
        if snippet is None:
            continue
        issues.append(LintIssue.for_snippet(snippet, "ruby -c", str(payload.get("error", ""))))
    output = ((result.stderr or "") + (result.stdout or "")).strip()
    if not issues and output:
        issues.append(LintIssue.for_snippet(snippet_files[0][0], "ruby -c", output))
    return issues


def _copy_bazel_sdk_tree_for_build(language: str, source: Path) -> Path:
    dst = SNIPPET_DIR / f"_{language}_sdk"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(
        source,
        dst,
        symlinks=True,
        ignore=shutil.ignore_patterns(".gradle", "bin", "build", "obj", "target"),
    )
    _make_tree_user_writable(dst)
    return dst


# ---------------------------------------------------------------------------
# .NET / C#
# ---------------------------------------------------------------------------


def _split_leading_dotnet_usings(code: str) -> tuple[str, str]:
    lines = code.splitlines()
    usings: list[str] = []
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    while idx < len(lines):
        stripped = lines[idx].lstrip()
        if not stripped.startswith("using ") or re.match(r"using\s+(?:var|\()", stripped):
            break
        usings.append(lines[idx])
        idx += 1
    return "\n".join(usings), "\n".join(lines[idx:])


def normalise_dotnet_snippet(code: str, class_name: str = "Snippet") -> str:
    usings, body = _split_leading_dotnet_usings(code)
    indented = "\n".join(f"    {line}" if line else "" for line in body.splitlines())
    return (
        f"{usings}\n\n"
        f"internal static class {class_name}\n"
        "{\n"
        "  public static async Task RunAsync()\n"
        "  {\n"
        f"{indented}\n"
        "  }\n"
        "}\n"
    )


def _prepare_dotnet_workspace(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    ws = SNIPPET_DIR / "_dotnetworkspace"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    (ws / "GlobalUsings.cs").write_text(
        "\n".join(
            [
                "global using System;",
                "global using System.Collections.Generic;",
                "global using System.IO;",
                "global using System.Linq;",
                "global using System.Threading.Tasks;",
                "",
            ]
        ),
        encoding="utf-8",
    )
    for index, (snippet, src) in enumerate(snippet_files):
        (ws / src.name).write_text(
            normalise_dotnet_snippet(snippet.code, f"Snippet_{index:04d}"),
            encoding="utf-8",
        )
    return ws


def _dotnet_sdk_root() -> Path | None:
    if DOTNET is None:
        return None
    result = subprocess.run(
        [DOTNET, "--list-sdks"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    candidates: list[Path] = []
    for line in result.stdout.splitlines():
        match = re.match(r"(?P<version>\S+)\s+\[(?P<root>[^\]]+)\]", line.strip())
        if match is None:
            continue
        candidates.append(Path(match.group("root")) / match.group("version"))
    return candidates[-1] if candidates else None


def _dotnet_sdk_for_build() -> Path:
    if "RETAB_DOTNET_SDK_ROOT" not in os.environ:
        return DOTNET_SDK
    return _copy_bazel_sdk_tree_for_build("dotnet", DOTNET_SDK_FOR_SNIPPETS)


def _dotnet_sdk_fingerprint(dotnet_sdk: Path) -> str:
    return hash_files(dotnet_sdk, ("Retab.csproj", "src/**/*.cs"))


def _build_dotnet_sdk_for_snippets(
    dotnet_sdk: Path,
    snippet: Snippet,
) -> tuple[Path | None, LintIssue | None]:
    fingerprint = _dotnet_sdk_fingerprint(dotnet_sdk)
    cache_dir = DOTNET_SNIPPET_SDK_CACHE_DIR / fingerprint
    assembly = cache_dir / "bin" / "Retab.dll"
    if assembly.exists():
        return assembly, None

    obj_dir = cache_dir / "obj"
    bin_dir = cache_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    obj_dir.mkdir(parents=True, exist_ok=True)
    sdk_build_cmd = [
        DOTNET,
        "build",
        str(dotnet_sdk / "Retab.csproj"),
        "--nologo",
        "--verbosity",
        "quiet",
        "/nodeReuse:false",
        f"-p:BaseIntermediateOutputPath={obj_dir}/",
        f"-p:OutputPath={bin_dir}/",
        "-p:GenerateAssemblyInfo=false",
        "-p:GenerateTargetFrameworkAttribute=false",
    ]
    for _ in range(3):
        sdk_build = subprocess.run(
            sdk_build_cmd,
            capture_output=True,
            text=True,
        )
        if sdk_build.returncode == 0:
            break
        if ((sdk_build.stdout or "") + (sdk_build.stderr or "")).strip():
            break
    if sdk_build.returncode != 0:
        output = ((sdk_build.stdout or "") + (sdk_build.stderr or "")).strip()
        return None, LintIssue.for_snippet(snippet, "dotnet", output)
    if not assembly.exists():
        return None, LintIssue.for_snippet(
            snippet,
            "dotnet",
            f"SDK build did not produce expected assembly: {assembly}",
        )
    return assembly, None


def _dotnet_references(dotnet_sdk: Path, sdk_assembly: Path) -> list[Path]:
    sdk_root = _dotnet_sdk_root()
    if sdk_root is None:
        return []
    dotnet_root = sdk_root.parent.parent
    ref_pack_root = dotnet_root / "packs" / "Microsoft.NETCore.App.Ref"
    ref_dirs = sorted(ref_pack_root.glob("*/ref/net*"))
    refs = list(ref_dirs[-1].glob("*.dll")) if ref_dirs else []
    refs.extend(
        [
            sdk_assembly,
            NUGET_PACKAGES
            / "newtonsoft.json"
            / "13.0.3"
            / "lib"
            / "netstandard2.0"
            / "Newtonsoft.Json.dll",
            NUGET_PACKAGES / "oneof" / "3.0.271" / "lib" / "netstandard2.0" / "OneOf.dll",
        ]
    )
    return [ref for ref in refs if ref.exists()]


def _fingerprint_paths_by_stat(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat_result = path.stat()
        except OSError:
            continue
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat_result.st_size).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat_result.st_mtime_ns).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _dotnet_compile_cache_key(
    snippet_files: list[tuple[Snippet, Path]],
    dotnet_sdk: Path,
    csc: Path,
    refs: list[Path],
) -> str:
    class_name_by_source = {
        src.name: f"Snippet_{index:04d}"
        for index, (_, src) in enumerate(snippet_files)
    }
    digest = hashlib.sha256()
    digest.update(_dotnet_sdk_fingerprint(dotnet_sdk).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(csc).encode("utf-8"))
    digest.update(b"\0")
    digest.update(_fingerprint_paths_by_stat([csc, *refs]).encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        hash_snippets(
            snippet_files,
            lambda snippet, src: normalise_dotnet_snippet(
                snippet.code,
                class_name_by_source[src.name],
            ),
        ).encode("utf-8")
    )
    return digest.hexdigest()[:24]


def _dotnet_csc() -> Path | None:
    sdk_root = _dotnet_sdk_root()
    if sdk_root is None:
        return None
    csc = sdk_root / "Roslyn" / "bincore" / "csc.dll"
    return csc if csc.exists() else None


def lint_dotnet_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files or DOTNET is None:
        return []
    dotnet_sdk_issue = _sdk_root_issue(
        snippet_files[0][0],
        "dotnet sdk",
        DOTNET_SDK_FOR_SNIPPETS,
        ("Retab.csproj", "src/Retab.Generated.cs"),
    )
    if dotnet_sdk_issue is not None:
        return [dotnet_sdk_issue]
    dotnet_sdk = _dotnet_sdk_for_build()
    sdk_assembly, sdk_build_issue = _build_dotnet_sdk_for_snippets(dotnet_sdk, snippet_files[0][0])
    if sdk_build_issue is not None:
        return [sdk_build_issue]
    if sdk_assembly is None:
        return []

    csc = _dotnet_csc()
    refs = _dotnet_references(dotnet_sdk, sdk_assembly)
    if csc is None or not refs:
        return []
    compile_cache_dir = DOTNET_SNIPPET_COMPILE_CACHE_DIR / _dotnet_compile_cache_key(
        snippet_files,
        dotnet_sdk,
        csc,
        refs,
    )
    by_name = {src.name: snippet for snippet, src in snippet_files}

    def work() -> list[LintIssue]:
        ws = _prepare_dotnet_workspace(snippet_files)
        response_file = ws / "csc.rsp"
        snippet_sources = [ws / src.name for _, src in snippet_files]
        response_file.write_text(
            "\n".join(
                [
                    "-nologo",
                    "-target:library",
                    "-langversion:12",
                    "-nullable:enable",
                    f"-out:{ws / 'Snippets.dll'}",
                    *(f"-r:{ref}" for ref in refs),
                    str(ws / "GlobalUsings.cs"),
                    *(str(path) for path in snippet_sources),
                ]
            ),
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [DOTNET, str(csc), f"@{response_file}"],
                capture_output=True,
                text=True,
                cwd=str(ws),
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return [
                LintIssue.for_snippet(
                    snippet_files[0][0],
                    "dotnet",
                    "dotnet csc timed out after 30s",
                )
            ]
        if result.returncode == 0:
            return []
        issues: list[LintIssue] = []
        output = (result.stdout or "") + (result.stderr or "")
        matched_diagnostic = False
        diag_re = re.compile(
            r"(?P<file>[^:()]+\.cs)\((?P<line>\d+),(?P<col>\d+)\):\s+"
            r"(?P<level>error|warning)\s+(?P<code>CS\d+):\s+(?P<msg>.+?)(?:\s+\[|$)"
        )
        for raw in output.splitlines():
            m = diag_re.search(raw.strip())
            if m is None:
                continue
            matched_diagnostic = True
            if m.group("level") != "error":
                continue
            snippet = by_name.get(Path(m.group("file")).name)
            if snippet is None:
                continue
            issue = LintIssue.for_snippet(
                snippet,
                "dotnet",
                f"{m.group('code')} {m.group('msg')} (line {m.group('line')})",
            )
            if issue not in issues:
                issues.append(issue)
        if not matched_diagnostic and output.strip():
            issues.append(LintIssue.for_snippet(snippet_files[0][0], "dotnet", output.strip()))
        return issues

    return cached_success(compile_cache_dir, ".NET snippet compile", work)


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


def _split_leading_java_imports(code: str) -> tuple[str, str]:
    lines = code.splitlines()
    imports: list[str] = []
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    while idx < len(lines) and lines[idx].lstrip().startswith("import "):
        imports.append(lines[idx])
        idx += 1
    return "\n".join(imports), "\n".join(lines[idx:])


def normalise_java_snippet(
    code: str,
    wrapper_class_name: str = "Snippet",
    rename_explicit_class: bool = False,
) -> str:
    if re.search(r"\bclass\s+\w+", code) is not None:
        normalised = re.sub(r"\bpublic\s+(?=(?:final\s+)?class\s+\w+)", "", code)
        if not rename_explicit_class:
            return normalised
        class_match = re.search(r"\bclass\s+(?P<name>\w+)", normalised)
        if class_match is None:
            return normalised
        return re.sub(
            rf"\b{re.escape(class_match.group('name'))}\b",
            wrapper_class_name,
            normalised,
        )
    imports, body = _split_leading_java_imports(code)
    indented = "\n".join(f"    {line}" if line else "" for line in body.splitlines())
    return (
        f"{imports}\n\n"
        f"final class {wrapper_class_name} {{\n"
        "  public static void main(String[] args) throws Exception {\n"
        f"{indented}\n"
        "  }\n"
        "}\n"
    )


def _java_sdk_for_build() -> Path:
    return JAVA_SDK_FOR_SNIPPETS.resolve()


def _java_sdk_fingerprint(java_sdk: Path) -> str:
    return hash_files(java_sdk, ("pom.xml", "src/main/java/**/*.java"))


def _cached_java_classpath(cache_dir: Path) -> str | None:
    sdk_classes_dir = cache_dir / "classes"
    cp_file = cache_dir / "classpath.txt"
    if not (sdk_classes_dir / "com" / "retab").is_dir():
        return None
    parts = [str(sdk_classes_dir)]
    if cp_file.exists() and cp_file.read_text(encoding="utf-8").strip():
        parts.append(cp_file.read_text(encoding="utf-8").strip())
    return os.pathsep.join(parts)


def _maven_local_repositories() -> list[Path]:
    repos: list[Path] = []
    for home_value in (
        os.environ.get("HOME"),
        os.environ.get("RETAB_HOST_HOME"),
        str(CACHE_REPO_ROOT / ".cache" / "bazel-local-home"),
    ):
        if home_value:
            repos.append(Path(home_value) / ".m2" / "repository")
    return list(dict.fromkeys(repo.resolve() for repo in repos))


def _resolve_maven_property(value: str, properties: dict[str, str]) -> str:
    match = re.fullmatch(r"\$\{(?P<name>[^}]+)\}", value.strip())
    if match is None:
        return value.strip()
    return properties.get(match.group("name"), value.strip())


def _java_pom_dependencies(java_sdk: Path) -> list[tuple[str, str, str]]:
    pom = java_sdk / "pom.xml"
    if not pom.exists():
        return []
    root = ET.fromstring(pom.read_text(encoding="utf-8"))
    namespace = {"m": root.tag.partition("}")[0].removeprefix("{")}
    properties: dict[str, str] = {}
    properties_node = root.find("m:properties", namespace)
    if properties_node is not None:
        for child in list(properties_node):
            name = child.tag.rpartition("}")[2]
            properties[name] = (child.text or "").strip()
    dependencies: list[tuple[str, str, str]] = []
    for dependency in root.findall("m:dependencies/m:dependency", namespace):
        scope = dependency.findtext("m:scope", default="", namespaces=namespace).strip()
        if scope == "test":
            continue
        group_id = dependency.findtext("m:groupId", default="", namespaces=namespace).strip()
        artifact_id = dependency.findtext("m:artifactId", default="", namespaces=namespace).strip()
        version = dependency.findtext("m:version", default="", namespaces=namespace).strip()
        if not group_id or not artifact_id or not version:
            continue
        dependencies.append(
            (group_id, artifact_id, _resolve_maven_property(version, properties))
        )
    return dependencies


def _maven_local_jar(group_id: str, artifact_id: str, version: str) -> Path | None:
    rel = Path(*group_id.split(".")) / artifact_id / version / f"{artifact_id}-{version}.jar"
    for repo in _maven_local_repositories():
        jar = repo / rel
        if jar.exists():
            return jar
    return None


def _java_dependency_jars(java_sdk: Path) -> list[Path] | None:
    dependencies = _java_pom_dependencies(java_sdk)
    expanded = list(dependencies)
    for group_id, artifact_id, version in dependencies:
        if group_id == "com.fasterxml.jackson.core" and artifact_id == "jackson-databind":
            expanded.extend(
                [
                    ("com.fasterxml.jackson.core", "jackson-core", version),
                    ("com.fasterxml.jackson.core", "jackson-annotations", version),
                ]
            )
    jars: list[Path] = []
    seen: set[Path] = set()
    for group_id, artifact_id, version in expanded:
        jar = _maven_local_jar(group_id, artifact_id, version)
        if jar is None:
            return None
        if jar not in seen:
            seen.add(jar)
            jars.append(jar)
    return jars


def _compile_java_sdk_direct(java_sdk: Path, cache_dir: Path) -> str | None:
    if JAVAC is None:
        return None
    dependency_jars = _java_dependency_jars(java_sdk)
    if dependency_jars is None:
        return None
    sources = sorted((java_sdk / "src" / "main" / "java").rglob("*.java"))
    if not sources:
        return None
    sdk_classes_dir = cache_dir / "classes"
    cp_file = cache_dir / "classpath.txt"
    shutil.rmtree(sdk_classes_dir, ignore_errors=True)
    sdk_classes_dir.mkdir(parents=True, exist_ok=True)
    dependency_classpath = os.pathsep.join(str(jar) for jar in dependency_jars)
    command = [
        JAVAC,
        "-Xlint:none",
        "--release",
        "11",
        "-d",
        str(sdk_classes_dir),
    ]
    if dependency_classpath:
        command.extend(["-cp", dependency_classpath])
    command.extend(str(source) for source in sources)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=str(java_sdk),
    )
    if result.returncode != 0:
        return None
    cp_file.write_text(dependency_classpath, encoding="utf-8")
    return _cached_java_classpath(cache_dir)


def _compile_java_sdk_with_maven(java_sdk: Path, cache_dir: Path) -> str | None:
    if MAVEN is None:
        return None
    sdk_classes_dir = cache_dir / "classes"
    sdk_build_dir = cache_dir / "build"
    cp_file = cache_dir / "classpath.txt"
    shutil.rmtree(sdk_classes_dir, ignore_errors=True)
    shutil.rmtree(sdk_build_dir, ignore_errors=True)
    sdk_classes_dir.mkdir(parents=True, exist_ok=True)
    sdk_build = subprocess.run(
        [
            MAVEN,
            "-q",
            "--batch-mode",
            "-DskipTests",
            f"-Dproject.build.directory={sdk_build_dir}",
            f"-Dmaven.compiler.outputDirectory={sdk_classes_dir}",
            "compile",
        ],
        capture_output=True,
        text=True,
        cwd=str(java_sdk),
    )
    if sdk_build.returncode != 0:
        return None
    cp_result = subprocess.run(
        [
            MAVEN,
            "-q",
            "--batch-mode",
            f"-Dproject.build.directory={sdk_build_dir}",
            "dependency:build-classpath",
            f"-Dmdep.outputFile={cp_file}",
        ],
        capture_output=True,
        text=True,
        cwd=str(java_sdk),
    )
    if cp_result.returncode != 0:
        return None
    return _cached_java_classpath(cache_dir)


def _java_classpath(java_sdk: Path) -> str | None:
    cache_dir = JAVA_SNIPPET_SDK_CACHE_DIR / _java_sdk_fingerprint(java_sdk)
    cached = _cached_java_classpath(cache_dir)
    if cached is not None:
        return cached
    with cache_lock(cache_dir, "Java SDK cache"):
        cached = _cached_java_classpath(cache_dir)
        if cached is not None:
            return cached
        classpath = _compile_java_sdk_direct(java_sdk, cache_dir)
        if classpath is not None:
            return classpath
        classpath = _compile_java_sdk_with_maven(java_sdk, cache_dir)
        if classpath is not None:
            return classpath
        return _cached_java_classpath(cache_dir)


def _javac_is_usable() -> bool:
    if JAVAC is None:
        return False
    result = subprocess.run(
        [JAVAC, "-version"],
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0 and "Unable to locate a Java Runtime" not in output


def _java_wrapper_class_name(index: int) -> str:
    return f"Snippet_{index:04d}"


def _java_has_explicit_class(code: str) -> bool:
    return re.search(r"\bclass\s+\w+", code) is not None


def _java_compile_cache_key(
    snippet_files: list[tuple[Snippet, Path]],
    java_sdk: Path,
    classpath: str,
) -> str:
    class_name_by_source = {
        src.name: _java_wrapper_class_name(index)
        for index, (_, src) in enumerate(snippet_files)
    }
    digest = hashlib.sha256()
    digest.update(_java_sdk_fingerprint(java_sdk).encode("utf-8"))
    digest.update(b"\0")
    digest.update(classpath.encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        hash_snippets(
            snippet_files,
            lambda snippet, src: normalise_java_snippet(
                snippet.code,
                wrapper_class_name=class_name_by_source[src.name],
                rename_explicit_class=_java_has_explicit_class(snippet.code),
            ),
        ).encode("utf-8")
    )
    return digest.hexdigest()[:24]


def lint_java_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files or not _javac_is_usable():
        return []
    java_sdk_issue = _sdk_root_issue(
        snippet_files[0][0],
        "java sdk",
        JAVA_SDK_FOR_SNIPPETS,
        ("pom.xml", "src/main/java/com/retab/RetabClient.java"),
    )
    if java_sdk_issue is not None:
        return [java_sdk_issue]
    java_sdk = _java_sdk_for_build()
    classpath = _java_classpath(java_sdk)
    if classpath is None:
        return []
    compile_cache_dir = JAVA_SNIPPET_COMPILE_CACHE_DIR / _java_compile_cache_key(
        snippet_files,
        java_sdk,
        classpath,
    )
    by_path = {str(path): snippet for snippet, path in snippet_files}
    diag_re = re.compile(
        r"^(?P<file>.+\.java):(?P<line>\d+):\s+error:\s+(?P<msg>.+)$"
    )

    def run_javac(
        source_files: list[Path],
        batch_index: int,
        classes_dir: Path,
    ) -> list[LintIssue]:
        if not source_files:
            return []
        batch_classes_dir = classes_dir / f"batch_{batch_index:04d}"
        batch_classes_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                JAVAC,
                "-Xlint:none",
                "-cp",
                classpath,
                "-d",
                str(batch_classes_dir),
                *(str(file_path) for file_path in source_files),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return []
        output = ((result.stderr or "") + (result.stdout or "")).strip()
        matched = False
        local_issues: list[LintIssue] = []
        for raw in output.splitlines():
            m = diag_re.match(raw.strip())
            if m is None:
                continue
            matched = True
            source_snippet = by_path.get(
                m.group("file"),
                by_path[str(source_files[0])],
            )
            local_issues.append(
                LintIssue.for_snippet(
                    source_snippet,
                    "javac",
                    f"{m.group('msg')} (line {m.group('line')})",
                )
            )
        if not matched and output:
            local_issues.append(
                LintIssue.for_snippet(by_path[str(source_files[0])], "javac", output)
            )
        return local_issues

    def work() -> list[LintIssue]:
        classes_dir = SNIPPET_DIR / "_javaclasses"
        shutil.rmtree(classes_dir, ignore_errors=True)
        classes_dir.mkdir(parents=True, exist_ok=True)
        java_files: list[Path] = []
        for index, (snippet, file_path) in enumerate(snippet_files):
            class_name = _java_wrapper_class_name(index)
            file_path.write_text(
                normalise_java_snippet(
                    snippet.code,
                    wrapper_class_name=class_name,
                    rename_explicit_class=_java_has_explicit_class(snippet.code),
                ),
                encoding="utf-8",
            )
            java_files.append(file_path)
        return run_javac(java_files, 0, classes_dir)

    return cached_success(compile_cache_dir, "Java snippet compile", work)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def reset_snippet_dir() -> None:
    if SNIPPET_DIR.exists():
        shutil.rmtree(SNIPPET_DIR)
    for attempt in range(3):
        try:
            SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.05)


def cleanup_snippet_dir() -> None:
    shutil.rmtree(SNIPPET_DIR, ignore_errors=True)


def group_issues(issues: list[LintIssue]) -> dict[Path, list[LintIssue]]:
    out: dict[Path, list[LintIssue]] = {}
    for issue in issues:
        out.setdefault(issue.source, []).append(issue)
    return out


@dataclass(frozen=True)
class PhaseTiming:
    name: str
    seconds: float


class PhaseTimer:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.timings: list[PhaseTiming] = []

    @contextmanager
    def record(self, name: str):
        if not self.enabled:
            yield
            return
        started_at = time.monotonic()
        try:
            yield
        finally:
            self.timings.append(PhaseTiming(name, time.monotonic() - started_at))

    def print(self) -> None:
        if not self.enabled:
            return
        print("\nSnippet lint timings:", file=sys.stderr)
        for timing in self.timings:
            print(f"  {timing.name}: {timing.seconds:.3f}s", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--language",
        choices=[*LINT_LANGUAGES, "all"],
        default="all",
        help="Restrict the lint pass to a single language.",
    )
    parser.add_argument(
        "--no-pyright",
        action="store_true",
        help="Skip pyright (still run py_compile and ruff).",
    )
    parser.add_argument(
        "--no-tsc",
        action="store_true",
        help="Skip TypeScript checking.",
    )
    parser.add_argument(
        "--no-go",
        action="store_true",
        help="Skip Go checking.",
    )
    parser.add_argument(
        "--no-rust",
        action="store_true",
        help="Skip Rust checking.",
    )
    parser.add_argument(
        "--no-php",
        action="store_true",
        help="Skip PHP checking.",
    )
    parser.add_argument(
        "--no-ruby",
        action="store_true",
        help="Skip Ruby checking.",
    )
    parser.add_argument(
        "--no-dotnet",
        action="store_true",
        help="Skip .NET checking.",
    )
    parser.add_argument(
        "--no-java",
        action="store_true",
        help="Skip Java checking.",
    )
    parser.add_argument(
        "--no-structural-checks",
        action="store_true",
        help="Skip JavaScript-fence and SDK language coverage checks.",
    )
    parser.add_argument(
        "--no-codegroup-coverage",
        action="store_true",
        help="Skip grouped SDK language coverage checks.",
    )
    parser.add_argument(
        "--structural-only",
        action="store_true",
        help="Run docs snippet structure checks without language toolchains.",
    )
    parser.add_argument(
        "--filter",
        default="",
        help="Substring filter on the source file path (e.g. 'primitives').",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Read a pre-extracted snippet manifest instead of scanning docs.",
    )
    parser.add_argument(
        "--write-manifest",
        type=Path,
        help="Write a snippet manifest and exit without running lint checks.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep extracted snippets in .snippets/ for inspection.",
    )
    parser.add_argument(
        "--timings",
        action="store_true",
        help="Print coarse phase timings to stderr.",
    )
    args = parser.parse_args()
    timer = PhaseTimer(args.timings)

    if args.write_manifest is not None and args.manifest is not None:
        parser.error("--write-manifest and --manifest are mutually exclusive")

    if args.manifest is not None:
        with timer.record("manifest.load"):
            manifest = load_snippet_manifest(args.manifest)
        snippet_source = "Loaded"
    else:
        with timer.record("manifest.extract"):
            manifest = extract_manifest_from_docs(iter_doc_files(DOCS_ROOT))
        snippet_source = "Extracted"
    with timer.record("manifest.filter"):
        manifest = filter_snippet_manifest(manifest, args.filter)
    docs, all_snippets, all_groups = manifest

    if args.write_manifest is not None:
        with timer.record("manifest.write"):
            write_snippet_manifest(args.write_manifest, manifest)
        print(
            f"Wrote {len(all_snippets)} snippets from {len(docs)} files "
            f"to {args.write_manifest}"
        )
        timer.print()
        return 0

    with timer.record("scratch.reset"):
        reset_snippet_dir()

    with timer.record("snippets.index_by_language"):
        by_lang: dict[str, list[Snippet]] = {}
        for s in all_snippets:
            by_lang.setdefault(s.language, []).append(s)

    print(
        f"{snippet_source} {len(all_snippets)} snippets from {len(docs)} files "
        f"({', '.join(f'{k}={len(v)}' for k, v in sorted(by_lang.items()))})"
    )

    issues: list[LintIssue] = []

    # --- Structural docs checks --------------------------------------
    if not args.no_structural_checks:
        with timer.record("structural"):
            issues.extend(check_javascript_fences(all_snippets))
            issues.extend(check_flat_workflow_accessors(all_snippets))
            if not args.no_codegroup_coverage:
                issues.extend(check_code_group_coverage(all_groups))
                issues.extend(check_placeholder_sdk_tabs(all_groups))

    # --- Python --------------------------------------------------------
    if args.structural_only:
        pass
    elif args.language in {"all", "python"}:
        with timer.record("python"):
            py_snippets = [s for s in by_lang.get("python", []) if is_self_contained_python(s)]
            py_files: list[tuple[Snippet, Path]] = []
            for snippet in py_snippets:
                file_path = write_snippet(snippet)
                py_files.append((snippet, file_path))
            issues.extend(
                lint_python_cached_batch(
                    py_files,
                    run_pyright=not args.no_pyright and PYRIGHT.exists(),
                )
            )

    # --- TypeScript ----------------------------------------------------
    if not args.structural_only and args.language in {"all", "typescript"} and not args.no_tsc:
        with timer.record("typescript"):
            ts_snippets: list[Snippet] = []
            ts_snippets.extend(
                s for s in by_lang.get("typescript", []) if is_self_contained_ts(s)
            )
            ts_files: list[tuple[Snippet, Path]] = []
            for snippet in ts_snippets:
                file_path = write_snippet(snippet)
                ts_files.append((snippet, file_path))
            issues.extend(lint_typescript_batch(ts_files))

    # --- Go ------------------------------------------------------------
    if not args.structural_only and args.language in {"all", "go"} and not args.no_go:
        with timer.record("go"):
            go_files: list[tuple[Snippet, Path]] = []
            for snippet in (s for s in by_lang.get("go", []) if is_self_contained_go(s)):
                file_path = write_snippet(snippet)
                go_files.append((snippet, file_path))
            issues.extend(lint_go_batch(go_files))

    # --- Rust ----------------------------------------------------------
    if not args.structural_only and args.language in {"all", "rust"} and not args.no_rust:
        with timer.record("rust"):
            rust_files: list[tuple[Snippet, Path]] = []
            for snippet in (
                s for s in by_lang.get("rust", []) if is_self_contained_rust(s)
            ):
                file_path = write_snippet(snippet)
                rust_files.append((snippet, file_path))
            issues.extend(lint_rust_batch(rust_files))

    # --- PHP -----------------------------------------------------------
    if not args.structural_only and args.language in {"all", "php"} and not args.no_php:
        with timer.record("php"):
            php_files: list[tuple[Snippet, Path]] = []
            for snippet in (s for s in by_lang.get("php", []) if is_self_contained_php(s)):
                file_path = write_snippet(snippet)
                php_files.append((snippet, file_path))
            issues.extend(lint_php_cached_batch(php_files))

    # --- Ruby ----------------------------------------------------------
    if not args.structural_only and args.language in {"all", "ruby"} and not args.no_ruby:
        with timer.record("ruby"):
            ruby_files: list[tuple[Snippet, Path]] = []
            for snippet in (
                s for s in by_lang.get("ruby", []) if is_self_contained_ruby(s)
            ):
                file_path = write_snippet(snippet)
                ruby_files.append((snippet, file_path))
            issues.extend(lint_ruby_batch(ruby_files))

    # --- .NET ----------------------------------------------------------
    if not args.structural_only and args.language in {"all", "dotnet"} and not args.no_dotnet:
        with timer.record("dotnet"):
            dotnet_files: list[tuple[Snippet, Path]] = []
            for snippet in (
                s for s in by_lang.get("dotnet", []) if is_self_contained_dotnet(s)
            ):
                file_path = write_snippet(snippet)
                dotnet_files.append((snippet, file_path))
            issues.extend(lint_dotnet_batch(dotnet_files))

    # --- Java ----------------------------------------------------------
    if not args.structural_only and args.language in {"all", "java"} and not args.no_java:
        with timer.record("java"):
            java_files: list[tuple[Snippet, Path]] = []
            for snippet in (
                s for s in by_lang.get("java", []) if is_self_contained_java(s)
            ):
                file_path = write_snippet(snippet)
                java_files.append((snippet, file_path))
            issues.extend(lint_java_batch(java_files))

    grouped = group_issues(issues)
    if not grouped:
        skipped_by_language = {
            language: sum(
                1
                for snippet in all_snippets
                if snippet.language == language and is_contextual_sdk_snippet(snippet)
            )
            for language in LINT_LANGUAGES
        }
        skipped_display = ", ".join(
            f"{language}={count}" for language, count in skipped_by_language.items()
        )
        print(
            "\nAll linted snippets passed. "
            "Contextual SDK snippets not typechecked: "
            f"{skipped_display}."
        )
        timer.print()
        if not args.keep:
            cleanup_snippet_dir()
        return 0

    by_checker = Counter(issue.checker for issue in issues)
    summary = ", ".join(
        f"{checker}={count}" for checker, count in sorted(by_checker.items())
    )
    print(f"\n{len(issues)} issue(s) across {len(grouped)} file(s) ({summary}):\n")
    for source in sorted(grouped):
        rel = source.relative_to(DOCS_ROOT)
        print(f"=== {rel} ===")
        for issue in grouped[source]:
            prefix = f"  L{issue.start_line} {issue.language} [{issue.checker}]"
            for line in issue.message.splitlines():
                print(f"{prefix} {line}")
                prefix = " " * len(prefix)
        print()
    timer.print()
    if not args.keep:
        cleanup_snippet_dir()
    return 1


if __name__ == "__main__":
    sys.exit(main())
