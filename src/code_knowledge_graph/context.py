"""Versioned query bundles for agents and static presentation exports."""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from .configuration import RetrievalConfig
from .models import CodeEdge, CodeNode, KnowledgeGraph, QueryResult, RepositorySnapshot

QUERY_BUNDLE_SCHEMA_VERSION = 1
MAX_SNIPPETS = 8
MAX_SNIPPET_CHARACTERS = 800


class QueryBundleError(ValueError):
    """Raised when a query bundle is unsafe or internally inconsistent."""


@dataclass(frozen=True)
class BundleRepository:
    name: str
    remote_url: str | None
    commit_sha: str
    tree_sha: str
    branch: str | None
    detached: bool
    clean: bool
    shallow: bool
    indexed_source_sha256: str
    graph_schema_version: str
    extractor_version: str


@dataclass(frozen=True)
class BundleRetrieval:
    seed_count: int
    hops: int
    related_count: int
    hop_decay: float


@dataclass(frozen=True)
class SourceLocation:
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class BundleNode:
    node_id: str
    kind: str
    name: str
    qualified_name: str
    language: str
    location: SourceLocation


@dataclass(frozen=True)
class RankedNode:
    rank: int
    node: BundleNode
    direct_score: float
    relationship_score: float
    pagerank_score: float


@dataclass(frozen=True)
class BundleEdge:
    edge_id: str
    source_id: str
    target_id: str
    kind: str
    direction: Literal["directed", "undirected"]
    count: int
    strength: float
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class PathEdgeContribution:
    edge_id: str
    kind: str
    traversal_direction: Literal["forward", "reverse", "undirected"]
    strength: float


@dataclass(frozen=True)
class PathStep:
    from_node_id: str
    to_node_id: str
    combined_strength: float
    contributions: tuple[PathEdgeContribution, ...]


@dataclass(frozen=True)
class StrongestPath:
    source_id: str
    target_id: str
    relationship_score: float
    node_ids: tuple[str, ...]
    steps: tuple[PathStep, ...]


@dataclass(frozen=True)
class SourceSnippet:
    node_id: str
    location: SourceLocation
    text: str
    reviewed: Literal[True] = True


@dataclass(frozen=True)
class QueryBundle:
    schema_version: int
    repository: BundleRepository
    query: str
    retrieval: BundleRetrieval
    anchors: tuple[RankedNode, ...]
    related_nodes: tuple[RankedNode, ...]
    strongest_paths: tuple[StrongestPath, ...]
    selected_edges: tuple[BundleEdge, ...]
    source_snippets: tuple[SourceSnippet, ...]


def _sanitize_remote_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{parsed.hostname}{port}", parsed.path, "", ""))
    if value.startswith("git@") and ":" in value:
        return value.split("?", 1)[0].split("#", 1)[0]
    return None


def _repository(snapshot: RepositorySnapshot) -> BundleRepository:
    return BundleRepository(
        name=snapshot.repository_name,
        remote_url=_sanitize_remote_url(snapshot.remote_url),
        commit_sha=snapshot.commit_sha,
        tree_sha=snapshot.tree_sha,
        branch=snapshot.branch,
        detached=snapshot.detached,
        clean=not snapshot.dirty,
        shallow=snapshot.shallow,
        indexed_source_sha256=snapshot.indexed_source_sha256,
        graph_schema_version=snapshot.schema_version,
        extractor_version=snapshot.extractor_version,
    )


def _retrieval(config: RetrievalConfig) -> BundleRetrieval:
    return BundleRetrieval(
        seed_count=config.seed_count,
        hops=config.hops,
        related_count=config.related_count,
        hop_decay=config.hop_decay,
    )


def _location(node: CodeNode) -> SourceLocation:
    return SourceLocation(node.path, node.start_line, node.end_line)


