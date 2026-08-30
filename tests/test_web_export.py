from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import networkx as nx
import pandas as pd
import plotly.graph_objects as go  # type: ignore[import-untyped]
import plotly.io as pio  # type: ignore[import-untyped]
import pytest
from plotly.offline import get_plotlyjs, get_plotlyjs_version  # type: ignore[import-untyped]

from code_knowledge_graph.artifact import (
    query_result_figure,
    repository_overview_figure,
)
from code_knowledge_graph.configuration import RetrievalConfig
from code_knowledge_graph.evaluation import (
    Benchmark,
    BenchmarkQuery,
    BenchmarkRepository,
    Judgment,
    compare_rankings,
)
from code_knowledge_graph.models import (
    CodeEdge,
    CodeNode,
    KnowledgeGraph,
    QueryResult,
    RepositorySnapshot,
)
from code_knowledge_graph.web_export import (
    WEB_DATA_SCHEMA_VERSION,
    WEB_PLOTLY_CONFIG,
    PublicExportError,
    evaluation_payload,
    query_payload,
    repository_payload,
    validate_public_payload,
    verify_public_snapshot,
    write_plotly_javascript,
    write_public_dataset,
)

DATA_PATH = Path(__file__).parents[1] / "web" / "public" / "data"
PLOTLY_PATH = DATA_PATH.parent / "vendor" / "plotly.min.js"
REMOVED_TOP_LEVEL_FIELDS = frozenset({"context", "nodes", "edges", "paths", "relevant", "related"})
INSPECTION_NODE_FIELDS = {
    "id",
    "label",
    "group",
    "color",
    "size",
    "qualified_name",
    "kind",
    "path",
    "start_line",
    "end_line",
    "signature",
    "docstring",
    "direct_relevance",
    "relationship_strength",
}
INSPECTION_EDGE_FIELDS = {"source_id", "target_id", "kind", "strength"}
INVARIANT_TRACE_FIELDS = {
    "type",
    "mode",
    "x",
    "y",
    "text",
    "textposition",
    "customdata",
    "hoverinfo",
    "hovertext",
    "hovertemplate",
    "name",
    "legendgroup",
    "showlegend",
}


def web_fixture(*, dirty: bool = False) -> tuple[KnowledgeGraph, QueryResult]:
    file_a = CodeNode(
        "file:src/a.py", "file", "a.py", "src/a.py", "src/a.py", "src.a", "python", 1, 20
    )
    symbol_a = CodeNode(
        "symbol:src/a.py::a",
        "function",
        "a",
        "a",
        "src/a.py",
        "src.a",
        "python",
        1,
        2,
        "def a() -> None:",
        "Call b.",
    )
    file_b = CodeNode(
        "file:src/b.py", "file", "b.py", "src/b.py", "src/b.py", "src.b", "python", 1, 20
    )
    symbol_b = CodeNode(
        "symbol:src/b.py::b",
        "function",
        "b",
        "b",
        "src/b.py",
        "src.b",
        "python",
        1,
        2,
        "def b() -> None:",
        "Complete the operation.",
    )
    nodes = {node.id: node for node in (file_a, symbol_a, file_b, symbol_b)}
    edges = (
        CodeEdge(file_a.id, symbol_a.id, "CONTAINS", 1, 1.0, 1.0, ("contains a",)),
        CodeEdge(file_b.id, symbol_b.id, "CONTAINS", 1, 1.0, 1.0, ("contains b",)),
        CodeEdge(symbol_a.id, symbol_b.id, "CALLS", 1, 1.0, 0.9, ("a calls b",)),
    )
    graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
    graph.add_nodes_from(nodes)
    for edge in edges:
        graph.add_edge(
            edge.source_id,
            edge.target_id,
            strength=edge.strength,
            kind=edge.kind,
        )
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
    knowledge = KnowledgeGraph(snapshot, nodes, edges, graph)
    result = QueryResult(
        "where is b called?",
        (symbol_a.id,),
        pd.DataFrame([{"node_id": symbol_a.id}]),
        pd.DataFrame([{"node_id": symbol_b.id}]),
        {symbol_a.id: 0.8, symbol_b.id: 0.2},
        {symbol_b.id: 0.765},
        {symbol_a.id: 0.6, symbol_b.id: 0.4},
        {symbol_b.id: (symbol_a.id, symbol_b.id)},
    )
    return knowledge, result


