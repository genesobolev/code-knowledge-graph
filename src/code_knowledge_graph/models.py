from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import pandas as pd
from sklearn.pipeline import FeatureUnion  # type: ignore[import-untyped]

from .configuration import RetrievalConfig


@dataclass(frozen=True)
class CodeNode:
    id: str
    kind: str
    name: str
    qualified_name: str
    path: str
    module: str
    language: str
    start_line: int
    end_line: int
    signature: str = ""
    docstring: str = ""
    source: str = ""

    @property
    def location(self) -> str:
        if self.start_line <= 0:
            return self.path
        return f"{self.path}:{self.start_line}"


@dataclass(frozen=True)
class CodeEdge:
    source_id: str
    target_id: str
    kind: str
    count: int
    confidence: float
    strength: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ParseIssue:
    path: str
    message: str


@dataclass(frozen=True)
class RepositorySnapshot:
    repository_name: str
    remote_url: str | None
    commit_sha: str
    tree_sha: str
    branch: str | None
    detached: bool
    dirty: bool
    shallow: bool
    indexed_source_sha256: str
    schema_version: str
    extractor_version: str
    retrieval: RetrievalConfig


@dataclass(frozen=True)
class KnowledgeGraph:
    snapshot: RepositorySnapshot
    nodes: Mapping[str, CodeNode]
    edges: tuple[CodeEdge, ...]
    graph: nx.MultiDiGraph[str] = field(repr=False, compare=False)
    issues: tuple[ParseIssue, ...] = ()
    resolution_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchIndex:
    node_ids: tuple[str, ...]
    vectorizer: FeatureUnion
    matrix: Any


@dataclass(frozen=True)
class QueryResult:
    query: str
    anchors: tuple[str, ...]
    relevant: pd.DataFrame
    related: pd.DataFrame
    direct_scores: Mapping[str, float]
    relationship_scores: Mapping[str, float]
    pagerank_scores: Mapping[str, float]
    paths: Mapping[str, tuple[str, ...]]


Node = CodeNode
Edge = CodeEdge