def _ranked(node: CodeNode, rank: int, result: QueryResult) -> RankedNode:
    return RankedNode(
        rank=rank,
        node=BundleNode(
            node_id=node.id,
            kind=node.kind,
            name=node.name,
            qualified_name=node.qualified_name,
            language=node.language,
            location=_location(node),
        ),
        direct_score=float(result.direct_scores.get(node.id, 0.0)),
        relationship_score=float(result.relationship_scores.get(node.id, 0.0)),
        pagerank_score=float(result.pagerank_scores.get(node.id, 0.0)),
    )


def _edge_id(edge: CodeEdge) -> str:
    return f"{edge.kind}:{edge.source_id}->{edge.target_id}"


def _path_edges(
    edges: Sequence[CodeEdge], left: str, right: str
) -> tuple[tuple[CodeEdge, Literal["forward", "reverse", "undirected"]], ...]:
    candidates: list[tuple[CodeEdge, Literal["forward", "reverse", "undirected"]]] = []
    for edge in edges:
        if edge.source_id == left and edge.target_id == right:
            direction: Literal["forward", "reverse", "undirected"] = (
                "undirected" if edge.kind == "CO_CHANGES" else "forward"
            )
            candidates.append((edge, direction))
        elif edge.source_id == right and edge.target_id == left:
            direction = "undirected" if edge.kind == "CO_CHANGES" else "reverse"
            candidates.append((edge, direction))
    if not candidates:
        raise QueryBundleError(f"path has no edge between {left} and {right}")
    return tuple(candidates)


def _combined_strength(edges: Sequence[CodeEdge]) -> float:
    combined = 0.0
    for edge in edges:
        combined = 1.0 - (1.0 - combined) * (1.0 - edge.strength)
    return combined


def _truncate_source(text: str, maximum: int) -> str:
    """Truncate source to an exact character budget with a visible marker."""

    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "…"


