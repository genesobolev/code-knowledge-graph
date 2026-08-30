from __future__ import annotations

from pathlib import Path

import pytest

from code_knowledge_graph import (
    GraphConfig,
    build_knowledge_graph,
    deserialize_knowledge_graph,
    serialize_knowledge_graph,
)


def test_graph_artifact_is_deterministic_portable_and_round_trips(
    git_repository: Path,
) -> None:
    graph = build_knowledge_graph(GraphConfig(git_repository))
    first = serialize_knowledge_graph(graph)
    second = serialize_knowledge_graph(graph)

    assert first == second
    assert str(git_repository).encode() not in first
    assert b'"repository_root"' not in first

    restored = deserialize_knowledge_graph(first)
    assert restored.snapshot == graph.snapshot
    assert restored.nodes == graph.nodes
    assert restored.edges == graph.edges
    assert serialize_knowledge_graph(restored) == first


def test_malformed_graph_structure_raises_a_clean_value_error() -> None:
    malformed = '{"schema_version":"1.0.0","snapshot":{"retrieval":{}},"nodes":[],"edges":[]}'

    with pytest.raises(ValueError, match="Invalid graph artifact structure"):
        deserialize_knowledge_graph(malformed)
