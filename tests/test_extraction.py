from __future__ import annotations

from pathlib import Path

from code_knowledge_graph import GraphConfig, build_knowledge_graph


def test_extraction_preserves_typed_ast_file_and_git_relations(git_repository: Path) -> None:
    knowledge = build_knowledge_graph(GraphConfig(git_repository))

    assert not knowledge.issues
    assert "file:web/app.js" in knowledge.nodes
    assert knowledge.nodes["file:web/app.js"].language == "javascript"
    worker_id = "symbol:src/sample/service.py::Worker"
    run_id = "symbol:src/sample/service.py::Worker.run"
    test_id = "symbol:tests/test_service.py::test_execute"
    assert knowledge.nodes[worker_id].kind == "class"
    assert knowledge.nodes[run_id].kind == "method"
    assert knowledge.nodes[test_id].kind == "test"

    edge_kinds = {edge.kind for edge in knowledge.edges}
    assert {"CALLS", "CO_CHANGES", "CONTAINS", "IMPORTS", "INHERITS", "TESTS"} <= edge_kinds
    cochange = next(edge for edge in knowledge.edges if edge.kind == "CO_CHANGES")
    assert cochange.count >= 2
    assert cochange.evidence


def test_extraction_is_deterministic_for_an_unchanged_repository(git_repository: Path) -> None:
    first = build_knowledge_graph(GraphConfig(git_repository))
    second = build_knowledge_graph(GraphConfig(git_repository))

    assert first.snapshot == second.snapshot
    assert first.nodes == second.nodes
    assert first.edges == second.edges
