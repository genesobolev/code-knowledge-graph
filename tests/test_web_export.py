from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import networkx as nx
import pandas as pd
import pytest
from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]

from code_knowledge_graph.configuration import RetrievalConfig
from code_knowledge_graph.context import build_query_bundle
from code_knowledge_graph.models import (
    CodeEdge,
    CodeNode,
    KnowledgeGraph,
    QueryResult,
    RepositorySnapshot,
)
from code_knowledge_graph.web_export import (
    PublicExportError,
    query_payload,
    repository_payload,
    validate_public_payload,
    verify_public_snapshot,
    write_public_dataset,
)

DATA_PATH = Path(__file__).parents[1] / "web" / "public" / "data"
SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "query-bundle-v1.schema.json"


def web_fixture(*, dirty: bool = False) -> tuple[KnowledgeGraph, QueryResult]:
    nodes = {
        node.id: node
        for node in (
            CodeNode("symbol:src/a.py::a", "function", "a", "a", "src/a.py", "a", "python", 1, 2),
            CodeNode("symbol:src/b.py::b", "function", "b", "b", "src/b.py", "b", "python", 1, 2),
        )
    }
    ids = tuple(nodes)
    edge = CodeEdge(ids[0], ids[1], "CALLS", 1, 1.0, 0.9, ("a calls b",))
    graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
    graph.add_edge(ids[0], ids[1], strength=edge.strength, kind=edge.kind)
    snapshot = RepositorySnapshot(
        "sample",
        "https://example.test/sample.git",
        "1" * 40,
        "2" * 40,
        "main",
        False,
        dirty,
        False,
        "3" * 64,
        "1.0.0",
        "1.0.0",
        RetrievalConfig(seed_count=1, related_count=1),
    )
    knowledge = KnowledgeGraph(snapshot, nodes, (edge,), graph)
    result = QueryResult(
        "where is b called?",
        (ids[0],),
        pd.DataFrame([{"node_id": ids[0]}]),
        pd.DataFrame([{"node_id": ids[1]}]),
        {ids[0]: 0.8, ids[1]: 0.2},
        {ids[1]: 0.765},
        {ids[0]: 0.6, ids[1]: 0.4},
        {ids[1]: ids},
    )
    return knowledge, result


def test_query_export_has_deterministic_coordinates_and_shared_context() -> None:
    knowledge, result = web_fixture()
    bundle = build_query_bundle(knowledge, result)

    first = cast(
        dict[str, Any],
        query_payload(knowledge, bundle, query_id="calls", label="Calls", description="Demo"),
    )
    second = query_payload(knowledge, bundle, query_id="calls", label="Calls", description="Demo")

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert all(isinstance(node["x"], float) for node in first["nodes"])
    assert all(100.0 <= node["x"] <= 900.0 for node in first["nodes"])
    assert all(60.0 <= node["y"] <= 540.0 for node in first["nodes"])
    assert first["context"]["json"]["query"] == result.query
    assert result.query in first["context"]["markdown"]


def test_query_coordinates_are_stable_across_hash_seeds() -> None:
    program = """
import json
import networkx as nx
from code_knowledge_graph.web_export import _positions

nodes = {"symbol:src/a.py::a", "symbol:src/b.py::b", "file:src/a.py", "file:src/b.py"}
edges = {
    ("symbol:src/a.py::a", "symbol:src/b.py::b", 0.9),
    ("file:src/a.py", "symbol:src/a.py::a", 0.6),
    ("file:src/b.py", "symbol:src/b.py::b", 0.6),
}
graph = nx.MultiDiGraph()
graph.add_nodes_from(nodes)
for source, target, strength in edges:
    graph.add_edge(source, target, strength=strength)
print(json.dumps(_positions(graph, tuple(nodes)), sort_keys=True))
"""
    outputs = []
    for hash_seed in ("3", "817"):
        completed = subprocess.run(
            [sys.executable, "-c", program],
            check=True,
            capture_output=True,
            text=True,
            env=dict(os.environ) | {"PYTHONHASHSEED": hash_seed},
        )
        outputs.append(completed.stdout)

    assert outputs[0] == outputs[1]


