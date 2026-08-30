from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import networkx as nx
import pandas as pd
import pytest

from code_knowledge_graph.configuration import RetrievalConfig
from code_knowledge_graph.context import (
    MAX_SNIPPET_CHARACTERS,
    QueryBundleError,
    build_query_bundle,
    query_bundle_from_dict,
    query_bundle_to_json,
    query_bundle_to_markdown,
    validate_query_bundle,
)
from code_knowledge_graph.models import (
    CodeEdge,
    CodeNode,
    KnowledgeGraph,
    QueryResult,
    RepositorySnapshot,
)
from code_knowledge_graph.retrieval import relationship_projection

SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "query-bundle-v1.schema.json"


def query_fixture(
    *, dirty: bool = False, absolute_path: bool = False
) -> tuple[KnowledgeGraph, QueryResult]:
    first = CodeNode(
        id="symbol:src/service.py::run",
        kind="function",
        name="run",
        qualified_name="run",
        path="/tmp/service.py" if absolute_path else "src/service.py",
        module="service",
        language="python",
        start_line=4,
        end_line=6,
        source="def run() -> None:\n    helper()",
    )
    second = CodeNode(
        id="symbol:src/service.py::helper",
        kind="function",
        name="helper",
        qualified_name="helper",
        path="src/service.py",
        module="service",
        language="python",
        start_line=1,
        end_line=2,
        source="def helper() -> None:\n    pass",
    )
    edge = CodeEdge(first.id, second.id, "CALLS", 1, 0.95, 0.9, ("run calls helper",))
    graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
    graph.add_edge(first.id, second.id, kind=edge.kind, strength=edge.strength)
    snapshot = RepositorySnapshot(
        repository_name="sample",
        remote_url="https://user:credential@example.test/acme/sample.git?token=hidden",
        commit_sha="1" * 40,
        tree_sha="2" * 40,
        branch="main",
        detached=False,
        dirty=dirty,
        shallow=False,
        indexed_source_sha256="3" * 64,
        schema_version="1.0.0",
        extractor_version="1.0.0",
        retrieval=RetrievalConfig(seed_count=1, related_count=1),
    )
    knowledge = KnowledgeGraph(snapshot, {first.id: first, second.id: second}, (edge,), graph)
    result = QueryResult(
        query="what calls helper?",
        anchors=(first.id,),
        relevant=pd.DataFrame([{"node_id": first.id}]),
        related=pd.DataFrame([{"node_id": second.id}]),
        direct_scores={first.id: 0.8, second.id: 0.2},
        relationship_scores={second.id: 0.765},
        pagerank_scores={first.id: 0.6, second.id: 0.4},
        paths={second.id: (first.id, second.id)},
    )
    return knowledge, result


def test_json_and_markdown_derive_from_the_same_structured_bundle() -> None:
    knowledge, result = query_fixture()
    bundle = build_query_bundle(knowledge, result)

    payload = json.loads(query_bundle_to_json(bundle))
    markdown = query_bundle_to_markdown(bundle)

    assert payload["anchors"][0]["node"]["node_id"] in markdown
    assert payload["selected_edges"][0]["evidence"] == ["run calls helper"]
    step = payload["strongest_paths"][0]["steps"][0]
    assert step["combined_strength"] == 0.9
    assert step["contributions"][0]["traversal_direction"] == "forward"
    assert payload["repository"]["remote_url"] == "https://example.test/acme/sample.git"
    assert query_bundle_from_dict(payload) == bundle


def test_path_step_exports_every_parallel_relationship_contribution() -> None:
    knowledge, result = query_fixture()
    first_id, second_id = result.anchors[0], result.related.iloc[0]["node_id"]
    reverse = CodeEdge(
        second_id,
        first_id,
        "IMPORTS",
        1,
        0.8,
        0.4,
        ("helper imports run",),
    )
    undirected = CodeEdge(
        first_id,
        second_id,
        "CO_CHANGES",
        2,
        0.7,
        0.25,
        ("changed together",),
    )
    knowledge = replace(knowledge, edges=(*knowledge.edges, reverse, undirected))
    projection_strength = float(relationship_projection(knowledge)[first_id][second_id]["weight"])
    result = replace(
        result,
        relationship_scores={second_id: 0.85 * projection_strength},
    )

    bundle = build_query_bundle(knowledge, result)
    step = bundle.strongest_paths[0].steps[0]

    assert step.combined_strength == pytest.approx(projection_strength)
    assert [(item.kind, item.traversal_direction) for item in step.contributions] == [
        ("CALLS", "forward"),
        ("IMPORTS", "reverse"),
        ("CO_CHANGES", "undirected"),
    ]
    assert {item.edge_id for item in step.contributions} == {
        edge.edge_id for edge in bundle.selected_edges
    }
    markdown = query_bundle_to_markdown(bundle)
    assert f"combined {projection_strength:.4f}" in markdown
    assert "`CALLS` forward 0.9000" in markdown
    assert "`IMPORTS` reverse 0.4000" in markdown
    assert "`CO_CHANGES` undirected 0.2500" in markdown
    assert query_bundle_from_dict(json.loads(query_bundle_to_json(bundle))) == bundle


