from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

import networkx as nx
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-untyped]
from sklearn.metrics.pairwise import cosine_similarity  # type: ignore[import-untyped]
from sklearn.pipeline import FeatureUnion  # type: ignore[import-untyped]

from .configuration import RetrievalConfig
from .models import CodeNode, KnowledgeGraph, QueryResult, SearchIndex

CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def expand_identifiers(value: str) -> str:
    expanded = CAMEL_BOUNDARY.sub(" ", value)
    return expanded.replace("_", " ").replace("-", " ")


def node_document(node: CodeNode) -> str:
    identity = " ".join(
        [
            node.kind,
            node.path,
            node.module,
            node.language,
            node.qualified_name,
            node.signature,
            node.docstring,
        ]
    )
    return f"{identity}\n{expand_identifiers(identity)}\n{node.source}"


def build_search_index(nodes: Mapping[str, CodeNode]) -> SearchIndex:
    node_ids = tuple(sorted(nodes))
    if not node_ids:
        raise ValueError("Can't build a search index without nodes")
    documents = [node_document(nodes[node_id]) for node_id in node_ids]
    vectorizer = FeatureUnion(
        [
            (
                "word",
                TfidfVectorizer(
                    lowercase=True,
                    ngram_range=(1, 2),
                    sublinear_tf=True,
                    token_pattern=r"(?u)\b[\w.]+\b",
                ),
            ),
            (
                "identifier",
                TfidfVectorizer(
                    analyzer="char_wb",
                    lowercase=True,
                    ngram_range=(3, 5),
                    sublinear_tf=True,
                ),
            ),
        ],
        transformer_weights={"word": 0.70, "identifier": 0.30},
    )
    matrix = vectorizer.fit_transform(documents)
    return SearchIndex(node_ids, vectorizer, matrix)


def direct_query_scores(index: SearchIndex, query: str) -> dict[str, float]:
    query_vector = index.vectorizer.transform([expand_identifiers(query)])
    values = cosine_similarity(query_vector, index.matrix).ravel()
    return {node_id: float(value) for node_id, value in zip(index.node_ids, values, strict=True)}


def relationship_projection(knowledge: KnowledgeGraph) -> nx.Graph[str]:
    projection: nx.Graph[str] = nx.Graph()
    projection.add_nodes_from(knowledge.nodes)
    for edge in knowledge.edges:
        if projection.has_edge(edge.source_id, edge.target_id):
            current = float(projection[edge.source_id][edge.target_id]["weight"])
            combined = 1.0 - (1.0 - current) * (1.0 - edge.strength)
            projection[edge.source_id][edge.target_id]["weight"] = combined
            projection[edge.source_id][edge.target_id]["kinds"].add(edge.kind)
        else:
            projection.add_edge(
                edge.source_id,
                edge.target_id,
                weight=edge.strength,
                kinds={edge.kind},
            )
    return projection


def strongest_paths(
    projection: nx.Graph[str],
    seed_scores: Mapping[str, float],
    *,
    hops: int,
    hop_decay: float,
) -> tuple[dict[str, float], dict[str, tuple[str, ...]]]:
    if hops < 1:
        raise ValueError("hops must be positive")
    if not 0.0 < hop_decay <= 1.0:
        raise ValueError("hop_decay must be greater than 0.0 and at most 1.0")
    maximum_seed_score = max(seed_scores.values(), default=1.0) or 1.0
    best_scores: dict[str, float] = {}
    best_paths: dict[str, tuple[str, ...]] = {}
    frontier: list[tuple[str, float, tuple[str, ...]]] = [
        (seed_id, score / maximum_seed_score, (seed_id,)) for seed_id, score in seed_scores.items()
    ]
    for _depth in range(hops):
        next_frontier: list[tuple[str, float, tuple[str, ...]]] = []
        for current_id, current_score, path in frontier:
            for neighbor_id, attributes in projection[current_id].items():
                if neighbor_id in path:
                    continue
                score = current_score * hop_decay * float(attributes["weight"])
                candidate_path = (*path, neighbor_id)
                if score > best_scores.get(neighbor_id, 0.0):
                    best_scores[neighbor_id] = score
                    best_paths[neighbor_id] = candidate_path
                next_frontier.append((neighbor_id, score, candidate_path))
        frontier = next_frontier
    return best_scores, best_paths