def test_public_exports_reject_dirty_or_mismatched_snapshots() -> None:
    dirty_knowledge, dirty_result = web_fixture(dirty=True)
    dirty_bundle = build_query_bundle(dirty_knowledge, dirty_result)

    with pytest.raises(PublicExportError, match="clean"):
        repository_payload(dirty_knowledge, repository_id="sample")
    with pytest.raises(PublicExportError, match="clean"):
        query_payload(
            dirty_knowledge,
            dirty_bundle,
            query_id="q",
            label="Query",
            description="Dirty",
        )

    clean_knowledge, _ = web_fixture()
    with pytest.raises(PublicExportError, match="mismatch"):
        verify_public_snapshot(clean_knowledge, "f" * 40)

    shallow_knowledge = replace(
        clean_knowledge, snapshot=replace(clean_knowledge.snapshot, shallow=True)
    )
    with pytest.raises(PublicExportError, match="complete Git history"):
        repository_payload(shallow_knowledge, repository_id="sample")


@pytest.mark.parametrize(
    "unsafe",
    [
        "/home/user/private.py",
        "/custom/build/output.py",
        r"C:\Users\person\private.py",
        r"\\server\private\source.py",
        "/tmp/repository/file.py",
        "password=not-a-placeholder",
        {"password": "correct-horse-battery-staple"},
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "https://user:credential@example.test/repo.git",
        "ghp_123456789012345678901234567890",
    ],
)
def test_public_export_rejects_absolute_paths_and_credentials(unsafe: object) -> None:
    with pytest.raises(PublicExportError):
        validate_public_payload({"nested": [{"value": unsafe}]})


@pytest.mark.parametrize(
    "safe",
    [
        'password = os.getenv("PASSWORD")',
        'headers = {"Authorization": f"Bearer {token}"}',
        'return "/api/v1/health"',
        "token = response.json()",
    ],
)
def test_public_export_allows_normal_code_snippets(safe: str) -> None:
    validate_public_payload({"source_snippet": safe})


def test_public_dataset_replaces_managed_files_and_removes_stale_queries(
    tmp_path: Path,
) -> None:
    output = tmp_path / "data"

    def payloads(query_id: str) -> dict[str, object]:
        return {
            "repository.json": {"id": "sample"},
            "evaluation.json": {"ranking_budget": 20},
            "manifest.json": {
                "schema_version": 1,
                "provenance": {"clean": True},
                "repositories": [],
                "queries": [{"id": query_id, "file": f"queries/{query_id}.json"}],
                "evaluation": "evaluation.json",
            },
            f"queries/{query_id}.json": {"id": query_id},
        }

    write_public_dataset(output, payloads("old-query"))
    write_public_dataset(output, payloads("new-query"))

    assert not (output / "queries/old-query.json").exists()
    assert json.loads((output / "queries/new-query.json").read_text(encoding="utf-8")) == {
        "id": "new-query"
    }
    assert sorted(path.name for path in (output / "queries").iterdir()) == ["new-query.json"]


def test_public_query_rejects_bundle_from_another_snapshot() -> None:
    knowledge, result = web_fixture()
    bundle = build_query_bundle(knowledge, result)
    other_snapshot = replace(knowledge.snapshot, commit_sha="f" * 40)
    other = replace(knowledge, snapshot=other_snapshot)

    with pytest.raises(PublicExportError, match="doesn't match"):
        query_payload(other, bundle, query_id="q", label="Query", description="Mismatch")


def test_generated_static_data_has_resolved_references_and_manifest_contract() -> None:
    manifest = json.loads((DATA_PATH / "manifest.json").read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    assert set(manifest) == {
        "schema_version",
        "provenance",
        "repositories",
        "queries",
        "evaluation",
    }
    assert manifest["provenance"]["clean"] is True
    assert len(manifest["queries"]) == 14
    for entry in manifest["queries"]:
        payload = json.loads((DATA_PATH / entry["file"]).read_text(encoding="utf-8"))
        validate_public_payload(payload)
        validator.validate(payload["context"]["json"])
        node_ids = {node["id"] for node in payload["nodes"]}
        assert all(
            edge["source_id"] in node_ids and edge["target_id"] in node_ids
            for edge in payload["edges"]
        )
        assert all(set(path["nodes"]) <= node_ids for path in payload["paths"])
