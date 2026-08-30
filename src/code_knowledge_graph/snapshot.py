from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .configuration import GraphConfig
from .models import RepositorySnapshot


class RepositoryStateError(ValueError):
    """Raised when a repository can't produce the requested artifact mode."""


def _git(repository: Path, *arguments: str, required: bool = True) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return completed.stdout.strip()
    if not required:
        return None
    detail = completed.stderr.strip() or completed.stdout.strip() or "unknown Git error"
    raise RepositoryStateError(f"Git command failed ({' '.join(arguments)}): {detail}")


def sanitize_remote_url(value: str | None) -> str | None:
    """Remove credentials, queries, fragments, and local absolute paths from a remote."""
    if not value:
        return None
    remote = value.strip()
    if remote.startswith("/") or remote.startswith("file:"):
        return None
    if "://" not in remote and ":" in remote:
        user_host, path = remote.split(":", 1)
        host = user_host.rsplit("@", 1)[-1]
        return f"ssh://{host}/{path.lstrip('/')}"
    if "://" not in remote:
        return None
    parsed = urlsplit(remote)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    sanitized = SplitResult(parsed.scheme, host, parsed.path, "", "")
    return urlunsplit(sanitized)


def _repository_name(repository: Path, remote_url: str | None) -> str:
    if remote_url:
        remote_name = PurePosixPath(urlsplit(remote_url).path).name
        if remote_name.endswith(".git"):
            remote_name = remote_name.removesuffix(".git")
        if remote_name:
            return remote_name
    return repository.name


def indexed_source_sha256(repository: Path, source_files: Sequence[Path]) -> str:
    """Hash relative names and bytes in a stable, unambiguous order."""
    digest = hashlib.sha256()
    digest.update(b"code-knowledge-graph:indexed-sources:v1\0")
    relative_files = sorted(
        ((path.relative_to(repository).as_posix(), path) for path in source_files),
        key=lambda item: item[0],
    )
    for relative, path in relative_files:
        validate_source_path(repository, path)
        name = relative.encode("utf-8", errors="surrogateescape")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def validate_source_path(repository: Path, path: Path) -> Path:
    """Reject source paths that escape the repository or traverse symbolic links."""

    try:
        relative = path.relative_to(repository)
    except ValueError as exc:
        raise RepositoryStateError(f"Source path is outside the repository: {path}") from exc
    if relative.is_absolute() or ".." in relative.parts:
        raise RepositoryStateError(f"Source path is outside the repository: {path}")

    current = repository
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RepositoryStateError(
                f"Refusing source path through a symbolic link: {relative.as_posix()}"
            )
    return relative


def create_repository_snapshot(
    config: GraphConfig,
    source_files: Sequence[Path],
) -> RepositorySnapshot:
    repository = config.repository_root
    if not repository.is_dir():
        raise FileNotFoundError(f"Target repository doesn't exist: {repository}")

    commit_sha = _git(repository, "rev-parse", "--verify", "HEAD")
    tree_sha = _git(repository, "rev-parse", "--verify", "HEAD^{tree}")
    assert commit_sha is not None
    assert tree_sha is not None
    branch = _git(repository, "symbolic-ref", "--quiet", "--short", "HEAD", required=False)
    status = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    dirty = bool(status)
    shallow_value = _git(repository, "rev-parse", "--is-shallow-repository")
    if shallow_value not in {"true", "false"}:
        raise RepositoryStateError(
            f"Git returned an invalid shallow-repository state: {shallow_value!r}"
        )
    shallow = shallow_value == "true"

    if config.is_public and dirty:
        raise RepositoryStateError(
            f"{config.mode} artifacts require a clean repository at {commit_sha}"
        )
    if config.is_public and shallow:
        raise RepositoryStateError(
            f"{config.mode} artifacts require complete Git history; the repository is shallow"
        )
    if config.expected_commit is not None and commit_sha != config.expected_commit:
        message = f"Expected commit {config.expected_commit}, found {commit_sha}"
        if config.is_public:
            raise RepositoryStateError(message)

    remote = sanitize_remote_url(_git(repository, "remote", "get-url", "origin", required=False))
    return RepositorySnapshot(
        repository_name=_repository_name(repository, remote),
        remote_url=remote,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        branch=branch,
        detached=branch is None,
        dirty=dirty,
        shallow=shallow,
        indexed_source_sha256=indexed_source_sha256(repository, source_files),
        schema_version=config.schema_version,
        extractor_version=config.extractor_version,
        retrieval=config.retrieval,
    )


def verify_repository_snapshot(
    config: GraphConfig,
    source_files: Sequence[Path],
    expected: RepositorySnapshot,
) -> None:
    """Reject repository or indexed-source changes that occurred during extraction."""
    current = create_repository_snapshot(config, source_files)
    compared_fields = (
        "commit_sha",
        "tree_sha",
        "branch",
        "detached",
        "dirty",
        "shallow",
        "indexed_source_sha256",
    )
    changed = [
        name for name in compared_fields if getattr(current, name) != getattr(expected, name)
    ]
    if changed:
        raise RepositoryStateError(
            "Repository changed while the graph was being extracted: " + ", ".join(changed)
        )