def serialized_figure(figure: go.Figure) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(pio.to_json(figure, validate=True, pretty=False, remove_uids=True)),
    )


def displayed_node_ids(figure: dict[str, Any]) -> set[str]:
    return {
        node_id
        for trace in figure["data"]
        if str(trace.get("legendgroup", "")).startswith("node:")
        for node_id in trace["customdata"]
    }


def assert_web_figure_preserves_notebook_graph(
    web_figure: dict[str, Any],
    notebook_figure: go.Figure,
) -> None:
    notebook = serialized_figure(notebook_figure)
    web_traces = web_figure["data"]
    notebook_traces = notebook["data"]

    assert web_figure["plotly_js_version"] == get_plotlyjs_version()
    assert len(web_traces) == len(notebook_traces)
    for web_trace, notebook_trace in zip(web_traces, notebook_traces, strict=True):
        for field in INVARIANT_TRACE_FIELDS:
            assert web_trace.get(field) == notebook_trace.get(field)
        if str(web_trace.get("legendgroup", "")).startswith("node:"):
            assert web_trace["marker"]["color"] == notebook_trace["marker"]["color"]
            assert web_trace["marker"]["size"] == notebook_trace["marker"]["size"]
            assert web_trace["marker"]["line"] == {"color": "#f8fafc", "width": 1.25}
            assert web_trace["textfont"]["color"] == "#1e293b"
        else:
            assert web_trace.get("line") == notebook_trace.get("line")
            assert web_trace.get("marker") == notebook_trace.get("marker")

    for field in ("dragmode", "height", "hovermode", "uirevision", "xaxis", "yaxis"):
        assert web_figure["layout"][field] == notebook["layout"][field]
    assert "title" in notebook["layout"]
    assert "title" not in web_figure["layout"]
    assert web_figure["layout"]["paper_bgcolor"] == "rgba(0,0,0,0)"
    assert web_figure["layout"]["plot_bgcolor"] == "rgba(0,0,0,0)"
    assert web_figure["layout"]["font"]["color"] == "#1e293b"
    assert web_figure["layout"]["legend"]["yanchor"] == "bottom"
    assert web_figure["layout"]["modebar"]["color"] == "#64748b"
    assert web_figure["config"] == dict(WEB_PLOTLY_CONFIG)
    assert web_figure["config"]["modeBarButtonsToRemove"] == ["select2d", "lasso2d"]
    assert "modeBarButtons" not in web_figure["config"]


def test_repository_and_query_exports_preserve_notebook_graphs_with_web_theme() -> None:
    knowledge, result = web_fixture()

    repository = repository_payload(knowledge, repository_id="sample")
    query = query_payload(
        knowledge,
        result,
        query_id="calls",
        label="Calls",
        description="Demo",
    )

    assert repository["schema_version"] == WEB_DATA_SCHEMA_VERSION == 3
    assert_web_figure_preserves_notebook_graph(
        cast(dict[str, Any], repository["figure"]),
        repository_overview_figure(knowledge),
    )
    assert query == query_payload(
        knowledge,
        result,
        query_id="calls",
        label="Calls",
        description="Demo",
    )
    assert_web_figure_preserves_notebook_graph(
        cast(dict[str, Any], query["figure"]),
        query_result_figure(knowledge, result),
    )
    assert set(query) == {
        "schema_version",
        "id",
        "label",
        "query",
        "description",
        "provenance",
        "figure",
        "inspection",
    }
    assert not REMOVED_TOP_LEVEL_FIELDS.intersection(query)


