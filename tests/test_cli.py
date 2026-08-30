from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_knowledge_graph.cli import main


def test_cli_indexes_and_exports_machine_readable_context(
    git_repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = tmp_path / "graph.json"
    assert (
        main(
            [
                "index",
                "--repo",
                str(git_repository),
                "--out",
                str(artifact),
            ]
        )
        == 0
    )
    assert artifact.is_file()

    assert (
        main(
            [
                "query",
                "--graph",
                str(artifact),
                "--query",
                "where is execute implemented and tested?",
                "--format",
                "json",
                "--include-source",
                "symbol:src/sample/service.py::execute",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["repository"]["clean"] is True
    assert payload["query"] == "where is execute implemented and tested?"
    assert payload["anchors"]
    assert payload["source_snippets"][0]["node_id"].endswith("::execute")
    assert str(git_repository) not in captured.out


def test_cli_markdown_uses_stdout_and_reports_invalid_public_mode(
    git_repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = tmp_path / "graph.json"
    assert main(["index", "--repo", str(git_repository), "--out", str(artifact)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "query",
                "--graph",
                str(artifact),
                "--query",
                "what tests execute the service?",
                "--format",
                "markdown",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out.startswith("# Code context")
    assert captured.err == ""

    assert (
        main(
            [
                "index",
                "--repo",
                str(git_repository),
                "--out",
                str(tmp_path / "public.json"),
                "--mode",
                "public",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "public mode requires expected_commit" in captured.err


def test_cli_rejects_blank_queries_and_artifact_commit_mismatches(
    git_repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = tmp_path / "graph.json"
    assert main(["index", "--repo", str(git_repository), "--out", str(artifact)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "query",
                "--graph",
                str(artifact),
                "--query",
                "   ",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "--query must not be blank" in captured.err
    assert captured.out == ""

    assert (
        main(
            [
                "query",
                "--graph",
                str(artifact),
                "--expected-commit",
                "f" * 40,
                "--query",
                "where is execute?",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "Expected commit" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    ("snapshot_field", "message"),
    [("dirty", "clean worktree"), ("shallow", "complete Git history")],
)
def test_expected_commit_rejects_non_public_graph_artifacts(
    git_repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    snapshot_field: str,
    message: str,
) -> None:
    artifact = tmp_path / "graph.json"
    assert main(["index", "--repo", str(git_repository), "--out", str(artifact)]) == 0
    capsys.readouterr()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["snapshot"][snapshot_field] = True
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        main(
            [
                "query",
                "--graph",
                str(artifact),
                "--expected-commit",
                payload["snapshot"]["commit_sha"],
                "--query",
                "where is execute?",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert message in captured.err
    assert captured.out == ""
