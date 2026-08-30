from __future__ import annotations

import html
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import networkx as nx
import plotly.graph_objects as go  # type: ignore[import-untyped]

from .configuration import SCHEMA_VERSION, RetrievalConfig
from .extraction import create_networkx_graph, validate_knowledge_graph
from .models import (
    CodeEdge,
    CodeNode,
    KnowledgeGraph,
    ParseIssue,
    QueryResult,
    RepositorySnapshot,
)
from .retrieval import relationship_projection

NODE_COLORS: Mapping[str, str] = {
    "file": "#64748b",
    "class": "#a78bfa",
    "method": "#60a5fa",
    "function": "#34d399",
    "test": "#f472b6",
    "fixture": "#facc15",
}
EDGE_COLORS: Mapping[str, str] = {
    "CALLS": "#38bdf8",
    "CO_CHANGES": "#a78bfa",
    "CONTAINS": "#64748b",
    "IMPORTS": "#22d3ee",
    "INHERITS": "#f472b6",
    "INSTANTIATES": "#fb923c",
    "TESTS": "#facc15",
}
PLOTLY_CONFIG: Mapping[str, object] = {
    "displaylogo": False,
    "responsive": True,
    "scrollZoom": True,
}


@dataclass(frozen=True)
class VisualizationNode:
    id: str
    label: str
    group: str
    color: str
    size: float
    tooltip: str


@dataclass(frozen=True)
class VisualizationEdge:
    source_id: str
    target_id: str
    kind: str
    strength: float
    tooltip: str


def knowledge_graph_to_dict(knowledge: KnowledgeGraph) -> dict[str, object]:
    """Return a canonicalizable graph record without the local repository root."""
    return {
        "schema_version": knowledge.snapshot.schema_version,
        "snapshot": asdict(knowledge.snapshot),
        "nodes": [asdict(knowledge.nodes[node_id]) for node_id in sorted(knowledge.nodes)],
        "edges": [
            asdict(edge)
            for edge in sorted(
                knowledge.edges,
                key=lambda item: (item.source_id, item.target_id, item.kind, item.evidence),
            )
        ],
        "issues": [asdict(issue) for issue in sorted(knowledge.issues, key=lambda item: item.path)],
        "resolution_counts": dict(sorted(knowledge.resolution_counts.items())),
    }


def serialize_knowledge_graph(knowledge: KnowledgeGraph) -> bytes:
    payload = knowledge_graph_to_dict(knowledge)
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Artifact field {name!r} must be an object")
    return cast(Mapping[str, Any], value)


def knowledge_graph_from_dict(payload: Mapping[str, Any]) -> KnowledgeGraph:
    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported graph schema {schema_version!r}; expected {SCHEMA_VERSION!r}"
        )

    snapshot_data = dict(_mapping(payload.get("snapshot"), "snapshot"))
    snapshot_data["retrieval"] = RetrievalConfig(
        **dict(_mapping(snapshot_data.get("retrieval"), "snapshot.retrieval"))
    )
    snapshot = RepositorySnapshot(**snapshot_data)

    node_records = payload.get("nodes")
    edge_records = payload.get("edges")
    issue_records = payload.get("issues", [])
    if not isinstance(node_records, list) or not isinstance(edge_records, list):
        raise ValueError("Artifact nodes and edges must be arrays")
    if not isinstance(issue_records, list):
        raise ValueError("Artifact issues must be an array")

    nodes = {
        node.id: node
        for node in (CodeNode(**dict(_mapping(value, "nodes[]"))) for value in node_records)
    }
    edges = tuple(
        CodeEdge(
            **{
                **dict(_mapping(value, "edges[]")),
                "evidence": tuple(_mapping(value, "edges[]")["evidence"]),
            }
        )
        for value in edge_records
    )
    issues = tuple(ParseIssue(**dict(_mapping(value, "issues[]"))) for value in issue_records)
    resolution_counts = {
        str(key): int(value)
        for key, value in _mapping(
            payload.get("resolution_counts", {}), "resolution_counts"
        ).items()
    }
    validate_knowledge_graph(nodes, edges)
    return KnowledgeGraph(
        snapshot=snapshot,
        nodes=nodes,
        edges=edges,
        graph=create_networkx_graph(nodes, edges),
        issues=issues,
        resolution_counts=resolution_counts,
    )


def deserialize_knowledge_graph(content: bytes | str) -> KnowledgeGraph:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid graph artifact JSON: {exc}") from exc
    try:
        return knowledge_graph_from_dict(_mapping(payload, "root"))
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f"Invalid graph artifact structure: {exc}") from exc


def write_graph_artifact(knowledge: KnowledgeGraph, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize_knowledge_graph(knowledge))
    return path


def read_graph_artifact(path: Path) -> KnowledgeGraph:
    return deserialize_knowledge_graph(path.read_bytes())