def test_inspection_data_matches_displayed_nodes_edges_and_query_scores() -> None:
    knowledge, result = web_fixture()
    repository = repository_payload(knowledge, repository_id="sample")
    query = query_payload(
        knowledge,
        result,
        query_id="calls",
        label="Calls",
        description="Demo",
    )

    repository_figure = cast(dict[str, Any], repository["figure"])
    repository_inspection = cast(dict[str, Any], repository["inspection"])
    repository_nodes = repository_inspection["nodes"]
    repository_edges = repository_inspection["edges"]
    assert {node["id"] for node in repository_nodes} == displayed_node_ids(repository_figure)
    assert all(set(node) == INSPECTION_NODE_FIELDS for node in repository_nodes)
    assert all(node["direct_relevance"] is None for node in repository_nodes)
    assert all(node["relationship_strength"] is None for node in repository_nodes)
    assert all(set(edge) == INSPECTION_EDGE_FIELDS for edge in repository_edges)
    assert repository_edges == [
        {
            "source_id": "file:src/a.py",
            "target_id": "file:src/b.py",
            "kind": "CALLS",
            "strength": 0.9,
        }
    ]

    query_figure = cast(dict[str, Any], query["figure"])
    query_inspection = cast(dict[str, Any], query["inspection"])
    query_nodes = {node["id"]: node for node in query_inspection["nodes"]}
    assert set(query_nodes) == displayed_node_ids(query_figure)
    assert all(set(node) == INSPECTION_NODE_FIELDS for node in query_nodes.values())
    assert query_nodes["symbol:src/a.py::a"]["signature"] == "def a() -> None:"
    assert query_nodes["symbol:src/a.py::a"]["docstring"] == "Call b."
    assert query_nodes["symbol:src/a.py::a"]["direct_relevance"] == 0.8
    assert query_nodes["symbol:src/a.py::a"]["relationship_strength"] == 0.0
    assert query_nodes["symbol:src/b.py::b"]["direct_relevance"] == 0.2
    assert query_nodes["symbol:src/b.py::b"]["relationship_strength"] == 0.765
    assert query_inspection["edges"] == [
        {
            "source_id": "symbol:src/a.py::a",
            "target_id": "symbol:src/b.py::b",
            "kind": "CALLS",
            "strength": 0.9,
        }
    ]


def evaluation_fixture() -> tuple[Benchmark, dict[str, CodeNode], list[str], list[str]]:
    benchmark = Benchmark(
        schema_version=1,
        repository=BenchmarkRepository(
            name="sample",
            url="https://example.test/sample",
            commit="1" * 40,
        ),
        review_status="reviewed",
        queries=(
            BenchmarkQuery(
                id="calls",
                query="where is b called?",
                judgments=(
                    Judgment("answer", "answer", 3),
                    Judgment("support", "supporting", 2),
                ),
            ),
        ),
    )
    all_node_ids = ["answer", "support", *(f"irrelevant:{index}" for index in range(18))]
    nodes = {
        node_id: CodeNode(
            node_id,
            "function",
            node_id,
            f"sample.{node_id}",
            "src/sample.py",
            "sample",
            "python",
            rank,
            rank + 1,
        )
        for rank, node_id in enumerate(all_node_ids, start=1)
    }
    lexical = ["answer", *all_node_ids[2:], "support"]
    graph = ["answer", "support", *all_node_ids[2:]]
    return benchmark, nodes, lexical, graph


