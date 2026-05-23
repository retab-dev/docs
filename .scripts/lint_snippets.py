#!/usr/bin/env python3
"""Extract fenced code blocks from ``open-source/docs/**/*.{md,mdx}`` and lint
them against the local Retab SDKs (Python + TypeScript/Node).

The script keeps drift between the docs and the generated SDKs honest. For each
fenced block tagged with a language we know how to check we:

  1. Write the block to a temporary file under ``open-source/docs/.snippets/``.
  2. Run a language-appropriate checker:
       * Python   -> ``py_compile`` (syntax) + ``ruff check`` (undefined names)
                      + ``pyright`` if available (full type-check against the
                      installed ``retab`` package).
       * TypeScript/JavaScript -> ``tsc --noEmit`` against the local
                      ``@retab/node`` SDK via a synthetic ``tsconfig.json``.
  3. Check docs structure:
       * Node snippets must use TypeScript fences, not JavaScript fences.
       * SDK example groups must include Python, TypeScript, Go, Rust, .NET,
         PHP, and Ruby variants.
  4. Aggregate results and print a per-source-file punch list.

Exit code is non-zero when any snippet fails. The script is intended as a
local guardrail / CI hook — production builds do not depend on it.
"""

from __future__ import annotations

import argparse
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
PY_VENV = REPO_ROOT / "backend" / "main_server" / ".venv"

RUFF = PY_VENV / "bin" / "ruff"
PYTHON = PY_VENV / "bin" / "python"
PYRIGHT = PY_VENV / "bin" / "pyright"
TSC = NODE_SDK / "node_modules" / "typescript" / "bin" / "tsc"

SNIPPET_DIR = DOCS_ROOT / ".snippets"

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
}

GROUP_COMPONENTS = ("CodeGroup", "RequestExample")

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
    """Return whether a grouped example is intended to show SDK parity.

    Some CodeGroups are package-manager install tabs, JSON output tabs, or
    shell/cURL examples. They may have titles like "Python" or "Ruby", but they
    are not SDK usage examples and should not be forced to include every SDK.
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

    if group.component != "RequestExample":
        code_text = "\n".join(snippet.code for snippet in group.snippets)
        if "X-Retab-Signature" in code_text and "api.retab.com" not in code_text:
            return False
        has_retab_usage = any(
            marker in snippet.code
            for snippet in group.snippets
            for marker in (
                "from retab",
                "import retab",
                "@retab/node",
                "github.com/retab-dev/retab",
                "Retab::Client",
                "Retab\\Client",
                "new Retab",
                "retab::",
                "api.retab.com",
            )
        )
        if not has_retab_usage:
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
    ext = {"python": ".py", "javascript": ".mjs", "typescript": ".mts"}[snippet.language]
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
# Driver
# ---------------------------------------------------------------------------


def reset_snippet_dir() -> None:
    if SNIPPET_DIR.exists():
        shutil.rmtree(SNIPPET_DIR)
    SNIPPET_DIR.mkdir()


def group_issues(issues: list[LintIssue]) -> dict[Path, list[LintIssue]]:
    out: dict[Path, list[LintIssue]] = {}
    for issue in issues:
        out.setdefault(issue.source, []).append(issue)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--language",
        choices=["python", "typescript", "javascript", "all"],
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
        help="Skip TypeScript / JavaScript checking.",
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

    # --- TypeScript / JavaScript --------------------------------------
    if args.language in {"all", "typescript", "javascript"} and not args.no_tsc:
        ts_snippets: list[Snippet] = []
        for lang in ("typescript", "javascript"):
            ts_snippets.extend(
                s for s in by_lang.get(lang, []) if is_self_contained_ts(s)
            )
        ts_files: list[tuple[Snippet, Path]] = []
        for snippet in ts_snippets:
            file_path = write_snippet(snippet)
            ts_files.append((snippet, file_path))
        issues.extend(lint_typescript_batch(ts_files))

    grouped = group_issues(issues)
    if not grouped:
        skipped_python = sum(is_contextual_python_sdk_snippet(s) for s in all_snippets)
        skipped_ts = sum(is_contextual_ts_sdk_snippet(s) for s in all_snippets)
        print(
            "\nAll linted snippets passed. "
            "Contextual SDK snippets not typechecked: "
            f"python={skipped_python}, typescript={skipped_ts}."
        )
        if not args.keep:
            shutil.rmtree(SNIPPET_DIR, ignore_errors=True)
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
        shutil.rmtree(SNIPPET_DIR, ignore_errors=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