graph_to_dict = knowledge_graph_to_dict
graph_from_dict = knowledge_graph_from_dict
serialize_graph = serialize_knowledge_graph
deserialize_graph = deserialize_knowledge_graph


def visualization_positions(
    nodes: Sequence[VisualizationNode],
    edges: Sequence[VisualizationEdge],
) -> dict[str, tuple[float, float]]:
    graph: nx.Graph[str] = nx.Graph()
    graph.add_nodes_from(node.id for node in nodes)
    for edge in edges:
        graph.add_edge(edge.source_id, edge.target_id, weight=edge.strength)
    positions = nx.spring_layout(graph, seed=42, weight="weight", iterations=100)
    return {
        node_id: (float(coordinates[0]), float(coordinates[1]))
        for node_id, coordinates in positions.items()
    }


def build_plotly_network(
    nodes: Sequence[VisualizationNode],
    edges: Sequence[VisualizationEdge],
    *,
    title: str,
    height: int,
) -> go.Figure:
    positions = visualization_positions(nodes, edges)
    traces: list[Any] = []

    edge_groups: dict[tuple[str, int], list[VisualizationEdge]] = defaultdict(list)
    for edge in edges:
        strength_bucket = min(4, int(edge.strength * 4))
        edge_groups[(edge.kind, strength_bucket)].append(edge)

    legend_kinds: set[str] = set()
    for (kind, strength_bucket), grouped_edges in sorted(edge_groups.items()):
        x_values: list[float | None] = []
        y_values: list[float | None] = []
        for edge in grouped_edges:
            source_x, source_y = positions[edge.source_id]
            target_x, target_y = positions[edge.target_id]
            x_values.extend([source_x, target_x, None])
            y_values.extend([source_y, target_y, None])
        show_legend = kind not in legend_kinds
        legend_kinds.add(kind)
        traces.append(
            go.Scatter(
                x=x_values,
                y=y_values,
                mode="lines",
                line={
                    "color": EDGE_COLORS.get(kind, "#94a3b8"),
                    "width": 0.75 + 1.25 * strength_bucket,
                },
                hoverinfo="skip",
                legendgroup=f"edge:{kind}",
                name=f"{kind} edge",
                showlegend=show_legend,
            )
        )

    traces.append(
        go.Scatter(
            x=[
                (positions[edge.source_id][0] + positions[edge.target_id][0]) / 2.0
                for edge in edges
            ],
            y=[
                (positions[edge.source_id][1] + positions[edge.target_id][1]) / 2.0
                for edge in edges
            ],
            mode="markers",
            marker={"color": "rgba(0,0,0,0.01)", "size": 12},
            hovertext=[edge.tooltip for edge in edges],
            hovertemplate="%{hovertext}<extra></extra>",
            name="relationship details",
            showlegend=False,
        )
    )

    nodes_by_group: dict[str, list[VisualizationNode]] = defaultdict(list)
    for node in nodes:
        nodes_by_group[node.group].append(node)
    for group, grouped_nodes in sorted(nodes_by_group.items()):
        traces.append(
            go.Scatter(
                x=[positions[node.id][0] for node in grouped_nodes],
                y=[positions[node.id][1] for node in grouped_nodes],
                mode="markers+text",
                text=[node.label for node in grouped_nodes],
                textposition="top center",
                textfont={"color": "#e2e8f0", "size": 10},
                marker={
                    "color": [node.color for node in grouped_nodes],
                    "line": {"color": "#e2e8f0", "width": 0.75},
                    "size": [node.size for node in grouped_nodes],
                },
                hovertext=[node.tooltip for node in grouped_nodes],
                hovertemplate="%{hovertext}<extra></extra>",
                name=group,
                legendgroup=f"node:{group}",
            )
        )

    figure = go.Figure(data=traces)
    figure.update_layout(
        title={"text": title, "x": 0.02},
        height=height,
        paper_bgcolor="#0f172a",
        plot_bgcolor="#0f172a",
        font={"color": "#e2e8f0"},
        hovermode="closest",
        dragmode="pan",
        margin={"b": 20, "l": 20, "r": 20, "t": 60},
        legend={"bgcolor": "rgba(15,23,42,0.75)", "orientation": "h"},
        xaxis={"showgrid": False, "showticklabels": False, "zeroline": False},
        yaxis={"showgrid": False, "showticklabels": False, "zeroline": False},
        uirevision=title,
    )
    return figure


def node_tooltip(node: CodeNode) -> str:
    signature = html.escape(node.signature or node.qualified_name)
    docstring = html.escape(node.docstring[:500])
    return (
        f"<b>{signature}</b><br>"
        f"{html.escape(node.location)}<br>"
        f"kind: {html.escape(node.kind)}<br>"
        f"{docstring}"
    )


