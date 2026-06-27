#!/usr/bin/env python3
"""Normalize fenced code-block tab labels in ``public/docs/**/*.{md,mdx}``.

Docs render fenced blocks as language tabs using the syntax
``` ```<lang> <Label> ```. The canonical label casing across the site is the
one used in ``primitives/Extract.mdx``:

    python      -> Python
    typescript  -> TypeScript
    go          -> Go
    ruby        -> Ruby
    rust        -> Rust
    php         -> PHP
    java        -> Java
    csharp      -> C#
    curl        -> cURL

A historical quirk also renders curl examples as ``` ```bash curl ```; we
rewrite those to ``` ```curl cURL ``` to match the canonical pattern.

Default run is read-only (exits 1 on drift, like a linter). Pass ``--fix`` to
rewrite files in place.

Out of scope (left untouched):
- ``json 200``/``json 404``/``json Response`` and other non-SDK status labels.
- ``bash Python`` / ``bash Go`` etc. used for install-command tabs, where the
  language is genuinely bash and the label intentionally names the SDK.
- Any fence without a label.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DOCS_ROOT = Path(__file__).resolve().parents[1]

# Map (fence-lang, label.lower()) -> (canonical-fence-lang, canonical-label).
# When the entry's fence-lang differs from the source, the fence itself is
# also rewritten (used for ``bash curl`` -> ``curl cURL``).
NORMALIZATIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("python", "python"): ("python", "Python"),
    ("typescript", "typescript"): ("typescript", "TypeScript"),
    ("go", "go"): ("go", "Go"),
    ("ruby", "ruby"): ("ruby", "Ruby"),
    ("rust", "rust"): ("rust", "Rust"),
    ("php", "php"): ("php", "PHP"),
    ("java", "java"): ("java", "Java"),
    ("csharp", "csharp"): ("csharp", "C#"),
    ("csharp", "c#"): ("csharp", "C#"),
    ("csharp", "cs"): ("csharp", "C#"),
    ("curl", "curl"): ("curl", "cURL"),
    ("bash", "curl"): ("curl", "cURL"),
}

FENCE_RE = re.compile(r"^(?P<indent>\s*)```(?P<lang>[A-Za-z0-9_#+.\-]+)\s+(?P<label>\S+)\s*$")


@dataclass
class Change:
    path: Path
    line_no: int  # 1-based
    before: str
    after: str


def normalize_file(path: Path) -> tuple[str, list[Change]]:
    original = path.read_text(encoding="utf-8")
    out_lines: list[str] = []
    changes: list[Change] = []
    for idx, line in enumerate(original.splitlines(keepends=True), start=1):
        stripped = line.rstrip("\n")
        # Preserve trailing newline shape (\n vs no-newline-at-eof).
        trailing = line[len(stripped):]
        m = FENCE_RE.match(stripped)
        if not m:
            out_lines.append(line)
            continue
        lang = m.group("lang")
        label = m.group("label")
        key = (lang.lower(), label.lower())
        target = NORMALIZATIONS.get(key)
        if target is None:
            out_lines.append(line)
            continue
        new_lang, new_label = target
        if new_lang == lang and new_label == label:
            out_lines.append(line)
            continue
        new_stripped = f"{m.group('indent')}```{new_lang} {new_label}"
        if new_stripped == stripped:
            out_lines.append(line)
            continue
        new_line = new_stripped + trailing
        changes.append(Change(path=path, line_no=idx, before=stripped, after=new_stripped))
        out_lines.append(new_line)
    return "".join(out_lines), changes


def iter_doc_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".md", ".mdx"}:
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        out.append(path)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Rewrite files in place. Default is check-only (exits 1 on drift).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DOCS_ROOT,
        help="Docs root to scan (default: public/docs/).",
    )
    parser.add_argument(
        "--filter",
        default="",
        help="Substring filter on the source file path (e.g. 'api-reference').",
    )
    args = parser.parse_args()

    files = iter_doc_files(args.root)
    if args.filter:
        files = [f for f in files if args.filter in str(f)]

    all_changes: list[Change] = []
    for path in files:
        new_text, changes = normalize_file(path)
        if not changes:
            continue
        all_changes.extend(changes)
        if args.fix:
            path.write_text(new_text, encoding="utf-8")

    if not all_changes:
        print(f"OK: {len(files)} files scanned, no label-case drift.")
        return 0

    verb = "Fixed" if args.fix else "Drift in"
    print(f"{verb} {len(all_changes)} fence(s) across {len({c.path for c in all_changes})} file(s):\n")
    current: Path | None = None
    for change in all_changes:
        if change.path != current:
            rel = change.path.relative_to(args.root)
            print(f"=== {rel} ===")
            current = change.path
        print(f"  L{change.line_no}: {change.before}  ->  {change.after}")

    if args.fix:
        return 0
    print("\nRun with --fix to apply.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
