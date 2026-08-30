from __future__ import annotations

from pathlib import Path

from code_knowledge_graph import (
    GraphConfig,
    build_knowledge_graph,
    build_search_index,
    format_path,
    query_graph,
    relationship_projection,
    strongest_paths,
)


def test_tfidf_query_and_relationship_expansion(git_repository: Path) -> None:
    knowledge = build_knowledge_graph(GraphConfig(git_repository))
    index = build_search_index(knowledge.nodes)
    result = query_graph(
        knowledge,
        index,
        "execute worker service",
        seed_count=2,
        hops=2,
        related_count=10,
    )

    assert len(result.anchors) == 2
    assert not result.relevant.empty
    assert not result.related.empty
    assert result.related["relationship_strength"].is_monotonic_decreasing
    assert result.related["strongest_path"].str.len().min() > 0
    assert set(result.paths) == set(result.relationship_scores)


def test_strongest_paths_keep_explainable_node_sequences(git_repository: Path) -> None:
    knowledge = build_knowledge_graph(GraphConfig(git_repository))
    projection = relationship_projection(knowledge)
    seed = "symbol:src/sample/service.py::execute"
    scores, paths = strongest_paths(projection, {seed: 1.0}, hops=2, hop_decay=0.85)

    assert scores
    destination = max(scores, key=scores.__getitem__)
    assert paths[destination][0] == seed
    assert paths[destination][-1] == destination
    assert knowledge.nodes[seed].qualified_name in format_path(knowledge, paths[destination])