def build_query_bundle(
    knowledge: KnowledgeGraph,
    result: QueryResult,
    *,
    retrieval: RetrievalConfig | None = None,
    include_source: bool = False,
    reviewed_snippet_node_ids: Sequence[str] = (),
    max_snippets: int = MAX_SNIPPETS,
    max_snippet_characters: int = MAX_SNIPPET_CHARACTERS,
) -> QueryBundle:
    """Build a bundle; snippets require explicit opt-in and reviewed node IDs."""

    if reviewed_snippet_node_ids and not include_source:
        raise QueryBundleError("reviewed snippet IDs require include_source=True")
    if not 0 <= max_snippets <= MAX_SNIPPETS:
        raise QueryBundleError(f"max_snippets must be between 0 and {MAX_SNIPPETS}")
    if not 1 <= max_snippet_characters <= MAX_SNIPPET_CHARACTERS:
        raise QueryBundleError(
            f"max_snippet_characters must be between 1 and {MAX_SNIPPET_CHARACTERS}"
        )
    anchor_ids = tuple(result.anchors)
    if result.related.empty:
        ranked_related_ids: tuple[str, ...] = ()
    elif "node_id" not in result.related.columns:
        raise QueryBundleError("non-empty related results must include a node_id column")
    else:
        ranked_related_ids = tuple(cast(str, value) for value in result.related["node_id"].tolist())
    path_node_ids = tuple(
        node_id
        for target_id in ranked_related_ids
        for node_id in result.paths.get(target_id, ())
        if node_id not in anchor_ids and node_id not in ranked_related_ids
    )
    related_ids = tuple(dict.fromkeys((*ranked_related_ids, *path_node_ids)))
    ranked_ids = set(anchor_ids) | set(related_ids)
    missing = ranked_ids - set(knowledge.nodes)
    if missing:
        raise QueryBundleError(f"query result references missing nodes: {sorted(missing)}")
    anchors = tuple(
        _ranked(knowledge.nodes[node_id], rank, result)
        for rank, node_id in enumerate(anchor_ids, 1)
    )
    related = tuple(
        _ranked(knowledge.nodes[node_id], rank, result)
        for rank, node_id in enumerate(related_ids, 1)
    )

    paths: list[StrongestPath] = []
    selected: dict[str, CodeEdge] = {}
    for target_id in ranked_related_ids:
        node_path = result.paths.get(target_id)
        if not node_path:
            continue
        steps: list[PathStep] = []
        for left, right in itertools.pairwise(node_path):
            path_edges = _path_edges(knowledge.edges, left, right)
            contributions: list[PathEdgeContribution] = []
            for edge, traversal_direction in path_edges:
                edge_id = _edge_id(edge)
                selected[edge_id] = edge
                contributions.append(
                    PathEdgeContribution(
                        edge_id=edge_id,
                        kind=edge.kind,
                        traversal_direction=traversal_direction,
                        strength=edge.strength,
                    )
                )
            steps.append(
                PathStep(
                    from_node_id=left,
                    to_node_id=right,
                    combined_strength=_combined_strength(
                        tuple(edge for edge, _direction in path_edges)
                    ),
                    contributions=tuple(contributions),
                )
            )
        paths.append(
            StrongestPath(
                node_path[0],
                target_id,
                float(result.relationship_scores.get(target_id, 0.0)),
                tuple(node_path),
                tuple(steps),
            )
        )
    edges = tuple(
        BundleEdge(
            edge_id=edge_id,
            source_id=edge.source_id,
            target_id=edge.target_id,
            kind=edge.kind,
            direction="undirected" if edge.kind == "CO_CHANGES" else "directed",
            count=edge.count,
            strength=edge.strength,
            confidence=edge.confidence,
            evidence=edge.evidence,
        )
        for edge_id, edge in sorted(selected.items())
    )

    if len(set(reviewed_snippet_node_ids)) != len(reviewed_snippet_node_ids):
        raise QueryBundleError("reviewed snippet node IDs must be unique")
    if len(reviewed_snippet_node_ids) > max_snippets:
        raise QueryBundleError("source snippet count exceeds the configured bound")
    snippets: list[SourceSnippet] = []
    if include_source:
        for node_id in reviewed_snippet_node_ids:
            if node_id not in ranked_ids:
                raise QueryBundleError(f"snippet node isn't ranked: {node_id}")
            node = knowledge.nodes[node_id]
            text = node.source.strip()
            text = _truncate_source(text, max_snippet_characters)
            snippets.append(SourceSnippet(node_id, _location(node), text))

    bundle = QueryBundle(
        schema_version=QUERY_BUNDLE_SCHEMA_VERSION,
        repository=_repository(knowledge.snapshot),
        query=result.query,
        retrieval=_retrieval(retrieval or knowledge.snapshot.retrieval),
        anchors=anchors,
        related_nodes=related,
        strongest_paths=tuple(paths),
        selected_edges=edges,
        source_snippets=tuple(snippets),
    )
    validate_query_bundle(bundle)
    return bundle


def _validate_path(path: str) -> None:
    value = PurePosixPath(path)
    if not path or value.is_absolute() or ".." in value.parts or "\\" in path:
        raise QueryBundleError(f"location isn't repository-relative POSIX: {path!r}")


def _require_text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise QueryBundleError(f"{name} must be {qualifier}")
    return value


