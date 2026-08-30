from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import git

from code_knowledge_graph import (
    GraphConfig,
    GraphMode,
    RepositoryStateError,
    build_knowledge_graph,
    create_repository_snapshot,
    discover_source_files,
    resolve_repository_root,
    sanitize_remote_url,
    verify_repository_snapshot,
)


def test_snapshot_records_reproducible_git_and_source_identity(git_repository: Path) -> None:
    commit = git(git_repository, "rev-parse", "HEAD")
    graph = build_knowledge_graph(
        GraphConfig(git_repository, mode="public", expected_commit=commit)
    )
    snapshot = graph.snapshot

    assert snapshot.repository_name == "sample"
    assert snapshot.remote_url == "https://example.test/acme/sample.git"
    assert snapshot.commit_sha == commit
    assert snapshot.tree_sha == git(git_repository, "rev-parse", "HEAD^{tree}")
    assert snapshot.branch == "main"
    assert not snapshot.detached
    assert not snapshot.dirty
    assert len(snapshot.indexed_source_sha256) == 64


def test_detached_and_dirty_states_are_explicit(git_repository: Path) -> None:
    git(git_repository, "checkout", "--detach")
    clean = build_knowledge_graph(GraphConfig(git_repository))
    assert clean.snapshot.detached
    assert clean.snapshot.branch is None

    (git_repository / "src/sample/service.py").write_text("dirty = True\n", encoding="utf-8")
    dirty = build_knowledge_graph(GraphConfig(git_repository))
    assert dirty.snapshot.dirty


def test_source_discovery_rejects_symbolic_links(git_repository: Path, tmp_path: Path) -> None:
    external_source = tmp_path / "external.py"
    external_source.write_text("external = True\n", encoding="utf-8")
    linked_source = git_repository / "src/sample/external.py"
    linked_source.symlink_to(external_source)

    with pytest.raises(RepositoryStateError, match="symbolic link"):
        discover_source_files(git_repository)

    git(git_repository, "add", "src/sample/external.py")
    git(git_repository, "commit", "-m", "Add linked source")
    commit = git(git_repository, "rev-parse", "HEAD")
    with pytest.raises(RepositoryStateError, match="symbolic link"):
        build_knowledge_graph(GraphConfig(git_repository, mode="public", expected_commit=commit))


def test_development_discovery_skips_deleted_tracked_sources(git_repository: Path) -> None:
    deleted = git_repository / "src/sample/service.py"
    deleted.unlink()

    source_files = discover_source_files(git_repository)
    graph = build_knowledge_graph(GraphConfig(git_repository))

    assert deleted not in source_files
    assert graph.snapshot.dirty is True


@pytest.mark.parametrize("mode", ["public", "demo"])
def test_public_modes_reject_shallow_history(
    git_repository: Path, tmp_path: Path, mode: GraphMode
) -> None:
    shallow_repository = tmp_path / f"shallow-{mode}"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            git_repository.as_uri(),
            str(shallow_repository),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    commit = git(shallow_repository, "rev-parse", "HEAD")

    development = build_knowledge_graph(GraphConfig(shallow_repository))
    assert development.snapshot.shallow is True
    with pytest.raises(RepositoryStateError, match="complete Git history"):
        build_knowledge_graph(GraphConfig(shallow_repository, mode=mode, expected_commit=commit))


def test_public_modes_require_matching_commit_and_clean_tree(git_repository: Path) -> None:
    with pytest.raises(ValueError, match="requires expected_commit"):
        GraphConfig(git_repository, mode="public")

    with pytest.raises(RepositoryStateError, match="Expected commit"):
        build_knowledge_graph(GraphConfig(git_repository, mode="demo", expected_commit="0" * 40))

    commit = git(git_repository, "rev-parse", "HEAD")
    (git_repository / "untracked.py").write_text("dirty = True\n", encoding="utf-8")
    with pytest.raises(RepositoryStateError, match="require a clean repository"):
        build_knowledge_graph(GraphConfig(git_repository, mode="public", expected_commit=commit))


def test_snapshot_recheck_detects_source_changes(git_repository: Path) -> None:
    config = GraphConfig(git_repository)
    source_files = discover_source_files(git_repository)
    snapshot = create_repository_snapshot(config, source_files)
    path = git_repository / "src/sample/service.py"
    path.write_text(path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

    with pytest.raises(RepositoryStateError, match="changed while"):
        verify_repository_snapshot(config, source_files, snapshot)


def test_build_rechecks_repository_after_extraction(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_knowledge_graph import extraction

    original = extraction.parse_repository

    def parse_then_change(
        repository: Path, source_files: tuple[Path, ...] | None = None
    ) -> extraction.ExtractionState:
        state = original(repository, source_files)
        path = repository / "src/sample/service.py"
        path.write_text(path.read_text(encoding="utf-8") + "# concurrent change\n")
        return state

    monkeypatch.setattr(extraction, "parse_repository", parse_then_change)
    with pytest.raises(RepositoryStateError, match="changed while"):
        build_knowledge_graph(GraphConfig(git_repository))


def test_target_resolution_uses_environment_then_notebook_sibling(tmp_path: Path) -> None:
    environment_target = tmp_path / "configured"
    environment_target.mkdir()
    sibling = tmp_path / "implicit-decision-gate"
    sibling.mkdir()
    notebook = tmp_path / "code-knowledge-graph" / "knowledge_code_graph.ipynb"

    assert (
        resolve_repository_root(
            notebook_path=notebook,
            environment={"CODE_GRAPH_TARGET": str(environment_target)},
        )
        == environment_target
    )
    assert resolve_repository_root(notebook_path=notebook, environment={}) == sibling


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        (
            "https://user:secret@example.test/org/repo.git?token=x#part",
            "https://example.test/org/repo.git",
        ),
        ("git@example.test:org/repo.git", "ssh://example.test/org/repo.git"),
        ("/home/person/private/repo", None),
        ("file:///home/person/private/repo", None),
    ],
)
def test_remote_sanitization(remote: str, expected: str | None) -> None:
    assert sanitize_remote_url(remote) == expected