def test_evaluation_export_has_honest_aggregates_and_auditable_rankings() -> None:
    benchmark, nodes, lexical, graph = evaluation_fixture()
    comparison = compare_rankings(benchmark, {"calls": lexical}, {"calls": graph})

    payload = evaluation_payload(
        comparison,
        provenance={"commit": "1" * 40, "tree": "2" * 40},
        nodes=nodes,
    )

    assert set(payload) == {
        "schema_version",
        "repository",
        "provenance",
        "ranking_budget",
        "metric_definition",
        "aggregate",
        "queries",
    }
    aggregate = cast(dict[str, Any], payload["aggregate"])
    assert set(aggregate) == {"lexical", "graph_expanded", "delta", "conclusion"}
    assert aggregate["delta"] == {
        "answer_mrr_at_10": 0.0,
        "recall_at_10": 0.5,
        "recall_at_20": 0.0,
        "supporting_recall_at_10": 1.0,
    }
    assert "answer MRR@10 is unchanged" in aggregate["conclusion"]
    assert "supporting recall@10 increases by 1.000" in aggregate["conclusion"]

    query = cast(dict[str, Any], cast(list[object], payload["queries"])[0])
    assert set(query) == {"id", "query", "lexical", "graph_expanded", "comparison"}
    strategy_fields = {
        "answer_rank",
        "reciprocal_answer_rank_at_10",
        "recall_at_10",
        "recall_at_20",
        "supporting_recall_at_10",
        "ranking",
    }
    assert set(query["lexical"]) == strategy_fields
    assert set(query["graph_expanded"]) == strategy_fields
    assert query["lexical"]["answer_rank"] == 1
    assert query["graph_expanded"]["answer_rank"] == 1
    assert len(query["lexical"]["ranking"]) == 20
    ranking_fields = {
        "rank",
        "node_id",
        "qualified_name",
        "kind",
        "path",
        "start_line",
        "end_line",
        "judgment_role",
        "relevance",
    }
    assert all(set(row) == ranking_fields for row in query["lexical"]["ranking"])
    assert query["lexical"]["ranking"][0]["judgment_role"] == "answer"
    assert query["lexical"]["ranking"][-1]["judgment_role"] == "supporting"
    assert query["lexical"]["ranking"][1]["judgment_role"] is None
    assert query["comparison"] == {
        "answer_rank_change": 0,
        "newly_retrieved_judgments_at_10": ["support"],
        "newly_missed_judgments_at_10": [],
        "regression": False,
    }


def test_evaluation_export_rejects_missing_ranking_node_metadata() -> None:
    benchmark, nodes, lexical, graph = evaluation_fixture()
    comparison = compare_rankings(benchmark, {"calls": lexical}, {"calls": graph})
    nodes.pop("irrelevant:0")

    with pytest.raises(PublicExportError, match="missing node"):
        evaluation_payload(comparison, provenance={}, nodes=nodes)


