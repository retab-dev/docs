#!/usr/bin/env python3
"""Extract fenced code blocks from ``open-source/docs/**/*.{md,mdx}`` and lint
them against the local Retab SDKs.

The script keeps drift between the docs and the generated SDKs honest. For each
fenced block tagged with a language we know how to check we:

  1. Write the block to a temporary file under ``open-source/docs/.snippets/``.
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
from collections import Counter
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_ROOT = REPO_ROOT / "open-source" / "docs"
NODE_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "node"
GO_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "go"
RUST_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "rust"
JAVA_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "java"
DOTNET_SDK = REPO_ROOT / "open-source" / "sdk" / "clients" / "dotnet"
PY_VENV = REPO_ROOT / "backend" / "main_server" / ".venv"

RUFF = PY_VENV / "bin" / "ruff"
PYTHON = PY_VENV / "bin" / "python"
PYRIGHT = PY_VENV / "bin" / "pyright"
TSC = NODE_SDK / "node_modules" / "typescript" / "bin" / "tsc"

GO = shutil.which("go")
CARGO = shutil.which("cargo")
MAVEN = shutil.which("mvn")
PHP = shutil.which("php")
RUBY = shutil.which("ruby")
DOTNET = shutil.which("dotnet")
JAVAC = shutil.which("javac")
NUGET_PACKAGES = Path.home() / ".nuget" / "packages"

SNIPPET_ROOT = DOCS_ROOT / ".snippets"
SNIPPET_DIR = SNIPPET_ROOT / f"run_{os.getpid()}"

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


def lint_python(snippet: Snippet, file_path: Path) -> list[LintIssue]:
    issues: list[LintIssue] = []
    # 1. Syntax via py_compile.
    try:
        py_compile.compile(str(file_path), doraise=True)
    except py_compile.PyCompileError as exc:
        issues.append(LintIssue.for_snippet(snippet, "py_compile", str(exc).strip()))
        return issues  # No point running ruff/pyright on broken syntax.

    # 2. Ruff — keep selection narrow so style noise doesn't drown signal.
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
            str(file_path),
        ],
        capture_output=True,
        text=True,
    )
    if ruff_result.stdout.strip():
        try:
            findings = json.loads(ruff_result.stdout)
        except json.JSONDecodeError:
            findings = []
        for f in findings:
            issues.append(
                LintIssue.for_snippet(
                    snippet,
                    "ruff",
                    f"{f['code']} {f['message']} (line {f['location']['row']})",
                )
            )
    return issues


PY_SDK_SRC = REPO_ROOT / "open-source" / "sdk" / "clients" / "python"


def lint_python_batch_pyright(
    snippet_files: list[tuple[Snippet, Path]],
) -> list[LintIssue]:
    """Run pyright once across every Python snippet. Pyright startup is heavy
    (Node + bundled type stubs) so batching matters."""

    if not PYRIGHT.exists():
        return []
    if not snippet_files:
        return []

    # Pyright doesn't follow editable installs reliably. Pin the SDK source
    # via `extraPaths` so `from retab import ...` resolves to the in-repo
    # package — the same source the generated SDK ships.
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
                "extraPaths": [str(PY_SDK_SRC)],
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


TSCONFIG_TEMPLATE: dict[str, object] = {
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
            "./node-sdk/node_modules/@types",
        ],
        "baseUrl": ".",
        "paths": {
            "@retab/node": ["./node-sdk/dist/index.d.ts"],
            "@retab/node/*": ["./node-sdk/dist/*"],
            # Allow snippets that import `zod` directly to resolve from
            # the SDK's installed copy (the SDK depends on zod itself).
            "zod": ["./node-sdk/node_modules/zod/index.d.ts"],
        },
    },
    "include": ["snippets/**/*"],
}


def _prepare_ts_workspace(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    """Create an isolated workspace that mirrors `@retab/node` so the SDK
    surface is type-checked end-to-end without polluting the doc tree."""

    ws = SNIPPET_DIR / "_tsworkspace"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    # Symlink the built SDK so tsc resolves `@retab/node` to the same
    # declarations a consumer would see after `npm install @retab/node`.
    sdk_link = ws / "node-sdk"
    sdk_link.symlink_to(NODE_SDK)
    snippets_dir = ws / "snippets"
    snippets_dir.mkdir()
    for snippet, src in snippet_files:
        dst = snippets_dir / src.name
        dst.write_text(snippet.code, encoding="utf-8")
    tsconfig = ws / "tsconfig.json"
    tsconfig.write_text(json.dumps(TSCONFIG_TEMPLATE, indent=2), encoding="utf-8")
    return ws


def lint_typescript_batch(
    snippet_files: list[tuple[Snippet, Path]],
) -> list[LintIssue]:
    if not snippet_files:
        return []
    if not TSC.exists():
        return []
    ws = _prepare_ts_workspace(snippet_files)
    # `tsc` emits diagnostics to stdout when --pretty=false and writes nothing
    # on success.
    result = subprocess.run(
        [
            str(TSC),
            "-p",
            str(ws / "tsconfig.json"),
            "--pretty",
            "false",
        ],
        capture_output=True,
        text=True,
        cwd=str(ws),
    )
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


def _prepare_go_workspace(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    ws = SNIPPET_DIR / "_goworkspace"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    (ws / "go.mod").write_text(
        "\n".join(
            [
                "module retab_docs_snippets",
                "",
                "go 1.23",
                "",
                "require github.com/retab-dev/retab/clients/go v0.0.0",
                f"replace github.com/retab-dev/retab/clients/go => {GO_SDK}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    sdk_go_sum = GO_SDK / "go.sum"
    if sdk_go_sum.exists():
        shutil.copyfile(sdk_go_sum, ws / "go.sum")
    for snippet, src in snippet_files:
        snippet_dir = ws / "snippets" / src.stem
        snippet_dir.mkdir(parents=True)
        (snippet_dir / "main.go").write_text(
            normalise_go_snippet(snippet.code),
            encoding="utf-8",
        )
    return ws


def lint_go_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files or GO is None:
        return []
    ws = _prepare_go_workspace(snippet_files)
    result = subprocess.run(
        [GO, "test", "-mod=mod", "./..."],
        capture_output=True,
        text=True,
        cwd=str(ws),
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return []
    by_name = {src.stem: snippet for snippet, src in snippet_files}
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


def _prepare_rust_workspace(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    ws = SNIPPET_DIR / "_rustworkspace"
    if ws.exists():
        shutil.rmtree(ws)
    (ws / "src" / "bin").mkdir(parents=True)
    (ws / "Cargo.toml").write_text(
        "\n".join(
            [
                "[package]",
                'name = "retab_docs_snippets"',
                'version = "0.0.0"',
                'edition = "2021"',
                "",
                "[dependencies]",
                f'retab = {{ path = "{RUST_SDK}" }}',
                'tokio = { version = "1", features = ["rt-multi-thread", "macros"] }',
                'reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls"] }',
                'serde_json = "1"',
                'base64 = "0.22"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    for snippet, src in snippet_files:
        (ws / "src" / "bin" / src.name).write_text(
            normalise_rust_snippet(snippet.code),
            encoding="utf-8",
        )
    return ws


def lint_rust_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files or CARGO is None:
        return []
    ws = _prepare_rust_workspace(snippet_files)
    result = subprocess.run(
        [CARGO, "check", "--bins", "--message-format", "short"],
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
        r"^src/bin/(?P<name>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*"
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


def lint_php(snippet: Snippet, file_path: Path) -> list[LintIssue]:
    if PHP is None:
        return []
    file_path.write_text(normalise_php_snippet(snippet.code), encoding="utf-8")
    result = subprocess.run(
        [PHP, "-l", str(file_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    output = ((result.stderr or "") + (result.stdout or "")).strip()
    return [LintIssue.for_snippet(snippet, "php -l", output)]


def lint_ruby(snippet: Snippet, file_path: Path) -> list[LintIssue]:
    if RUBY is None:
        return []
    result = subprocess.run(
        [RUBY, "-c", str(file_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    output = ((result.stderr or "") + (result.stdout or "")).strip()
    return [LintIssue.for_snippet(snippet, "ruby -c", output)]


# ---------------------------------------------------------------------------
# .NET / C#
# ---------------------------------------------------------------------------


def _prepare_dotnet_workspace(snippet_files: list[tuple[Snippet, Path]]) -> Path:
    ws = SNIPPET_DIR / "_dotnetworkspace"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    for snippet, src in snippet_files:
        project_dir = ws / src.stem
        project_dir.mkdir()
        (project_dir / "Program.cs").write_text(
            "\n".join(
                [
                    "global using System;",
                    "global using System.Collections.Generic;",
                    "global using System.IO;",
                    "global using System.Linq;",
                    "global using System.Threading.Tasks;",
                    snippet.code,
                ]
            ),
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


def _dotnet_references() -> list[Path]:
    sdk_root = _dotnet_sdk_root()
    if sdk_root is None:
        return []
    dotnet_root = sdk_root.parent.parent
    ref_pack_root = dotnet_root / "packs" / "Microsoft.NETCore.App.Ref"
    ref_dirs = sorted(ref_pack_root.glob("*/ref/net*"))
    refs = list(ref_dirs[-1].glob("*.dll")) if ref_dirs else []
    refs.extend(
        [
            DOTNET_SDK / "bin" / "Debug" / "net8.0" / "Retab.dll",
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


def _dotnet_csc() -> Path | None:
    sdk_root = _dotnet_sdk_root()
    if sdk_root is None:
        return None
    csc = sdk_root / "Roslyn" / "bincore" / "csc.dll"
    return csc if csc.exists() else None


def lint_dotnet_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files or DOTNET is None:
        return []
    sdk_build_cmd = [
        DOTNET,
        "build",
        str(DOTNET_SDK / "Retab.csproj"),
        "--nologo",
        "--verbosity",
        "quiet",
        "/nodeReuse:false",
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
        return [LintIssue.for_snippet(snippet_files[0][0], "dotnet", output)]

    ws = _prepare_dotnet_workspace(snippet_files)
    csc = _dotnet_csc()
    refs = _dotnet_references()
    if csc is None or not refs:
        return []
    by_stem = {src.stem: snippet for snippet, src in snippet_files}
    max_workers = min(8, max(1, os.cpu_count() or 1))

    def build_one(src: Path) -> list[LintIssue]:
        project_dir = ws / src.stem
        response_file = project_dir / "csc.rsp"
        response_file.write_text(
            "\n".join(
                [
                    "-nologo",
                    "-target:exe",
                    "-langversion:12",
                    "-nullable:enable",
                    f"-out:{project_dir / 'Snippet.dll'}",
                    *(f"-r:{ref}" for ref in refs),
                    str(project_dir / "Program.cs"),
                ]
            ),
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [DOTNET, str(csc), f"@{response_file}"],
                capture_output=True,
                text=True,
                cwd=str(project_dir),
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return [
                LintIssue.for_snippet(
                    by_stem[src.stem],
                    "dotnet",
                    "dotnet csc timed out after 30s",
                )
            ]
        if result.returncode == 0:
            return []
        output = (result.stdout or "") + (result.stderr or "")
        snippet = by_stem[src.stem]
        matched_diagnostic = False
        local_issues: list[LintIssue] = []
        diag_re = re.compile(
            r"Program\.cs\((?P<line>\d+),(?P<col>\d+)\):\s+"
            r"(?P<level>error|warning)\s+(?P<code>CS\d+):\s+(?P<msg>.+?)(?:\s+\[|$)"
        )
        for raw in output.splitlines():
            m = diag_re.search(raw.strip())
            if m is None:
                continue
            matched_diagnostic = True
            if m.group("level") != "error":
                continue
            issue = LintIssue.for_snippet(
                snippet,
                "dotnet",
                f"{m.group('code')} {m.group('msg')} (line {m.group('line')})",
            )
            if issue not in local_issues:
                local_issues.append(issue)
        if not matched_diagnostic and output.strip():
            local_issues.append(LintIssue.for_snippet(snippet, "dotnet", output.strip()))
        return local_issues

    issues: list[LintIssue] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(build_one, src) for _, src in snippet_files]
        for future in concurrent.futures.as_completed(futures):
            issues.extend(future.result())
    return issues


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


def normalise_java_snippet(code: str) -> str:
    if re.search(r"\bclass\s+\w+", code) is not None:
        return re.sub(r"\bpublic\s+(?=(?:final\s+)?class\s+\w+)", "", code)
    imports, body = _split_leading_java_imports(code)
    indented = "\n".join(f"    {line}" if line else "" for line in body.splitlines())
    return (
        f"{imports}\n\n"
        "final class Snippet {\n"
        "  public static void main(String[] args) throws Exception {\n"
        f"{indented}\n"
        "  }\n"
        "}\n"
    )


def _java_classpath() -> str | None:
    if MAVEN is None:
        return None
    sdk_classes_dir = SNIPPET_DIR / "_javasdk_classes"
    sdk_build_dir = SNIPPET_DIR / "_javasdk_build"
    cp_file = SNIPPET_DIR / "_java_classpath.txt"
    shutil.rmtree(sdk_classes_dir, ignore_errors=True)
    shutil.rmtree(sdk_build_dir, ignore_errors=True)
    sdk_classes_dir.mkdir(parents=True)
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
        cwd=str(JAVA_SDK),
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
        cwd=str(JAVA_SDK),
    )
    if cp_result.returncode != 0:
        return None
    if not (sdk_classes_dir / "com" / "retab").is_dir():
        return None
    parts = [str(sdk_classes_dir)]
    if cp_file.exists() and cp_file.read_text(encoding="utf-8").strip():
        parts.append(cp_file.read_text(encoding="utf-8").strip())
    return os.pathsep.join(parts)


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


def lint_java_batch(snippet_files: list[tuple[Snippet, Path]]) -> list[LintIssue]:
    if not snippet_files or not _javac_is_usable():
        return []
    classpath = _java_classpath()
    if classpath is None:
        return []
    classes_dir = SNIPPET_DIR / "_javaclasses"
    shutil.rmtree(classes_dir, ignore_errors=True)
    classes_dir.mkdir(parents=True, exist_ok=True)
    issues: list[LintIssue] = []
    by_path = {str(path): snippet for snippet, path in snippet_files}
    diag_re = re.compile(
        r"^(?P<file>.+\.java):(?P<line>\d+):\s+error:\s+(?P<msg>.+)$"
    )
    for snippet, file_path in snippet_files:
        file_path.write_text(normalise_java_snippet(snippet.code), encoding="utf-8")
        result = subprocess.run(
            [
                JAVAC,
                "-Xlint:none",
                "-cp",
                classpath,
                "-d",
                str(classes_dir),
                str(file_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            continue
        output = ((result.stderr or "") + (result.stdout or "")).strip()
        matched = False
        for raw in output.splitlines():
            m = diag_re.match(raw.strip())
            if m is None:
                continue
            matched = True
            source_snippet = by_path.get(m.group("file"), snippet)
            issues.append(
                LintIssue.for_snippet(
                    source_snippet,
                    "javac",
                    f"{m.group('msg')} (line {m.group('line')})",
                )
            )
        if not matched and output:
            issues.append(LintIssue.for_snippet(snippet, "javac", output))
    return issues


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def reset_snippet_dir() -> None:
    if SNIPPET_DIR.exists():
        shutil.rmtree(SNIPPET_DIR)
    SNIPPET_DIR.mkdir(parents=True)


def cleanup_snippet_dir() -> None:
    shutil.rmtree(SNIPPET_DIR, ignore_errors=True)
    try:
        SNIPPET_ROOT.rmdir()
    except OSError:
        pass


def group_issues(issues: list[LintIssue]) -> dict[Path, list[LintIssue]]:
    out: dict[Path, list[LintIssue]] = {}
    for issue in issues:
        out.setdefault(issue.source, []).append(issue)
    return out


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
        "--filter",
        default="",
        help="Substring filter on the source file path (e.g. 'primitives').",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep extracted snippets in .snippets/ for inspection.",
    )
    args = parser.parse_args()

    reset_snippet_dir()

    docs = iter_doc_files(DOCS_ROOT)
    if args.filter:
        docs = [d for d in docs if args.filter in str(d)]

    all_snippets: list[Snippet] = []
    all_groups: list[CodeGroup] = []
    for path in docs:
        snippets = extract_snippets(path)
        all_snippets.extend(snippets)
        all_groups.extend(extract_code_groups(path, snippets))

    by_lang: dict[str, list[Snippet]] = {}
    for s in all_snippets:
        by_lang.setdefault(s.language, []).append(s)

    print(
        f"Extracted {len(all_snippets)} snippets from {len(docs)} files "
        f"({', '.join(f'{k}={len(v)}' for k, v in sorted(by_lang.items()))})"
    )

    issues: list[LintIssue] = []

    # --- Structural docs checks --------------------------------------
    if not args.no_structural_checks:
        issues.extend(check_javascript_fences(all_snippets))
        if not args.no_codegroup_coverage:
            issues.extend(check_code_group_coverage(all_groups))
            issues.extend(check_placeholder_sdk_tabs(all_groups))

    # --- Python --------------------------------------------------------
    if args.language in {"all", "python"}:
        py_snippets = [s for s in by_lang.get("python", []) if is_self_contained_python(s)]
        py_files: list[tuple[Snippet, Path]] = []
        for snippet in py_snippets:
            file_path = write_snippet(snippet)
            py_files.append((snippet, file_path))
            issues.extend(lint_python(snippet, file_path))
        if not args.no_pyright and PYRIGHT.exists():
            issues.extend(lint_python_batch_pyright(py_files))

    # --- TypeScript ----------------------------------------------------
    if args.language in {"all", "typescript"} and not args.no_tsc:
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
    if args.language in {"all", "go"} and not args.no_go:
        go_files: list[tuple[Snippet, Path]] = []
        for snippet in (s for s in by_lang.get("go", []) if is_self_contained_go(s)):
            file_path = write_snippet(snippet)
            go_files.append((snippet, file_path))
        issues.extend(lint_go_batch(go_files))

    # --- Rust ----------------------------------------------------------
    if args.language in {"all", "rust"} and not args.no_rust:
        rust_files: list[tuple[Snippet, Path]] = []
        for snippet in (
            s for s in by_lang.get("rust", []) if is_self_contained_rust(s)
        ):
            file_path = write_snippet(snippet)
            rust_files.append((snippet, file_path))
        issues.extend(lint_rust_batch(rust_files))

    # --- PHP -----------------------------------------------------------
    if args.language in {"all", "php"} and not args.no_php:
        for snippet in (s for s in by_lang.get("php", []) if is_self_contained_php(s)):
            file_path = write_snippet(snippet)
            issues.extend(lint_php(snippet, file_path))

    # --- Ruby ----------------------------------------------------------
    if args.language in {"all", "ruby"} and not args.no_ruby:
        for snippet in (
            s for s in by_lang.get("ruby", []) if is_self_contained_ruby(s)
        ):
            file_path = write_snippet(snippet)
            issues.extend(lint_ruby(snippet, file_path))

    # --- .NET ----------------------------------------------------------
    if args.language in {"all", "dotnet"} and not args.no_dotnet:
        dotnet_files: list[tuple[Snippet, Path]] = []
        for snippet in (
            s for s in by_lang.get("dotnet", []) if is_self_contained_dotnet(s)
        ):
            file_path = write_snippet(snippet)
            dotnet_files.append((snippet, file_path))
        issues.extend(lint_dotnet_batch(dotnet_files))

    # --- Java ----------------------------------------------------------
    if args.language in {"all", "java"} and not args.no_java:
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
    if not args.keep:
        cleanup_snippet_dir()
    return 1


if __name__ == "__main__":
    sys.exit(main())