def repository_overview_figure(knowledge: KnowledgeGraph) -> go.Figure:
    file_nodes = {node.id: node for node in knowledge.nodes.values() if node.kind == "file"}
    path_to_file = {node.path: node.id for node in file_nodes.values()}
    aggregated: dict[tuple[str, str, str], float] = {}
    for edge in knowledge.edges:
        source_file = path_to_file[knowledge.nodes[edge.source_id].path]
        target_file = path_to_file[knowledge.nodes[edge.target_id].path]
        if source_file == target_file:
            continue
        key = (source_file, target_file, edge.kind)
        current = aggregated.get(key, 0.0)
        aggregated[key] = 1.0 - (1.0 - current) * (1.0 - edge.strength)

    degree_counts: Counter[str] = Counter()
    overview_edges: list[VisualizationEdge] = []
    for (source_id, target_id, kind), strength in sorted(aggregated.items()):
        degree_counts.update((source_id, target_id))
        source = file_nodes[source_id]
        target = file_nodes[target_id]
        connector = "--" if kind == "CO_CHANGES" else "->"
        overview_edges.append(
            VisualizationEdge(
                source_id=source_id,
                target_id=target_id,
                kind=kind,
                strength=strength,
                tooltip=(
                    f"<b>{kind}</b><br>{html.escape(source.path)} {connector} "
                    f"{html.escape(target.path)}<br>strength: {strength:.3f}"
                ),
            )
        )
    overview_nodes = [
        VisualizationNode(
            id=node.id,
            label=node.path,
            group="file",
            color=NODE_COLORS["file"],
            size=15.0 + 2.5 * math.log1p(degree_counts[node.id]),
            tooltip=node_tooltip(node),
        )
        for node in sorted(file_nodes.values(), key=lambda item: item.id)
    ]
    return build_plotly_network(
        overview_nodes,
        overview_edges,
        title="Repository file relationships",
        height=720,
    )


def strongest_edge_between(
    knowledge: KnowledgeGraph, left_id: str, right_id: str
) -> CodeEdge | None:
    matches = [
        edge for edge in knowledge.edges if {edge.source_id, edge.target_id} == {left_id, right_id}
    ]
    return max(matches, key=lambda edge: edge.strength, default=None)


def query_result_figure(knowledge: KnowledgeGraph, result: QueryResult) -> go.Figure:
    selected: set[str] = set(result.anchors)
    related_ids = set(result.related["node_id"].tolist()) if not result.related.empty else set()
    selected.update(related_ids)
    for node_id in tuple(selected):
        selected.update(result.paths.get(node_id, ()))

    top_anchor = result.anchors[0] if result.anchors else ""
    query_nodes: list[VisualizationNode] = []
    for node_id in sorted(selected):
        node = knowledge.nodes[node_id]
        if node_id == top_anchor:
            color = "#fb7185"
            group = "top anchor"
        elif node_id in result.anchors:
            color = "#f59e0b"
            group = "anchor"
        else:
            color = NODE_COLORS.get(node.kind, "#38bdf8")
            group = node.kind
        score = max(
            result.direct_scores.get(node_id, 0.0),
            result.relationship_scores.get(node_id, 0.0),
        )
        query_nodes.append(
            VisualizationNode(
                id=node_id,
                label=node.qualified_name,
                group=group,
                color=color,
                size=14.0 + 24.0 * score,
                tooltip=(
                    f"{node_tooltip(node)}<br>direct relevance: "
                    f"{result.direct_scores.get(node_id, 0.0):.4f}<br>"
                    f"relationship strength: "
                    f"{result.relationship_scores.get(node_id, 0.0):.4f}"
                ),
            )
        )

    query_edges: list[VisualizationEdge] = []
    projection = relationship_projection(knowledge)
    for left_id, right_id in sorted(projection.subgraph(selected).edges):
        edge = strongest_edge_between(knowledge, left_id, right_id)
        if edge is None:
            continue
        source = knowledge.nodes[edge.source_id]
        target = knowledge.nodes[edge.target_id]
        connector = "--" if edge.kind == "CO_CHANGES" else "->"
        evidence = "<br>".join(html.escape(item) for item in edge.evidence)
        query_edges.append(
            VisualizationEdge(
                source_id=edge.source_id,
                target_id=edge.target_id,
                kind=edge.kind,
                strength=edge.strength,
                tooltip=(
                    f"<b>{edge.kind}</b><br>{html.escape(source.qualified_name)} "
                    f"{connector} {html.escape(target.qualified_name)}<br>"
                    f"strength: {edge.strength:.3f}<br>"
                    f"confidence: {edge.confidence:.3f}<br>"
                    f"evidence count: {edge.count}<br>{evidence}"
                ),
            )
        )
    return build_plotly_network(
        query_nodes,
        query_edges,
        title=f"Query relationships: {result.query}",
        height=760,
    )


def write_plotly_html(figure: go.Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.write_html(
        str(path),
        include_plotlyjs=True,
        full_html=True,
        auto_open=False,
        config=dict(PLOTLY_CONFIG),
        div_id=f"code-knowledge-graph-{path.stem}",
    )
    return path