def _require_integer(value: object, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise QueryBundleError(f"{name} must be an integer greater than or equal to {minimum}")
    return value


def _require_number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QueryBundleError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise QueryBundleError(f"{name} must be a finite number")
    if minimum is not None and (number < minimum or (exclusive_minimum and number == minimum)):
        comparator = "greater than" if exclusive_minimum else "greater than or equal to"
        raise QueryBundleError(f"{name} must be {comparator} {minimum}")
    if maximum is not None and number > maximum:
        raise QueryBundleError(f"{name} must be less than or equal to {maximum}")
    return number


def _validate_location(location: SourceLocation, name: str) -> None:
    if not isinstance(location, SourceLocation):
        raise QueryBundleError(f"{name} must be a source location")
    _require_text(location.path, f"{name}.path")
    _validate_path(location.path)
    start_line = _require_integer(location.start_line, f"{name}.start_line")
    end_line = _require_integer(location.end_line, f"{name}.end_line")
    if end_line < start_line:
        raise QueryBundleError(f"{name}.end_line must not precede start_line")


def _validate_repository(repository: BundleRepository) -> None:
    if not isinstance(repository, BundleRepository):
        raise QueryBundleError("repository must be an object")
    _require_text(repository.name, "repository.name")
    if repository.remote_url is not None:
        _require_text(repository.remote_url, "repository.remote_url")
    for name, sha_value in (
        ("commit_sha", repository.commit_sha),
        ("tree_sha", repository.tree_sha),
    ):
        if not isinstance(sha_value, str) or re.fullmatch(r"[0-9a-f]{40}", sha_value) is None:
            raise QueryBundleError(f"repository.{name} must be a full lowercase Git SHA")
    if repository.branch is not None:
        _require_text(repository.branch, "repository.branch")
    for name, boolean_value in (
        ("detached", repository.detached),
        ("clean", repository.clean),
        ("shallow", repository.shallow),
    ):
        if type(boolean_value) is not bool:
            raise QueryBundleError(f"repository.{name} must be a boolean")
    if (
        not isinstance(repository.indexed_source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", repository.indexed_source_sha256) is None
    ):
        raise QueryBundleError("repository.indexed_source_sha256 must be a lowercase SHA-256")
    _require_text(
        repository.graph_schema_version,
        "repository.graph_schema_version",
        allow_empty=True,
    )
    _require_text(
        repository.extractor_version,
        "repository.extractor_version",
        allow_empty=True,
    )


def _validate_ranked_node(item: RankedNode, name: str) -> None:
    _require_integer(item.rank, f"{name}.rank")
    node = item.node
    if not isinstance(node, BundleNode):
        raise QueryBundleError(f"{name}.node must be an object")
    for field_name, value in (
        ("node_id", node.node_id),
        ("kind", node.kind),
        ("name", node.name),
        ("qualified_name", node.qualified_name),
    ):
        _require_text(value, f"{name}.node.{field_name}")
    _require_text(node.language, f"{name}.node.language", allow_empty=True)
    _validate_location(node.location, f"{name}.node.location")
    for field_name, score_value in (
        ("direct_score", item.direct_score),
        ("relationship_score", item.relationship_score),
        ("pagerank_score", item.pagerank_score),
    ):
        _require_number(score_value, f"{name}.{field_name}")


def _validate_schema_contract(bundle: QueryBundle) -> None:
    """Enforce the published v1 schema without requiring a JSON Schema runtime."""

    _validate_repository(bundle.repository)
    retrieval = bundle.retrieval
    if not isinstance(retrieval, BundleRetrieval):
        raise QueryBundleError("retrieval must be an object")
    for name, count_value in (
        ("seed_count", retrieval.seed_count),
        ("hops", retrieval.hops),
        ("related_count", retrieval.related_count),
    ):
        _require_integer(count_value, f"retrieval.{name}")
    _require_number(
        retrieval.hop_decay,
        "retrieval.hop_decay",
        minimum=0.0,
        maximum=1.0,
        exclusive_minimum=True,
    )
    for group_name, group in (
        ("anchors", bundle.anchors),
        ("related_nodes", bundle.related_nodes),
    ):
        for index, item in enumerate(group):
            _validate_ranked_node(item, f"{group_name}[{index}]")
    for index, edge in enumerate(bundle.selected_edges):
        name = f"selected_edges[{index}]"
        for field_name, text_value in (
            ("edge_id", edge.edge_id),
            ("source_id", edge.source_id),
            ("target_id", edge.target_id),
            ("kind", edge.kind),
        ):
            _require_text(text_value, f"{name}.{field_name}", allow_empty=True)
        if edge.direction not in ("directed", "undirected"):
            raise QueryBundleError(f"{name}.direction is invalid")
        _require_integer(edge.count, f"{name}.count")
        _require_number(edge.strength, f"{name}.strength")
        _require_number(edge.confidence, f"{name}.confidence")
        if not all(isinstance(item, str) for item in edge.evidence):
            raise QueryBundleError(f"{name}.evidence must contain only strings")
    for path_index, path in enumerate(bundle.strongest_paths):
        name = f"strongest_paths[{path_index}]"
        _require_text(path.source_id, f"{name}.source_id", allow_empty=True)
        _require_text(path.target_id, f"{name}.target_id", allow_empty=True)
        _require_number(path.relationship_score, f"{name}.relationship_score")
        if not path.node_ids or not all(isinstance(item, str) for item in path.node_ids):
            raise QueryBundleError(f"{name}.node_ids must be a non-empty string array")
        for step_index, step in enumerate(path.steps):
            step_name = f"{name}.steps[{step_index}]"
            _require_text(step.from_node_id, f"{step_name}.from_node_id", allow_empty=True)
            _require_text(step.to_node_id, f"{step_name}.to_node_id", allow_empty=True)
            _require_number(
                step.combined_strength,
                f"{step_name}.combined_strength",
                minimum=0.0,
                maximum=1.0,
            )
            if not step.contributions:
                raise QueryBundleError(f"{step_name}.contributions must not be empty")
            for contribution_index, contribution in enumerate(step.contributions):
                contribution_name = f"{step_name}.contributions[{contribution_index}]"
                _require_text(
                    contribution.edge_id,
                    f"{contribution_name}.edge_id",
                    allow_empty=True,
                )
                _require_text(
                    contribution.kind,
                    f"{contribution_name}.kind",
                    allow_empty=True,
                )
                if contribution.traversal_direction not in (
                    "forward",
                    "reverse",
                    "undirected",
                ):
                    raise QueryBundleError(f"{contribution_name}.traversal_direction is invalid")
                _require_number(
                    contribution.strength,
                    f"{contribution_name}.strength",
                    minimum=0.0,
                    maximum=1.0,
                )
    for index, snippet in enumerate(bundle.source_snippets):
        name = f"source_snippets[{index}]"
        _require_text(snippet.node_id, f"{name}.node_id", allow_empty=True)
        _validate_location(snippet.location, f"{name}.location")
        _require_text(snippet.text, f"{name}.text", allow_empty=True)
        if snippet.reviewed is not True:
            raise QueryBundleError(f"{name}.reviewed must be true")


def validate_query_bundle(bundle: QueryBundle) -> None:
    """Reject incompatible, non-portable, or unresolved bundle content."""

    if (
        type(bundle.schema_version) is not int
        or bundle.schema_version != QUERY_BUNDLE_SCHEMA_VERSION
    ):
        raise QueryBundleError(f"unsupported query bundle schema: {bundle.schema_version}")
    _validate_schema_contract(bundle)
    _require_text(bundle.query, "query")
    if not bundle.query.strip():
        raise QueryBundleError("query must not be blank")
    ranked = (*bundle.anchors, *bundle.related_nodes)
    node_ids = {item.node.node_id for item in ranked}
    if len(node_ids) != len(ranked):
        raise QueryBundleError("ranked nodes must be unique")
    for item in ranked:
        _validate_path(item.node.location.path)
    edges_by_id = {edge.edge_id: edge for edge in bundle.selected_edges}
    if len(edges_by_id) != len(bundle.selected_edges):
        raise QueryBundleError("selected edges must be unique")
    for edge in bundle.selected_edges:
        if edge.source_id not in node_ids or edge.target_id not in node_ids:
            raise QueryBundleError(f"selected edge has an unresolved node: {edge.edge_id}")
    anchors_by_id = {item.node.node_id: item for item in bundle.anchors}
    maximum_anchor_score = (
        max(
            (item.direct_score for item in bundle.anchors),
            default=1.0,
        )
        or 1.0
    )
    referenced_edge_ids: set[str] = set()
    for path in bundle.strongest_paths:
        if not path.node_ids or path.source_id != path.node_ids[0]:
            raise QueryBundleError(f"invalid path source: {path.target_id}")
        if path.target_id != path.node_ids[-1] or len(path.steps) != len(path.node_ids) - 1:
            raise QueryBundleError(f"invalid path target or length: {path.target_id}")
        if any(node_id not in node_ids for node_id in path.node_ids):
            raise QueryBundleError(f"path has an unresolved node: {path.target_id}")
        if path.source_id not in anchors_by_id:
            raise QueryBundleError(f"path source isn't a ranked anchor: {path.source_id}")
        reproduced_score = anchors_by_id[path.source_id].direct_score / maximum_anchor_score
        for step, left, right in zip(
            path.steps, path.node_ids[:-1], path.node_ids[1:], strict=True
        ):
            if (step.from_node_id, step.to_node_id) != (left, right):
                raise QueryBundleError(f"path traversal is inconsistent: {path.target_id}")
            if not step.contributions:
                raise QueryBundleError(f"path step has no edge contributions: {left} -> {right}")
            contribution_ids = {item.edge_id for item in step.contributions}
            if len(contribution_ids) != len(step.contributions):
                raise QueryBundleError(f"path step repeats an edge: {left} -> {right}")
            for contribution in step.contributions:
                selected_edge = edges_by_id.get(contribution.edge_id)
                if selected_edge is None:
                    raise QueryBundleError(
                        f"path has an unresolved edge contribution: {contribution.edge_id}"
                    )
                referenced_edge_ids.add(contribution.edge_id)
                if contribution.kind != selected_edge.kind or not math.isclose(
                    contribution.strength,
                    selected_edge.strength,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                ):
                    raise QueryBundleError(
                        f"path contribution doesn't match selected edge: {contribution.edge_id}"
                    )
                if selected_edge.direction == "undirected":
                    expected_direction = "undirected"
                    endpoints_match = {
                        selected_edge.source_id,
                        selected_edge.target_id,
                    } == {left, right}
                elif (selected_edge.source_id, selected_edge.target_id) == (left, right):
                    expected_direction = "forward"
                    endpoints_match = True
                else:
                    expected_direction = "reverse"
                    endpoints_match = (
                        selected_edge.source_id,
                        selected_edge.target_id,
                    ) == (right, left)
                if not endpoints_match or contribution.traversal_direction != expected_direction:
                    raise QueryBundleError(
                        f"path contribution has an invalid traversal: {contribution.edge_id}"
                    )
            reproduced_step_strength = 1.0 - math.prod(
                1.0 - contribution.strength for contribution in step.contributions
            )
            if not math.isclose(
                step.combined_strength,
                reproduced_step_strength,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise QueryBundleError(
                    f"path step has an invalid combined strength: {left} -> {right}"
                )
            reproduced_score *= bundle.retrieval.hop_decay * step.combined_strength
        if not math.isclose(
            path.relationship_score,
            reproduced_score,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise QueryBundleError(f"path relationship score can't be reproduced: {path.target_id}")
    if referenced_edge_ids != set(edges_by_id):
        raise QueryBundleError("selected edges must exactly match path contributions")
    if len(bundle.source_snippets) > MAX_SNIPPETS:
        raise QueryBundleError("query bundle has too many source snippets")
    for snippet in bundle.source_snippets:
        if snippet.node_id not in node_ids:
            raise QueryBundleError(f"snippet has an unresolved node: {snippet.node_id}")
        _validate_path(snippet.location.path)
        if len(snippet.text) > MAX_SNIPPET_CHARACTERS:
            raise QueryBundleError(f"snippet is too long: {snippet.node_id}")


def query_bundle_to_json(bundle: QueryBundle) -> str:
    validate_query_bundle(bundle)
    return json.dumps(asdict(bundle), indent=2, sort_keys=True) + "\n"


def query_bundle_to_markdown(bundle: QueryBundle) -> str:
    """Render Markdown from the same bundle used for JSON."""

    validate_query_bundle(bundle)
    lines = [
        "# Code context",
        "",
        f"Query: {bundle.query}",
        "",
        f"Repository: `{bundle.repository.name}` at `{bundle.repository.commit_sha}`.",
        "",
        "## Ranked anchors",
        "",
    ]
    for item in bundle.anchors:
        lines.append(
            f"- {item.rank}. `{item.node.qualified_name}` at "
            f"`{item.node.location.path}:{item.node.location.start_line}` "
            f"with direct score {item.direct_score:.4f}"
        )
    lines.extend(["", "## Related nodes", ""])
    for item in bundle.related_nodes:
        lines.append(
            f"- {item.rank}. `{item.node.qualified_name}` at "
            f"`{item.node.location.path}:{item.node.location.start_line}` "
            f"with relationship score {item.relationship_score:.4f}"
        )
    lines.extend(["", "## Strongest paths", ""])
    for path in bundle.strongest_paths:
        parts = [path.node_ids[0]]
        for step in path.steps:
            contributions = ", ".join(
                f"`{item.kind}` {item.traversal_direction} {item.strength:.4f}"
                for item in step.contributions
            )
            parts.append(
                f"-[combined {step.combined_strength:.4f} from {contributions}]-> {step.to_node_id}"
            )
        lines.append(f"- {' '.join(parts)} with relationship score {path.relationship_score:.4f}")
    if bundle.source_snippets:
        lines.extend(["", "## Reviewed source snippets", ""])
        for snippet in bundle.source_snippets:
            lines.extend(
                [
                    f"### {snippet.node_id}",
                    "",
                    f"Location: `{snippet.location.path}:{snippet.location.start_line}`",
                    "",
                    "```",
                    snippet.text,
                    "```",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def query_bundle_from_dict(payload: object) -> QueryBundle:
    """Construct and validate a bundle from parsed JSON."""

    if not isinstance(payload, Mapping):
        raise QueryBundleError("query bundle must be an object")
    data = cast(Mapping[str, Any], payload)
    try:

        def location(raw: Mapping[str, Any]) -> SourceLocation:
            return SourceLocation(**raw)

        def ranked(raw: Mapping[str, Any]) -> RankedNode:
            node_data = dict(raw["node"])
            node_data["location"] = location(node_data["location"])
            return RankedNode(
                rank=raw["rank"],
                node=BundleNode(**node_data),
                direct_score=raw["direct_score"],
                relationship_score=raw["relationship_score"],
                pagerank_score=raw["pagerank_score"],
            )

        paths = tuple(
            StrongestPath(
                source_id=raw["source_id"],
                target_id=raw["target_id"],
                relationship_score=raw["relationship_score"],
                node_ids=tuple(raw["node_ids"]),
                steps=tuple(
                    PathStep(
                        from_node_id=step["from_node_id"],
                        to_node_id=step["to_node_id"],
                        combined_strength=step["combined_strength"],
                        contributions=tuple(
                            PathEdgeContribution(**contribution)
                            for contribution in step["contributions"]
                        ),
                    )
                    for step in raw["steps"]
                ),
            )
            for raw in data["strongest_paths"]
        )
        snippets = tuple(
            SourceSnippet(
                node_id=raw["node_id"],
                location=location(raw["location"]),
                text=raw["text"],
                reviewed=raw["reviewed"],
            )
            for raw in data["source_snippets"]
        )
        bundle = QueryBundle(
            schema_version=data["schema_version"],
            repository=BundleRepository(**data["repository"]),
            query=data["query"],
            retrieval=BundleRetrieval(**data["retrieval"]),
            anchors=tuple(ranked(raw) for raw in data["anchors"]),
            related_nodes=tuple(ranked(raw) for raw in data["related_nodes"]),
            strongest_paths=paths,
            selected_edges=tuple(
                BundleEdge(**(dict(raw) | {"evidence": tuple(raw["evidence"])}))
                for raw in data["selected_edges"]
            ),
            source_snippets=snippets,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise QueryBundleError(f"invalid query bundle structure: {error}") from error
    validate_query_bundle(bundle)
    return bundle


def load_query_bundle(path: Path) -> QueryBundle:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QueryBundleError(f"couldn't read query bundle: {error}") from error
    return query_bundle_from_dict(payload)