def edge_step(knowledge: KnowledgeGraph, left_id: str, right_id: str) -> str:
    candidates: list[tuple[float, str]] = []
    forward = cast(
        Mapping[object, Mapping[str, Any]],
        knowledge.graph.get_edge_data(left_id, right_id, default={}),
    )
    reverse = cast(
        Mapping[object, Mapping[str, Any]],
        knowledge.graph.get_edge_data(right_id, left_id, default={}),
    )
    for attributes in forward.values():
        kind = str(attributes["kind"])
        step = (
            f"-[{kind} {attributes['strength']:.2f}]-"
            if kind == "CO_CHANGES"
            else f"-[{kind} {attributes['strength']:.2f}]->"
        )
        candidates.append((float(attributes["strength"]), step))
    for attributes in reverse.values():
        kind = str(attributes["kind"])
        step = (
            f"-[{kind} {attributes['strength']:.2f}]-"
            if kind == "CO_CHANGES"
            else f"<-[{kind} {attributes['strength']:.2f}]-"
        )
        candidates.append((float(attributes["strength"]), step))
    return max(candidates, default=(0.0, "--"))[1]


def format_path(knowledge: KnowledgeGraph, path: Sequence[str]) -> str:
    if not path:
        return ""
    parts = [knowledge.nodes[path[0]].qualified_name]
    for left_id, right_id in itertools.pairwise(path):
        parts.extend(
            [edge_step(knowledge, left_id, right_id), knowledge.nodes[right_id].qualified_name]
        )
    return " ".join(parts)


def query_graph(
    knowledge: KnowledgeGraph,
    index: SearchIndex,
    query: str,
    *,
    config: RetrievalConfig | None = None,
    seed_count: int | None = None,
    hops: int | None = None,
    related_count: int | None = None,
    hop_decay: float | None = None,
) -> QueryResult:
    parameters = config or knowledge.snapshot.retrieval
    parameters = RetrievalConfig(
        seed_count=parameters.seed_count if seed_count is None else seed_count,
        hops=parameters.hops if hops is None else hops,
        related_count=parameters.related_count if related_count is None else related_count,
        hop_decay=parameters.hop_decay if hop_decay is None else hop_decay,
    )

    direct_scores = direct_query_scores(index, query)
    ranked_direct = sorted(
        direct_scores,
        key=lambda node_id: (-direct_scores[node_id], node_id),
    )
    anchors = tuple(ranked_direct[: min(parameters.seed_count, len(ranked_direct))])
    anchor_scores = {node_id: direct_scores[node_id] for node_id in anchors}

    projection = relationship_projection(knowledge)
    personalization_total = sum(anchor_scores.values())
    if personalization_total:
        personalization = {
            node_id: score / personalization_total for node_id, score in anchor_scores.items()
        }
    else:
        personalization = {node_id: 1.0 / len(anchors) for node_id in anchors}
    pagerank = nx.pagerank(
        projection,
        alpha=0.85,
        personalization=personalization,
        weight="weight",
    )

    relationship_scores, paths = strongest_paths(
        projection,
        anchor_scores,
        hops=parameters.hops,
        hop_decay=parameters.hop_decay,
    )
    for anchor in anchors:
        relationship_scores.pop(anchor, None)
        paths.pop(anchor, None)

    maximum_pagerank = max(pagerank.values(), default=1.0) or 1.0
    candidates = sorted(
        relationship_scores,
        key=lambda node_id: (
            -relationship_scores[node_id],
            -pagerank.get(node_id, 0.0),
            node_id,
        ),
    )[: parameters.related_count]

    relevant_rows = []
    for rank, node_id in enumerate(anchors, start=1):
        node = knowledge.nodes[node_id]
        relevant_rows.append(
            {
                "rank": rank,
                "node_id": node.id,
                "node": node.qualified_name,
                "kind": node.kind,
                "location": node.location,
                "direct_relevance": round(direct_scores[node_id], 4),
            }
        )

    related_rows = []
    for rank, node_id in enumerate(candidates, start=1):
        node = knowledge.nodes[node_id]
        related_rows.append(
            {
                "rank": rank,
                "node_id": node.id,
                "node": node.qualified_name,
                "kind": node.kind,
                "location": node.location,
                "relationship_strength": round(relationship_scores[node_id], 4),
                "pagerank": round(pagerank.get(node_id, 0.0) / maximum_pagerank, 4),
                "direct_relevance": round(direct_scores[node_id], 4),
                "strongest_path": format_path(knowledge, paths[node_id]),
            }
        )

    return QueryResult(
        query=query,
        anchors=anchors,
        relevant=pd.DataFrame(relevant_rows),
        related=pd.DataFrame(related_rows),
        direct_scores=direct_scores,
        relationship_scores=relationship_scores,
        pagerank_scores=pagerank,
        paths=paths,
    )
