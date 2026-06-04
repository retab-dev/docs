import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[2] / ".scripts" / "lint_snippets.py"
SPEC = importlib.util.spec_from_file_location("lint_snippets", MODULE_PATH)
assert SPEC is not None
lint_snippets = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = lint_snippets
SPEC.loader.exec_module(lint_snippets)


def _snippet(
    language: str,
    code: str,
    raw_language: str | None = None,
    title: str = "",
    source: Path | None = None,
) -> lint_snippets.Snippet:
    return lint_snippets.Snippet(
        source=source or lint_snippets.DOCS_ROOT / "example.mdx",
        index=0,
        start_line=1,
        raw_language=raw_language or language,
        language=language,
        title=title,
        code=code,
    )


def _fake_rust_sdk(root: Path) -> Path:
    sdk = root / "rust-sdk"
    (sdk / "src").mkdir(parents=True)
    (sdk / "Cargo.toml").write_text('[package]\nname = "retab"\nversion = "0.0.0"\nedition = "2021"\n')
    (sdk / "src" / "lib.rs").write_text("pub struct Retab;\n")
    return sdk


def _fake_node_sdk(root: Path) -> Path:
    sdk = root / "node-sdk"
    (sdk / "src").mkdir(parents=True)
    (sdk / "package.json").write_text('{"name":"@retab/node"}\n', encoding="utf-8")
    (sdk / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (sdk / "src" / "index.ts").write_text("export class Retab {}\n", encoding="utf-8")
    return sdk


def _fake_node_deps(root: Path) -> Path:
    deps = root / "node-deps"
    for package in (
        "typescript",
        "@types/node",
        "zod",
    ):
        package_dir = deps / "node_modules" / package
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text('{"version":"0.0.0"}\n', encoding="utf-8")
    (deps / "package-lock.json").write_text("{}\n", encoding="utf-8")
    return deps


def test_cached_success_runs_work_once(tmp_path: Path) -> None:
    calls = 0

    def work() -> list[lint_snippets.LintIssue]:
        nonlocal calls
        calls += 1
        return []

    cache_dir = tmp_path / "success-cache"

    assert lint_snippets.cached_success(cache_dir, "test cache", work) == []
    assert lint_snippets.cached_success(cache_dir, "test cache", work) == []

    assert calls == 1
    assert (cache_dir / "success").exists()


def test_cached_success_does_not_mark_failed_work(tmp_path: Path) -> None:
    issue = lint_snippets.LintIssue.for_snippet(
        _snippet("python", "broken"),
        "checker",
        "failed",
    )

    def work() -> list[lint_snippets.LintIssue]:
        return [issue]

    cache_dir = tmp_path / "success-cache"

    assert lint_snippets.cached_success(cache_dir, "test cache", work) == [issue]

    assert not (cache_dir / "success").exists()


def test_cache_lock_removes_lock_after_success(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    lock_dir = tmp_path / "cache.lock"

    with lint_snippets.cache_lock(cache_dir, "test cache"):
        assert lock_dir.exists()

    assert not lock_dir.exists()


def test_cache_lock_recovers_stale_pid_lock(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    lock_dir = tmp_path / "cache.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("123456789", encoding="utf-8")

    with patch.object(lint_snippets, "_process_exists", return_value=False):
        with lint_snippets.cache_lock(cache_dir, "test cache"):
            assert lock_dir.exists()

    assert not lock_dir.exists()


def test_cached_tree_reuses_ready_cache_dir(tmp_path: Path) -> None:
    cache_dir = tmp_path / "tree-cache"
    cache_dir.mkdir()
    (cache_dir / "ready").write_text("ok\n", encoding="utf-8")

    def populate(_: Path) -> None:
        raise AssertionError("ready cache should not be populated")

    assert lint_snippets.cached_tree(
        cache_dir,
        lambda path: (path / "ready").exists(),
        populate,
        "tree cache",
    ) == cache_dir


def test_cached_tree_cleans_stale_tmp_dir(tmp_path: Path) -> None:
    cache_dir = tmp_path / "tree-cache"
    stale_tmp = tmp_path / ".tree-cache.stale.tmp"
    stale_tmp.mkdir()
    (stale_tmp / "stale").write_text("stale\n", encoding="utf-8")

    def populate(tmp_dir: Path) -> None:
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "ready").write_text("ok\n", encoding="utf-8")

    assert lint_snippets.cached_tree(
        cache_dir,
        lambda path: (path / "ready").exists(),
        populate,
        "tree cache",
    ) == cache_dir

    assert not stale_tmp.exists()
    assert (cache_dir / "ready").exists()


def test_required_sdk_group_languages_cover_every_public_sdk() -> None:
    assert lint_snippets.REQUIRED_SDK_GROUP_LANGUAGES == (
        "python",
        "typescript",
        "go",
        "rust",
        "dotnet",
        "php",
        "ruby",
        "java",
    )


def test_phase_timer_records_elapsed_time() -> None:
    timer = lint_snippets.PhaseTimer(enabled=True)

    with patch.object(lint_snippets.time, "monotonic", side_effect=[1.0, 1.25]):
        with timer.record("phase"):
            pass

    assert timer.timings == [lint_snippets.PhaseTiming("phase", 0.25)]


def test_snippet_manifest_round_trip_and_filter(tmp_path: Path) -> None:
    source = lint_snippets.DOCS_ROOT / "example.mdx"
    snippets = [
        lint_snippets.Snippet(
            source=source,
            index=0,
            start_line=3,
            raw_language="python",
            language="python",
            title="Python",
            code="from retab import Retab\n",
        )
    ]
    groups = [
        lint_snippets.CodeGroup(
            source=source,
            component="CodeGroup",
            start_line=1,
            end_line=6,
            snippets=tuple(snippets),
        )
    ]
    manifest_path = tmp_path / "snippet_manifest.json"

    lint_snippets.write_snippet_manifest(
        manifest_path,
        ([source], snippets, groups),
    )
    docs, loaded_snippets, loaded_groups = lint_snippets.load_snippet_manifest(
        manifest_path
    )
    filtered_docs, filtered_snippets, filtered_groups = (
        lint_snippets.filter_snippet_manifest(
            (docs, loaded_snippets, loaded_groups),
            "open-source/docs/example.mdx",
        )
    )

    assert filtered_docs == [source]
    assert filtered_snippets == snippets
    assert filtered_groups == groups


def test_code_group_coverage_requires_all_sdk_languages() -> None:
    group = lint_snippets.CodeGroup(
        source=lint_snippets.DOCS_ROOT / "example.mdx",
        component="CodeGroup",
        start_line=1,
        end_line=20,
        snippets=(
            _snippet("python", "from retab import Retab\n", title="Python"),
            _snippet("typescript", 'import { Retab } from "@retab/node";\n', title="TypeScript"),
            _snippet("go", 'import retab "github.com/retab-dev/retab/clients/go"\n', title="Go"),
        ),
    )

    issues = lint_snippets.check_code_group_coverage([group])

    assert len(issues) == 1
    assert issues[0].checker == "coverage"
    assert "Rust" in issues[0].message
    assert ".NET" in issues[0].message
    assert "PHP" in issues[0].message
    assert "Ruby" in issues[0].message
    assert "Java" in issues[0].message


def test_javascript_fence_does_not_satisfy_typescript_coverage() -> None:
    group = lint_snippets.CodeGroup(
        source=lint_snippets.DOCS_ROOT / "example.mdx",
        component="CodeGroup",
        start_line=1,
        end_line=20,
        snippets=(
            _snippet("python", "from retab import Retab\n", title="Python"),
            _snippet("javascript", 'import { Retab } from "@retab/node";\n', raw_language="javascript", title="JavaScript"),
            _snippet("go", 'import retab "github.com/retab-dev/retab/clients/go"\n', title="Go"),
            _snippet("rust", "use retab::Retab;\n", title="Rust"),
            _snippet("dotnet", "using Retab;\n", raw_language="csharp", title="C#"),
            _snippet("php", "use Retab\\Client;\n", title="PHP"),
            _snippet("ruby", "require 'retab'\n", title="Ruby"),
            _snippet("java", "import com.retab.RetabClient;\n", title="Java"),
        ),
    )

    issues = lint_snippets.check_code_group_coverage([group])

    assert len(issues) == 1
    assert "TypeScript" in issues[0].message
    assert "JavaScript tabs do not satisfy TypeScript coverage" in issues[0].message


def test_typescript_batch_uses_incremental_cached_workspace(tmp_path: Path) -> None:
    node_sdk = tmp_path / "node-sdk"
    node_deps = tmp_path / "node-deps"
    (node_sdk / "src").mkdir(parents=True)
    (node_sdk / "package.json").write_text('{"name":"@retab/node"}\n', encoding="utf-8")
    (node_sdk / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (node_sdk / "src" / "index.ts").write_text("export class Retab {}\n", encoding="utf-8")
    (node_deps / "node_modules" / "typescript" / "bin").mkdir(parents=True)
    (node_deps / "node_modules" / "typescript" / "package.json").write_text("{}\n", encoding="utf-8")
    (node_deps / "node_modules" / "typescript" / "bin" / "tsc").write_text("", encoding="utf-8")
    (node_deps / "node_modules" / "@types" / "node").mkdir(parents=True)
    (node_deps / "node_modules" / "@types" / "node" / "package.json").write_text("{}\n", encoding="utf-8")
    (node_deps / "node_modules" / "zod").mkdir(parents=True)
    (node_deps / "node_modules" / "zod" / "package.json").write_text("{}\n", encoding="utf-8")
    (node_deps / "package-lock.json").write_text("{}\n", encoding="utf-8")
    snippet_files = [
        (
            _snippet("typescript", 'import { Retab } from "@retab/node";\nnew Retab();\n'),
            tmp_path / "snippet.mts",
        )
    ]
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        calls.append((command, Path(str(kwargs["cwd"]))))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(lint_snippets, "NODE", "node"),
        patch.object(
            lint_snippets,
            "TSC",
            node_deps / "node_modules" / "typescript" / "bin" / "tsc",
        ),
        patch.object(lint_snippets, "NODE_SDK_FOR_SNIPPETS", node_sdk),
        patch.object(lint_snippets, "NODE_SDK", node_deps),
        patch.object(lint_snippets, "TS_SNIPPET_WORKSPACE_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_typescript_batch(snippet_files) == []

    assert len(calls) == 1
    command, cwd = calls[0]
    assert "--incremental" in command
    assert "--tsBuildInfoFile" in command
    assert cwd.parent == tmp_path / "cache"
    assert (cwd / "node-sdk").resolve().parent == tmp_path / "cache" / "node-sdk-cache"
    assert (cwd / "snippets" / "snippet.mts").exists()


def test_diagnose_graph_direct_http_sdk_tabs_are_allowed_when_no_sdk_method_exists() -> None:
    source = lint_snippets.DOCS_ROOT / "api-reference/workflows/diagnose-graph.mdx"
    group = lint_snippets.CodeGroup(
        source=source,
        component="CodeGroup",
        start_line=1,
        end_line=20,
        snippets=(
            _snippet(
                "rust",
                'use reqwest::Client;\nClient::new().post("https://api.retab.com/v1/workflows/wf_abc123/diagnose-graph");\n',
                title="Rust",
                source=source,
            ),
        ),
    )

    assert lint_snippets.check_placeholder_sdk_tabs([group]) == []


def test_go_snippet_with_leading_comment_before_package_is_not_wrapped() -> None:
    code = """// Call the API directly.
package main

func main() {}
"""

    assert lint_snippets.normalise_go_snippet(code) == code


def test_go_contextual_fragment_without_import_is_not_linted_as_standalone() -> None:
    snippet = _snippet(
        "go",
        "result, err := client.Extractions.Create(ctx, retab.ExtractionCreateRequest{})\n",
        title="Go",
    )

    assert not lint_snippets.is_self_contained_go(snippet)
    assert lint_snippets.is_contextual_sdk_snippet(snippet)


def test_go_batch_reuses_successful_content_cached_workspace(tmp_path: Path) -> None:
    go_sdk = tmp_path / "go-sdk"
    go_sdk.mkdir()
    (go_sdk / "go.mod").write_text(
        "module github.com/retab-dev/retab/clients/go\n",
        encoding="utf-8",
    )
    (go_sdk / "retab.go").write_text("package retab\n", encoding="utf-8")
    snippet_files = [
        (_snippet("go", 'import retab "github.com/retab-dev/retab/clients/go"\n_ = retab.Retab{}\n'), tmp_path / "snippet.go")
    ]
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        calls.append((command, Path(str(kwargs["cwd"]))))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(lint_snippets, "GO", "go"),
        patch.object(lint_snippets, "GO_SDK_FOR_SNIPPETS", go_sdk),
        patch.object(lint_snippets, "GO_SNIPPET_WORKSPACE_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_go_batch(snippet_files) == []
        assert lint_snippets.lint_go_batch(snippet_files) == []

    assert len(calls) == 1
    assert calls[0][0] == ["go", "test", "-mod=mod", "./..."]
    assert calls[0][1].parent == tmp_path / "cache"


def test_dotnet_contextual_fragment_without_client_declaration_is_not_standalone() -> None:
    snippet = _snippet(
        "dotnet",
        "using Retab;\n\nvar result = await client.Extractions.CreateAsync(new ExtractionsCreateOptions());\n",
        raw_language="csharp",
        title="C#",
    )

    assert not lint_snippets.is_self_contained_dotnet(snippet)
    assert lint_snippets.is_contextual_sdk_snippet(snippet)


def test_dotnet_snippet_is_wrapped_in_class() -> None:
    code = (
        "using Retab;\n"
        "using RetabClient = Retab.Retab;\n\n"
        "var client = new RetabClient(\"test\");\n"
        "await client.Splits.DeleteAsync(\"split_abc123\");\n"
    )

    wrapped = lint_snippets.normalise_dotnet_snippet(code, "Snippet_0001")

    assert "using Retab;" in wrapped
    assert "internal static class Snippet_0001" in wrapped
    assert "public static async Task RunAsync()" in wrapped
    assert "    var client = new RetabClient" in wrapped


def test_dotnet_workspace_checks_snippets_in_one_compilation(tmp_path: Path) -> None:
    snippets = [
        (
            _snippet("dotnet", f"using Retab;\n\nvar value = {index};\n"),
            tmp_path / f"snippet_{index}.cs",
        )
        for index in range(2)
    ]

    with patch.object(lint_snippets, "SNIPPET_DIR", tmp_path):
        workspace = lint_snippets._prepare_dotnet_workspace(snippets)

    assert (workspace / "GlobalUsings.cs").exists()
    assert "class Snippet_0000" in (workspace / "snippet_0.cs").read_text(
        encoding="utf-8"
    )
    assert "class Snippet_0001" in (workspace / "snippet_1.cs").read_text(
        encoding="utf-8"
    )
    assert not (workspace / "snippet_0").exists()


def test_dotnet_sdk_build_uses_fingerprinted_cache(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "src").mkdir(parents=True)
    (sdk / "Retab.csproj").write_text("<Project />\n", encoding="utf-8")
    (sdk / "src" / "Retab.Generated.cs").write_text(
        "namespace Retab;\npublic sealed class Retab {}\n",
        encoding="utf-8",
    )

    with patch.object(lint_snippets, "DOTNET_SNIPPET_SDK_CACHE_DIR", tmp_path / "cache"):
        fingerprint = lint_snippets._dotnet_sdk_fingerprint(sdk)
        cached_assembly = tmp_path / "cache" / fingerprint / "bin" / "Retab.dll"
        cached_assembly.parent.mkdir(parents=True)
        cached_assembly.write_text("compiled", encoding="utf-8")

        with patch.object(lint_snippets.subprocess, "run") as run:
            assembly, issue = lint_snippets._build_dotnet_sdk_for_snippets(
                sdk,
                _snippet("dotnet", "using Retab;\n"),
            )

    assert assembly == cached_assembly
    assert issue is None
    run.assert_not_called()


def test_dotnet_batch_reuses_successful_compile_cache(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "src").mkdir(parents=True)
    (sdk / "Retab.csproj").write_text("<Project />\n", encoding="utf-8")
    (sdk / "src" / "Retab.Generated.cs").write_text("namespace Retab;\n", encoding="utf-8")
    assembly = tmp_path / "Retab.dll"
    assembly.write_text("compiled", encoding="utf-8")
    csc = tmp_path / "csc.dll"
    csc.write_text("compiler", encoding="utf-8")
    snippet_files = [
        (_snippet("dotnet", "using Retab;\n\nvar value = 1;\n"), tmp_path / "snippet.cs")
    ]
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(lint_snippets, "DOTNET", "dotnet"),
        patch.object(lint_snippets, "DOTNET_SDK_FOR_SNIPPETS", sdk),
        patch.object(lint_snippets, "DOTNET_SNIPPET_COMPILE_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "_dotnet_sdk_for_build", return_value=sdk),
        patch.object(lint_snippets, "_build_dotnet_sdk_for_snippets", return_value=(assembly, None)),
        patch.object(lint_snippets, "_dotnet_csc", return_value=csc),
        patch.object(lint_snippets, "_dotnet_references", return_value=[assembly]),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_dotnet_batch(snippet_files) == []
        assert lint_snippets.lint_dotnet_batch(snippet_files) == []

    assert len(calls) == 1
    assert calls[0][:2] == ["dotnet", str(csc)]


def test_java_fragments_are_checked_as_standalone_snippets() -> None:
    snippet = _snippet(
        "java",
        'var extraction = client.extractions().get("extraction_abc123");\n',
        title="Java",
    )

    assert lint_snippets.is_self_contained_java(snippet)
    assert not lint_snippets.is_contextual_sdk_snippet(snippet)


def test_java_snippet_is_wrapped_in_class() -> None:
    code = "import com.retab.RetabClient;\n\nRetabClient client = new RetabClient(\"test\");\n"

    wrapped = lint_snippets.normalise_java_snippet(code)

    assert "final class Snippet" in wrapped
    assert "public static void main(String[] args) throws Exception" in wrapped
    assert "RetabClient client = new RetabClient" in wrapped


def test_java_public_final_class_is_demoted_for_generated_filename() -> None:
    code = "public final class Example {\n  public static void main(String[] args) {}\n}\n"

    assert lint_snippets.normalise_java_snippet(code).startswith("final class Example")


def test_java_explicit_class_can_be_renamed_for_batch_compile() -> None:
    code = (
        "public final class Example {\n"
        "  public Example() {}\n"
        "  public static void main(String[] args) {\n"
        "    System.out.println(Example.class.getName());\n"
        "  }\n"
        "}\n"
    )

    normalised = lint_snippets.normalise_java_snippet(
        code,
        wrapper_class_name="Snippet_0001",
        rename_explicit_class=True,
    )

    assert normalised.startswith("final class Snippet_0001")
    assert "Example" not in normalised
    assert "public Snippet_0001()" in normalised


def test_java_classpath_reuses_fingerprinted_sdk_cache(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "src" / "main" / "java" / "com" / "retab").mkdir(parents=True)
    (sdk / "pom.xml").write_text("<project />\n", encoding="utf-8")
    (sdk / "src" / "main" / "java" / "com" / "retab" / "RetabClient.java").write_text(
        "package com.retab;\npublic final class RetabClient {}\n",
        encoding="utf-8",
    )

    with (
        patch.object(lint_snippets, "JAVA_SNIPPET_SDK_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "MAVEN", "mvn"),
    ):
        fingerprint = lint_snippets._java_sdk_fingerprint(sdk)
        cache_dir = tmp_path / "cache" / fingerprint
        (cache_dir / "classes" / "com" / "retab").mkdir(parents=True)
        (cache_dir / "classpath.txt").write_text("deps.jar\n", encoding="utf-8")

        with patch.object(lint_snippets.subprocess, "run") as run:
            classpath = lint_snippets._java_classpath(sdk)

    assert classpath == (
        str(cache_dir / "classes") + lint_snippets.os.pathsep + "deps.jar"
    )
    run.assert_not_called()


def test_java_classpath_prefers_direct_javac_sdk_compile(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "src" / "main" / "java" / "com" / "retab").mkdir(parents=True)
    (sdk / "pom.xml").write_text("<project />\n", encoding="utf-8")
    (sdk / "src" / "main" / "java" / "com" / "retab" / "RetabClient.java").write_text(
        "package com.retab;\npublic final class RetabClient {}\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        commands.append(command)
        output_dir = Path(command[command.index("-d") + 1])
        (output_dir / "com" / "retab").mkdir(parents=True)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    with (
        patch.object(lint_snippets, "JAVA_SNIPPET_SDK_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "JAVAC", "javac"),
        patch.object(lint_snippets, "MAVEN", "mvn"),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        classpath = lint_snippets._java_classpath(sdk)

    assert classpath is not None
    assert len(commands) == 1
    assert commands[0][0] == "javac"


def test_java_wrapper_snippets_are_compiled_in_one_javac_invocation(tmp_path: Path) -> None:
    snippets = [
        (
            _snippet("java", f"System.out.println({index});\n"),
            tmp_path / f"snippet_{index}.java",
        )
        for index in range(2)
    ]
    javac_calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        if command and command[0] == "javac":
            javac_calls.append(command)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    with (
        patch.object(lint_snippets, "SNIPPET_DIR", tmp_path),
        patch.object(lint_snippets, "JAVAC", "javac"),
        patch.object(lint_snippets, "JAVA_SNIPPET_COMPILE_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "_javac_is_usable", return_value=True),
        patch.object(lint_snippets, "_java_sdk_for_build", return_value=tmp_path / "sdk"),
        patch.object(lint_snippets, "_java_classpath", return_value="classes"),
        patch.object(lint_snippets, "_sdk_root_issue", return_value=None),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_java_batch(snippets) == []

    assert len(javac_calls) == 1
    assert snippets[0][1].read_text(encoding="utf-8") != snippets[1][1].read_text(
        encoding="utf-8"
    )


def test_java_explicit_class_snippets_are_compiled_in_one_javac_invocation(
    tmp_path: Path,
) -> None:
    snippets = [
        (
            _snippet(
                "java",
                "public final class Example {\n"
                "  public static void main(String[] args) {}\n"
                "}\n",
            ),
            tmp_path / f"snippet_{index}.java",
        )
        for index in range(2)
    ]
    javac_calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        if command and command[0] == "javac":
            javac_calls.append(command)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    with (
        patch.object(lint_snippets, "SNIPPET_DIR", tmp_path),
        patch.object(lint_snippets, "JAVAC", "javac"),
        patch.object(lint_snippets, "JAVA_SNIPPET_COMPILE_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "_javac_is_usable", return_value=True),
        patch.object(lint_snippets, "_java_sdk_for_build", return_value=tmp_path / "sdk"),
        patch.object(lint_snippets, "_java_classpath", return_value="classes"),
        patch.object(lint_snippets, "_sdk_root_issue", return_value=None),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_java_batch(snippets) == []

    assert len(javac_calls) == 1
    assert "class Snippet_0000" in snippets[0][1].read_text(encoding="utf-8")
    assert "class Snippet_0001" in snippets[1][1].read_text(encoding="utf-8")


def test_java_batch_reuses_successful_compile_cache(tmp_path: Path) -> None:
    snippets = [
        (
            _snippet("java", "System.out.println(1);\n"),
            tmp_path / "snippet.java",
        )
    ]
    javac_calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        if command and command[0] == "javac":
            javac_calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(lint_snippets, "SNIPPET_DIR", tmp_path / "snippets"),
        patch.object(lint_snippets, "JAVAC", "javac"),
        patch.object(lint_snippets, "JAVA_SNIPPET_COMPILE_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "_javac_is_usable", return_value=True),
        patch.object(lint_snippets, "_java_sdk_for_build", return_value=tmp_path / "sdk"),
        patch.object(lint_snippets, "_java_classpath", return_value="classes"),
        patch.object(lint_snippets, "_sdk_root_issue", return_value=None),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_java_batch(snippets) == []
        assert lint_snippets.lint_java_batch(snippets) == []

    assert len(javac_calls) == 1


def test_php_snippet_gets_open_tag_for_syntax_lint() -> None:
    assert lint_snippets.normalise_php_snippet("$client = new Client();\n").startswith(
        "<?php\n"
    )


def test_php_and_ruby_batch_helpers_run_every_snippet(tmp_path: Path) -> None:
    snippet_files = [
        (_snippet("php", "$client = new Client();\n"), tmp_path / "a.php"),
        (_snippet("php", "$client = new Client();\n"), tmp_path / "b.php"),
    ]
    seen: list[Path] = []

    def lint_one(_: lint_snippets.Snippet, file_path: Path) -> list[lint_snippets.LintIssue]:
        seen.append(file_path)
        return []

    assert lint_snippets._run_parallel_snippet_lints(snippet_files, lint_one) == []

    assert sorted(seen) == sorted(file_path for _, file_path in snippet_files)


def test_php_cached_batch_reuses_successful_result(tmp_path: Path) -> None:
    php_sdk = tmp_path / "php-sdk"
    (php_sdk / "lib").mkdir(parents=True)
    (php_sdk / "composer.json").write_text("{}\n", encoding="utf-8")
    (php_sdk / "composer.lock").write_text("{}\n", encoding="utf-8")
    (php_sdk / "lib" / "Client.php").write_text("<?php\n", encoding="utf-8")
    snippet_files = [
        (_snippet("php", "use Retab\\Client;\n$client = new Client();\n"), tmp_path / "a.php"),
    ]
    calls = 0

    def fake_batch(_: list[tuple[lint_snippets.Snippet, Path]]) -> list[lint_snippets.LintIssue]:
        nonlocal calls
        calls += 1
        return []

    with (
        patch.object(lint_snippets, "PHP_SDK_FOR_SNIPPETS", php_sdk),
        patch.object(lint_snippets, "PHP_SNIPPET_SUCCESS_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "lint_php_batch", side_effect=fake_batch),
    ):
        assert lint_snippets.lint_php_cached_batch(snippet_files) == []
        assert lint_snippets.lint_php_cached_batch(snippet_files) == []

    assert calls == 1


def test_python_batch_runs_ruff_once_for_all_syntax_valid_snippets(tmp_path: Path) -> None:
    snippet_files = [
        (_snippet("python", "from retab import Retab\n"), tmp_path / "a.py"),
        (_snippet("python", "from retab import Retab\n"), tmp_path / "b.py"),
    ]
    for snippet, file_path in snippet_files:
        file_path.write_text(snippet.code, encoding="utf-8")
    ruff = tmp_path / "ruff"
    ruff.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    with (
        patch.object(lint_snippets, "RUFF", ruff),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_python_batch(snippet_files) == []

    assert len(calls) == 1
    assert str(snippet_files[0][1]) in calls[0]
    assert str(snippet_files[1][1]) in calls[0]


def test_python_cached_batch_reuses_successful_result(tmp_path: Path) -> None:
    python_sdk = tmp_path / "python-sdk"
    (python_sdk / "retab").mkdir(parents=True)
    (python_sdk / "retab" / "__init__.py").write_text("class Retab: ...\n", encoding="utf-8")
    snippet_files = [
        (_snippet("python", "from retab import Retab\nRetab()\n"), tmp_path / "snippet.py")
    ]
    calls = {"ruff": 0, "pyright": 0}

    def fake_ruff(_: list[tuple[lint_snippets.Snippet, Path]]) -> list[lint_snippets.LintIssue]:
        calls["ruff"] += 1
        return []

    def fake_pyright(_: list[tuple[lint_snippets.Snippet, Path]]) -> list[lint_snippets.LintIssue]:
        calls["pyright"] += 1
        return []

    with (
        patch.object(lint_snippets, "PY_SDK_FOR_SNIPPETS", python_sdk),
        patch.object(lint_snippets, "PYTHON_SNIPPET_SUCCESS_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "lint_python_batch", side_effect=fake_ruff),
        patch.object(lint_snippets, "lint_python_batch_pyright", side_effect=fake_pyright),
    ):
        assert lint_snippets.lint_python_cached_batch(snippet_files, run_pyright=True) == []
        assert lint_snippets.lint_python_cached_batch(snippet_files, run_pyright=True) == []

    assert calls == {"ruff": 1, "pyright": 1}


def test_ruby_batch_checks_all_snippets_in_one_process(tmp_path: Path) -> None:
    snippet_files = [
        (_snippet("ruby", "require 'retab'\n"), tmp_path / "a.rb"),
        (_snippet("ruby", "require 'retab'\n"), tmp_path / "b.rb"),
    ]
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(lint_snippets, "RUBY", "ruby"),
        patch.object(lint_snippets, "_sdk_root_issue", return_value=None),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_ruby_batch(snippet_files) == []

    assert len(calls) == 1
    assert str(snippet_files[0][1]) in calls[0]
    assert str(snippet_files[1][1]) in calls[0]
    assert snippet_files[0][1].read_text(encoding="utf-8") == "require 'retab'\n"


def test_typescript_workspace_preserves_unchanged_snippet_files(tmp_path: Path) -> None:
    snippet_files = [
        (
            _snippet("typescript", 'import { Retab } from "@retab/node";\n'),
            tmp_path / "snippet.mts",
        )
    ]
    node_sdk = _fake_node_sdk(tmp_path)
    node_deps = _fake_node_deps(tmp_path)

    with (
        patch.object(lint_snippets, "NODE_SDK_FOR_SNIPPETS", node_sdk),
        patch.object(lint_snippets, "NODE_SDK", node_deps),
        patch.object(lint_snippets, "TS_SNIPPET_WORKSPACE_CACHE_DIR", tmp_path / "ts-cache"),
    ):
        workspace = lint_snippets._prepare_ts_workspace(snippet_files)
        snippet_path = workspace / "snippets" / "snippet.mts"
        old_time = 1_700_000_000
        os.utime(snippet_path, (old_time, old_time))

        same_workspace = lint_snippets._prepare_ts_workspace(snippet_files)

    assert same_workspace == workspace
    assert int(snippet_path.stat().st_mtime) == old_time
    assert (tmp_path / "ts-cache" / "node-sdk-cache").exists()
    assert (workspace / "node-sdk").is_symlink()


def test_rust_expression_snippet_is_wrapped_in_async_function() -> None:
    code = "use retab::Retab;\n\nlet client = Retab::new(\"test\");\n"

    wrapped = lint_snippets.normalise_rust_snippet(code)

    assert "async fn snippet()" in wrapped
    assert "let client = Retab::new" in wrapped


def test_rust_workspace_checks_snippets_as_one_binary(tmp_path: Path) -> None:
    snippets = [
        (
            _snippet("rust", f"let value = {index};\n"),
            tmp_path / f"snippet-{index}.rs",
        )
        for index in range(2)
    ]

    cache_dir = tmp_path / "cache"
    rust_sdk = _fake_rust_sdk(tmp_path)
    with (
        patch.object(lint_snippets, "RUST_SDK_FOR_SNIPPETS", rust_sdk),
        patch.object(lint_snippets, "RUST_SNIPPET_TARGET_DIR", None),
        patch.object(lint_snippets, "RUST_SNIPPET_WORKSPACE_CACHE_DIR", cache_dir),
        patch.object(lint_snippets, "RUST_SNIPPET_SDK_CACHE_DIR", tmp_path / "sdk-cache"),
    ):
        workspace = lint_snippets._prepare_rust_workspace(snippets)

    main_rs = (workspace / "src" / "main.rs").read_text(encoding="utf-8")

    assert workspace.parent == cache_dir
    assert "#![allow(dead_code, unused_assignments, unused_variables)]" in main_rs
    assert 'path = "snippets/snippet-0.rs"' in main_rs
    assert 'path = "snippets/snippet-1.rs"' in main_rs
    assert not (workspace / "src" / "bin").exists()
    assert (workspace / "src" / "snippets" / "snippet-0.rs").exists()
    assert (workspace / "src" / "snippets" / "snippet-1.rs").exists()


def test_rust_workspace_cache_key_changes_with_snippet_content(tmp_path: Path) -> None:
    first = [(_snippet("rust", "let value = 1;\n"), tmp_path / "snippet.rs")]
    second = [(_snippet("rust", "let value = 2;\n"), tmp_path / "snippet.rs")]
    rust_sdk = _fake_rust_sdk(tmp_path)

    with patch.object(lint_snippets, "RUST_SDK_FOR_SNIPPETS", rust_sdk):
        assert lint_snippets._rust_workspace_cache_key(first) != lint_snippets._rust_workspace_cache_key(second)


def test_rust_workspace_uses_content_addressed_sdk_cache(tmp_path: Path) -> None:
    snippets = [(_snippet("rust", "use retab::Retab;\n"), tmp_path / "snippet.rs")]
    rust_sdk = _fake_rust_sdk(tmp_path)

    with (
        patch.object(lint_snippets, "RUST_SDK_FOR_SNIPPETS", rust_sdk),
        patch.object(lint_snippets, "RUST_SNIPPET_TARGET_DIR", None),
        patch.object(lint_snippets, "RUST_SNIPPET_WORKSPACE_CACHE_DIR", tmp_path / "workspace-cache"),
        patch.object(lint_snippets, "RUST_SNIPPET_SDK_CACHE_DIR", tmp_path / "sdk-cache"),
    ):
        workspace = lint_snippets._prepare_rust_workspace(snippets)

    cargo_toml = (workspace / "Cargo.toml").read_text(encoding="utf-8")
    assert str(tmp_path / "sdk-cache") in cargo_toml
    assert list((tmp_path / "sdk-cache").glob("*"))


def test_rust_sdk_cache_cleans_stale_readonly_tmp_dir(tmp_path: Path) -> None:
    rust_sdk = _fake_rust_sdk(tmp_path)
    sdk_cache = tmp_path / "sdk-cache"
    fingerprint = lint_snippets._rust_sdk_fingerprint(rust_sdk)
    stale_tmp = sdk_cache / f".{fingerprint}.stale.tmp"
    stale_tmp.mkdir(parents=True)
    (stale_tmp / "stale.txt").write_text("stale", encoding="utf-8")
    stale_tmp.chmod(0o555)

    try:
        with (
            patch.object(lint_snippets, "RUST_SDK_FOR_SNIPPETS", rust_sdk),
            patch.object(lint_snippets, "RUST_SNIPPET_SDK_CACHE_DIR", sdk_cache),
        ):
            cached_sdk = lint_snippets._rust_sdk_for_snippets()
    finally:
        if stale_tmp.exists():
            stale_tmp.chmod(0o755)

    assert not stale_tmp.exists()
    assert (cached_sdk / "Cargo.toml").exists()


def test_rust_batch_uses_stable_target_dir(tmp_path: Path) -> None:
    snippets = [
        (
            _snippet("rust", "use retab::Retab;\n\nlet _client = Retab::new(\"test\");\n"),
            tmp_path / "snippet.rs",
        )
    ]
    cargo_calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        if command and command[0] == "cargo":
            cargo_calls.append(command)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    with (
        patch.object(lint_snippets, "SNIPPET_DIR", tmp_path / "snippets"),
        patch.object(lint_snippets, "RUST_SDK_FOR_SNIPPETS", _fake_rust_sdk(tmp_path)),
        patch.object(lint_snippets, "RUST_SNIPPET_SDK_CACHE_DIR", tmp_path / "sdk-cache"),
        patch.object(lint_snippets, "CARGO", "cargo"),
        patch.object(lint_snippets, "RUST_SNIPPET_TARGET_DIR", tmp_path / "target"),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_rust_batch(snippets) == []

    assert cargo_calls == [
        [
            "cargo",
            "check",
            "--message-format",
            "short",
            "--target-dir",
            str(tmp_path / "target"),
        ]
    ]


def test_rust_batch_uses_content_cached_workspace_by_default(tmp_path: Path) -> None:
    snippets = [
        (
            _snippet("rust", "use retab::Retab;\n\nlet _client = Retab::new(\"test\");\n"),
            tmp_path / "snippet.rs",
        )
    ]
    cargo_calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        if command and command[0] == "cargo":
            cargo_calls.append((command, Path(str(kwargs["cwd"]))))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    with (
        patch.object(lint_snippets, "RUST_SDK_FOR_SNIPPETS", _fake_rust_sdk(tmp_path)),
        patch.object(lint_snippets, "RUST_SNIPPET_TARGET_DIR", None),
        patch.object(lint_snippets, "RUST_SNIPPET_WORKSPACE_CACHE_DIR", tmp_path / "cache"),
        patch.object(lint_snippets, "RUST_SNIPPET_SDK_CACHE_DIR", tmp_path / "sdk-cache"),
        patch.object(lint_snippets, "CARGO", "cargo"),
        patch.object(lint_snippets.subprocess, "run", side_effect=fake_run),
    ):
        assert lint_snippets.lint_rust_batch(snippets) == []

    assert cargo_calls
    assert cargo_calls[0][0] == ["cargo", "check", "--message-format", "short"]
    assert cargo_calls[0][1].parent == tmp_path / "cache"
    assert (cargo_calls[0][1] / "src" / "snippets" / "snippet.rs").exists()


def test_workflow_spec_dotnet_examples_pass_required_yaml_definition() -> None:
    for relative_path, option_name in (
        ("api-reference/workflows/spec/validate.mdx", "WorkflowSpecValidateOptions"),
        ("api-reference/workflows/spec/plan.mdx", "WorkflowSpecPlanOptions"),
        ("api-reference/workflows/spec/plan-to.mdx", "WorkflowsCreatePlanOptions"),
        ("api-reference/workflows/spec/apply.mdx", "WorkflowSpecApplyOptions"),
        (
            "api-reference/workflows/spec/apply-to.mdx",
            "WorkflowSpecApplyToWorkflowOptions",
        ),
    ):
        text = (lint_snippets.DOCS_ROOT / relative_path).read_text()

        assert f"new {option_name}())" not in text
        assert f"new {option_name} {{ YamlDefinition = yamlDefinition }}" in text
        assert "client.WorkflowSpecs." not in text


def test_workflow_spec_export_php_example_uses_nested_workflows_service() -> None:
    text = (lint_snippets.DOCS_ROOT / "api-reference/workflows/spec.mdx").read_text()

    assert "$client->workflowSpec()->get(" not in text
    assert "$client->workflows()->spec()->get('wf_abc123')" in text


def test_workflow_yaml_examples_include_metadata_id() -> None:
    for relative_path in (
        "api-reference/workflows/spec/validate.mdx",
        "api-reference/workflows/spec/plan.mdx",
        "api-reference/workflows/spec/apply.mdx",
        "workflows/Workflows.mdx",
    ):
        text = (lint_snippets.DOCS_ROOT / relative_path).read_text()

        assert '"name: invoice workflow\\n"' not in text
        assert "metadata:\n  id:" in text


def test_structural_only_skips_language_toolchains() -> None:
    with patch.object(sys, "argv", ["lint_snippets.py", "--structural-only"]):
        assert lint_snippets.main() == 0
