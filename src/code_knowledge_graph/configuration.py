from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SCHEMA_VERSION = "1.0.0"
EXTRACTOR_VERSION = "1.0.0"
TARGET_ENVIRONMENT_VARIABLE = "CODE_GRAPH_TARGET"

GraphMode = Literal["dev", "public", "demo"]


@dataclass(frozen=True)
class RetrievalConfig:
    seed_count: int = 6
    hops: int = 2
    related_count: int = 25
    hop_decay: float = 0.85

    def __post_init__(self) -> None:
        if self.seed_count < 1 or self.hops < 1 or self.related_count < 1:
            raise ValueError("seed_count, hops, and related_count must all be positive")
        if not 0.0 < self.hop_decay <= 1.0:
            raise ValueError("hop_decay must be greater than 0.0 and at most 1.0")


@dataclass(frozen=True)
class GraphConfig:
    repository_root: Path
    mode: GraphMode = "dev"
    expected_commit: str | None = None
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    schema_version: str = SCHEMA_VERSION
    extractor_version: str = EXTRACTOR_VERSION
    cochange_minimum_support: int = 2
    cochange_maximum_files_per_commit: int = 20

    def __post_init__(self) -> None:
        root = self.repository_root.expanduser().resolve()
        object.__setattr__(self, "repository_root", root)
        if self.mode not in {"dev", "public", "demo"}:
            raise ValueError(f"Unsupported graph mode: {self.mode}")
        if self.expected_commit is not None and not re.fullmatch(
            r"[0-9a-fA-F]{40}", self.expected_commit
        ):
            raise ValueError("expected_commit must be a full 40-character Git SHA")
        if self.expected_commit is not None:
            object.__setattr__(self, "expected_commit", self.expected_commit.lower())
        if self.is_public and self.expected_commit is None:
            raise ValueError(f"{self.mode} mode requires expected_commit")
        if self.cochange_minimum_support < 1:
            raise ValueError("cochange_minimum_support must be positive")
        if self.cochange_maximum_files_per_commit < 2:
            raise ValueError("cochange_maximum_files_per_commit must be at least 2")

    @property
    def is_public(self) -> bool:
        return self.mode in {"public", "demo"}

    @classmethod
    def resolve(
        cls,
        repository_root: str | Path | None = None,
        *,
        notebook_path: Path | None = None,
        environment: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> GraphConfig:
        root = resolve_repository_root(
            repository_root,
            notebook_path=notebook_path,
            environment=environment,
        )
        return cls(repository_root=root, **kwargs)  # type: ignore[arg-type]


def resolve_repository_root(
    repository_root: str | Path | None = None,
    *,
    notebook_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve an explicit target, then the environment, then a notebook sibling."""
    if repository_root is not None:
        candidate = Path(repository_root).expanduser().resolve()
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"Target repository doesn't exist: {candidate}")

    values = os.environ if environment is None else environment
    configured = values.get(TARGET_ENVIRONMENT_VARIABLE)
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(
            f"{TARGET_ENVIRONMENT_VARIABLE} points to a missing directory: {candidate}"
        )

    anchor = (notebook_path or Path.cwd() / "knowledge_code_graph.ipynb").resolve()
    sibling = anchor.parent.parent / "implicit-decision-gate"
    if sibling.is_dir():
        return sibling
    raise FileNotFoundError(
        "No target repository was provided. Pass repository_root, set "
        f"{TARGET_ENVIRONMENT_VARIABLE}, or place implicit-decision-gate beside the notebook repo."
    )