def test_path_validation_reproduces_steps_and_relationship_score() -> None:
    knowledge, result = query_fixture()
    bundle = build_query_bundle(knowledge, result)
    path = bundle.strongest_paths[0]
    step = path.steps[0]

    with pytest.raises(QueryBundleError, match="unresolved edge contribution"):
        validate_query_bundle(replace(bundle, selected_edges=()))
    with pytest.raises(QueryBundleError, match="combined strength"):
        invalid_step = replace(step, combined_strength=0.1)
        validate_query_bundle(
            replace(bundle, strongest_paths=(replace(path, steps=(invalid_step,)),))
        )
    with pytest.raises(QueryBundleError, match="can't be reproduced"):
        validate_query_bundle(
            replace(bundle, strongest_paths=(replace(path, relationship_score=0.1),))
        )


def test_schema_describes_parallel_path_contributions() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    step = schema["$defs"]["step"]
    contribution = schema["$defs"]["pathEdgeContribution"]

    assert set(step["required"]) == {
        "from_node_id",
        "to_node_id",
        "combined_strength",
        "contributions",
    }
    assert step["properties"]["contributions"]["minItems"] == 1
    assert set(contribution["required"]) == {
        "edge_id",
        "kind",
        "traversal_direction",
        "strength",
    }


def test_empty_related_frame_without_columns_produces_an_anchor_only_bundle() -> None:
    knowledge, result = query_fixture()
    empty = replace(
        result,
        related=pd.DataFrame(),
        relationship_scores={},
        paths={},
    )

    bundle = build_query_bundle(knowledge, empty)

    assert not bundle.related_nodes
    assert not bundle.strongest_paths
    assert not bundle.selected_edges


def test_blank_bundle_query_is_rejected() -> None:
    knowledge, result = query_fixture()

    with pytest.raises(QueryBundleError, match="must not be blank"):
        build_query_bundle(knowledge, replace(result, query="   "))


def test_dev_bundle_preserves_dirty_provenance() -> None:
    knowledge, result = query_fixture(dirty=True)

    bundle = build_query_bundle(knowledge, result)

    assert bundle.repository.clean is False
    assert '"clean": false' in query_bundle_to_json(bundle)


def test_source_snippets_are_opt_in_reviewed_and_bounded() -> None:
    knowledge, result = query_fixture()
    node_id = result.anchors[0]

    assert not build_query_bundle(knowledge, result).source_snippets
    with pytest.raises(QueryBundleError, match="include_source"):
        build_query_bundle(knowledge, result, reviewed_snippet_node_ids=(node_id,))

    bundle = build_query_bundle(
        knowledge,
        result,
        include_source=True,
        reviewed_snippet_node_ids=(node_id,),
        max_snippet_characters=12,
    )
    assert bundle.source_snippets[0].reviewed is True
    assert len(bundle.source_snippets[0].text) <= 12
    assert MAX_SNIPPET_CHARACTERS == 800


@pytest.mark.parametrize("maximum", [1, 2])
def test_tiny_source_snippet_limits_are_honored(maximum: int) -> None:
    knowledge, result = query_fixture()
    bundle = build_query_bundle(
        knowledge,
        result,
        include_source=True,
        reviewed_snippet_node_ids=(result.anchors[0],),
        max_snippet_characters=maximum,
    )

    assert len(bundle.source_snippets[0].text) <= maximum


def test_loaded_bundles_enforce_schema_types_and_review_status() -> None:
    knowledge, result = query_fixture()
    node_id = result.anchors[0]
    bundle = build_query_bundle(
        knowledge,
        result,
        include_source=True,
        reviewed_snippet_node_ids=(node_id,),
    )
    payload = json.loads(query_bundle_to_json(bundle))
    payload["source_snippets"][0]["reviewed"] = False

    with pytest.raises(QueryBundleError, match="reviewed must be true"):
        query_bundle_from_dict(payload)

    payload = json.loads(query_bundle_to_json(bundle))
    payload["retrieval"]["seed_count"] = True
    with pytest.raises(QueryBundleError, match="seed_count must be an integer"):
        query_bundle_from_dict(payload)

    payload = json.loads(query_bundle_to_json(bundle))
    payload["repository"]["commit_sha"] = "not-a-sha"
    with pytest.raises(QueryBundleError, match="full lowercase Git SHA"):
        query_bundle_from_dict(payload)


def test_absolute_repository_location_is_rejected() -> None:
    knowledge, result = query_fixture(absolute_path=True)

    with pytest.raises(QueryBundleError, match="repository-relative"):
        build_query_bundle(knowledge, result)


def test_unresolved_query_nodes_are_rejected() -> None:
    knowledge, result = query_fixture()
    missing = replace(result, anchors=("symbol:missing.py::lost",))

    with pytest.raises(QueryBundleError, match="missing nodes"):
        build_query_bundle(knowledge, missing)