def test_public_exports_reject_dirty_or_mismatched_snapshots() -> None:
    dirty_knowledge, dirty_result = web_fixture(dirty=True)

    with pytest.raises(PublicExportError, match="clean"):
        repository_payload(dirty_knowledge, repository_id="sample")
    with pytest.raises(PublicExportError, match="clean"):
        query_payload(
            dirty_knowledge,
            dirty_result,
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


def test_query_export_validates_inspection_text() -> None:
    knowledge, result = web_fixture()
    nodes = dict(knowledge.nodes)
    node_id = "symbol:src/a.py::a"
    nodes[node_id] = replace(nodes[node_id], docstring="Read /home/person/private.txt")
    unsafe_knowledge = replace(knowledge, nodes=nodes)

    with pytest.raises(PublicExportError, match="absolute path"):
        query_payload(
            unsafe_knowledge,
            result,
            query_id="calls",
            label="Calls",
            description="Demo",
        )


def test_public_dataset_replaces_managed_files_and_removes_stale_queries(
    tmp_path: Path,
) -> None:
    output = tmp_path / "data"

    def payloads(query_id: str) -> dict[str, object]:
        return {
            "repository.json": {"id": "sample"},
            "evaluation.json": {"ranking_budget": 20},
            "manifest.json": {
                "schema_version": WEB_DATA_SCHEMA_VERSION,
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


def test_plotly_javascript_is_copied_from_the_installed_package(tmp_path: Path) -> None:
    destination = tmp_path / "vendor" / "plotly.min.js"

    result = write_plotly_javascript(destination)

    assert result == destination
    assert destination.read_text(encoding="utf-8") == get_plotlyjs()
    assert get_plotlyjs_version() in destination.read_text(encoding="utf-8")[:200]


def test_generated_static_data_has_one_snapshot_and_notebook_plotly_contract() -> None:
    manifest = json.loads((DATA_PATH / "manifest.json").read_text(encoding="utf-8"))
    repository = json.loads((DATA_PATH / "repository.json").read_text(encoding="utf-8"))
    evaluation = json.loads((DATA_PATH / "evaluation.json").read_text(encoding="utf-8"))

    assert set(manifest) == {
        "schema_version",
        "provenance",
        "repositories",
        "queries",
        "evaluation",
    }
    assert manifest["schema_version"] == WEB_DATA_SCHEMA_VERSION == 3
    assert repository["schema_version"] == WEB_DATA_SCHEMA_VERSION
    assert evaluation["schema_version"] == WEB_DATA_SCHEMA_VERSION
    assert manifest["provenance"]["clean"] is True
    assert len(manifest["queries"]) == 14
    expected_snapshot = {
        "commit": manifest["provenance"]["commit"],
        "tree": manifest["provenance"]["tree"],
    }
    assert {
        "commit": repository["provenance"]["commit"],
        "tree": repository["provenance"]["tree"],
    } == expected_snapshot
    assert {
        "commit": evaluation["provenance"]["commit"],
        "tree": evaluation["provenance"]["tree"],
    } == expected_snapshot
    assert set(repository["figure"]) == {"plotly_js_version", "data", "layout", "config"}
    assert repository["figure"]["data"]
    assert set(repository["inspection"]) == {"nodes", "edges"}
    assert {node["id"] for node in repository["inspection"]["nodes"]} == displayed_node_ids(
        repository["figure"]
    )
    assert all(set(node) == INSPECTION_NODE_FIELDS for node in repository["inspection"]["nodes"])
    assert all(set(edge) == INSPECTION_EDGE_FIELDS for edge in repository["inspection"]["edges"])
    assert PLOTLY_PATH.is_file()
    assert (
        repository["figure"]["plotly_js_version"] in PLOTLY_PATH.read_text(encoding="utf-8")[:200]
    )

    manifest_query_ids = {entry["id"] for entry in manifest["queries"]}
    assert len(evaluation["queries"]) == 14
    assert {query["id"] for query in evaluation["queries"]} == manifest_query_ids
    assert all(len(query["lexical"]["ranking"]) == 20 for query in evaluation["queries"])
    assert all(len(query["graph_expanded"]["ranking"]) == 20 for query in evaluation["queries"])

    for entry in manifest["queries"]:
        payload = json.loads((DATA_PATH / entry["file"]).read_text(encoding="utf-8"))
        validate_public_payload(payload)
        assert payload["schema_version"] == WEB_DATA_SCHEMA_VERSION
        assert {
            "commit": payload["provenance"]["commit"],
            "tree": payload["provenance"]["tree"],
        } == expected_snapshot
        assert set(payload["figure"]) == {
            "plotly_js_version",
            "data",
            "layout",
            "config",
        }
        assert payload["figure"]["plotly_js_version"] == repository["figure"]["plotly_js_version"]
        assert payload["figure"]["data"]
        assert isinstance(payload["figure"]["layout"], dict)
        assert payload["figure"]["config"] == dict(WEB_PLOTLY_CONFIG)
        assert set(payload["inspection"]) == {"nodes", "edges"}
        assert {node["id"] for node in payload["inspection"]["nodes"]} == displayed_node_ids(
            payload["figure"]
        )
        assert all(set(node) == INSPECTION_NODE_FIELDS for node in payload["inspection"]["nodes"])
        assert all(set(edge) == INSPECTION_EDGE_FIELDS for edge in payload["inspection"]["edges"])
        assert not REMOVED_TOP_LEVEL_FIELDS.intersection(payload)
