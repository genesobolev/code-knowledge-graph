"""Deterministic, validated data exports for the same-origin static web app."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import cast

import plotly.graph_objects as go  # type: ignore[import-untyped]
import plotly.io as pio  # type: ignore[import-untyped]
from plotly.offline import get_plotlyjs, get_plotlyjs_version  # type: ignore[import-untyped]

from .artifact import (
    PLOTLY_CONFIG,
    VisualizationEdge,
    VisualizationNode,
    query_result_elements,
    query_result_figure_from_elements,
    repository_overview_elements,
    repository_overview_figure_from_elements,
)
from .evaluation import EvaluationComparison, QueryEvaluation, StrategyEvaluation
from .models import CodeNode, KnowledgeGraph, QueryResult

WEB_DATA_SCHEMA_VERSION = 3
WEB_PLOTLY_CONFIG: Mapping[str, object] = {
    **PLOTLY_CONFIG,
    "displayModeBar": True,
    "modeBarButtonsToRemove": ["select2d", "lasso2d"],
}
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?ix)"
    r"(?:['\"])?\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|private[_-]?key|secret[_-]?key|secret|token)\b(?:['\"])?\s*[:=]\s*"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]\r\n]+)"
)
_CREDENTIAL_FIELD = re.compile(
    r"(?i)^(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|"
    r"private[_-]?key|secret[_-]?key|secret|token)$"
)
_TOKEN = re.compile(
    r"(?:"
    r"gh[opsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|"
    r"sk_live_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----", re.IGNORECASE)
_URL_CREDENTIAL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@")
_LOCAL_POSIX_PATH = re.compile(
    r"(?<![A-Za-z0-9:])/(?:builds?|etc|home|mnt|opt|private|root|srv|tmp|Users|var|"
    r"workspaces?)(?:/[^\s'\"`<>]*)?"
)
_WINDOWS_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/](?:[^\r\n'\"<>|:*?]+[\\/]?)+|"
    r"\\\\[^\\/\r\n]+[\\/][^\r\n'\"<>|:*?]+)"
)
_MANAGED_ROOT_FILES = frozenset({"evaluation.json", "manifest.json", "repository.json"})


class PublicExportError(ValueError):
    """Raised when public data isn't reproducible or safe to publish."""


def _require_publishable_snapshot(knowledge: KnowledgeGraph) -> None:
    snapshot = knowledge.snapshot
    if snapshot.dirty:
        raise PublicExportError("public export requires a clean repository snapshot")
    if snapshot.shallow:
        raise PublicExportError("public export requires complete Git history")


def provenance_payload(knowledge: KnowledgeGraph) -> dict[str, object]:
    """Return public provenance for the exact graph snapshot being rendered."""

    snapshot = knowledge.snapshot
    return {
        "repository": snapshot.repository_name,
        "commit": snapshot.commit_sha,
        "tree": snapshot.tree_sha,
        "branch": snapshot.branch,
        "detached": snapshot.detached,
        "clean": not snapshot.dirty,
        "shallow": snapshot.shallow,
        "schema_version": snapshot.schema_version,
        "extractor_version": snapshot.extractor_version,
        "indexed_source_sha256": snapshot.indexed_source_sha256,
        "retrieval": asdict(snapshot.retrieval),
    }


def _plotly_figure_payload(figure: go.Figure) -> dict[str, object]:
    """Serialize notebook graph geometry with the web canvas presentation."""

    serialized = json.loads(pio.to_json(figure, validate=True, pretty=False, remove_uids=True))
    if not isinstance(serialized, dict):
        raise PublicExportError("Plotly figure serialization must produce an object")
    data = serialized.get("data")
    layout = serialized.get("layout")
    if not isinstance(data, list) or not isinstance(layout, dict):
        raise PublicExportError("Plotly figure serialization is missing data or layout")

    for raw_trace in data:
        if not isinstance(raw_trace, dict):
            raise PublicExportError("Plotly figure traces must be objects")
        legend_group = raw_trace.get("legendgroup")
        if not isinstance(legend_group, str) or not legend_group.startswith("node:"):
            continue
        text_font = raw_trace.get("textfont", {})
        if not isinstance(text_font, dict):
            raise PublicExportError("Plotly node text style must be an object")
        raw_trace["textfont"] = {**text_font, "color": "#1e293b"}
        marker = raw_trace.get("marker", {})
        if not isinstance(marker, dict):
            raise PublicExportError("Plotly node marker style must be an object")
        line = marker.get("line", {})
        if not isinstance(line, dict):
            raise PublicExportError("Plotly node outline style must be an object")
        raw_trace["marker"] = {
            **marker,
            "line": {**line, "color": "#f8fafc", "width": 1.25},
        }

    layout.pop("title", None)
    font = layout.get("font", {})
    margin = layout.get("margin", {})
    legend = layout.get("legend", {})
    if not isinstance(font, dict) or not isinstance(margin, dict) or not isinstance(legend, dict):
        raise PublicExportError("Plotly figure layout styles must be objects")
    layout.update(
        {
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "font": {**font, "color": "#1e293b"},
            "hoverlabel": {
                "bgcolor": "#ffffff",
                "bordercolor": "#cbd5e1",
                "font": {"color": "#0f172a"},
            },
            "legend": {
                **legend,
                "bgcolor": "rgba(255,255,255,0.88)",
                "bordercolor": "rgba(203,213,225,0.9)",
                "borderwidth": 1,
                "font": {"color": "#334155", "size": 11},
                "orientation": "h",
                "x": 0.0,
                "xanchor": "left",
                "y": 1.02,
                "yanchor": "bottom",
            },
            "margin": {**margin, "t": 96},
            "modebar": {
                "activecolor": "#0f172a",
                "bgcolor": "rgba(255,255,255,0.88)",
                "color": "#64748b",
            },
        }
    )
    return {
        "plotly_js_version": get_plotlyjs_version(),
        "data": cast(list[object], data),
        "layout": cast(dict[str, object], layout),
        "config": dict(WEB_PLOTLY_CONFIG),
    }


def _inspection_payload(
    knowledge: KnowledgeGraph,
    nodes: Sequence[VisualizationNode],
    edges: Sequence[VisualizationEdge],
    *,
    result: QueryResult | None = None,
) -> dict[str, object]:
    node_records: list[dict[str, object]] = []
    for visualization_node in nodes:
        node = knowledge.nodes.get(visualization_node.id)
        if node is None:
            raise PublicExportError(
                f"visualization references a missing node: {visualization_node.id}"
            )
        node_records.append(
            {
                "id": visualization_node.id,
                "label": visualization_node.label,
                "group": visualization_node.group,
                "color": visualization_node.color,
                "size": visualization_node.size,
                "qualified_name": node.qualified_name,
                "kind": node.kind,
                "path": node.path,
                "start_line": node.start_line,
                "end_line": node.end_line,
                "signature": node.signature,
                "docstring": node.docstring,
                "direct_relevance": (
                    None
                    if result is None
                    else float(result.direct_scores.get(visualization_node.id, 0.0))
                ),
                "relationship_strength": (
                    None
                    if result is None
                    else float(result.relationship_scores.get(visualization_node.id, 0.0))
                ),
            }
        )
    return {
        "nodes": node_records,
        "edges": [
            {
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "kind": edge.kind,
                "strength": edge.strength,
            }
            for edge in edges
        ],
    }


def repository_payload(knowledge: KnowledgeGraph, *, repository_id: str) -> dict[str, object]:
    """Build the repository overview and aligned node inspection data."""

    snapshot = knowledge.snapshot
    _require_publishable_snapshot(knowledge)
    nodes, edges = repository_overview_elements(knowledge)
    payload = {
        "schema_version": WEB_DATA_SCHEMA_VERSION,
        "id": repository_id,
        "name": snapshot.repository_name,
        "commit": snapshot.commit_sha,
        "summary": {
            "node_count": len(knowledge.nodes),
            "edge_count": len(knowledge.edges),
            "issue_count": len(knowledge.issues),
            "resolution_counts": dict(sorted(knowledge.resolution_counts.items())),
        },
        "counts": {
            "nodes": dict(sorted(Counter(node.kind for node in knowledge.nodes.values()).items())),
            "edges": dict(sorted(Counter(edge.kind for edge in knowledge.edges).items())),
        },
        "provenance": provenance_payload(knowledge),
        "figure": _plotly_figure_payload(repository_overview_figure_from_elements(nodes, edges)),
        "inspection": _inspection_payload(knowledge, nodes, edges),
    }
    validate_public_payload(payload)
    return payload


def query_payload(
    knowledge: KnowledgeGraph,
    result: QueryResult,
    *,
    query_id: str,
    label: str,
    description: str,
) -> dict[str, object]:
    """Build one recorded query and aligned node inspection data."""

    _require_publishable_snapshot(knowledge)
    nodes, edges = query_result_elements(knowledge, result)
    payload = {
        "schema_version": WEB_DATA_SCHEMA_VERSION,
        "id": query_id,
        "label": label,
        "query": result.query,
        "description": description,
        "provenance": provenance_payload(knowledge),
        "figure": _plotly_figure_payload(
            query_result_figure_from_elements(nodes, edges, query=result.query)
        ),
        "inspection": _inspection_payload(knowledge, nodes, edges, result=result),
    }
    validate_public_payload(payload)
    return payload


def _ranking_rows(
    result: QueryEvaluation,
    nodes: Mapping[str, CodeNode],
) -> list[dict[str, object]]:
    judgments = {judgment.node_id: judgment for judgment in result.judgments}
    rows: list[dict[str, object]] = []
    for rank, node_id in enumerate(result.ranking, start=1):
        node = nodes.get(node_id)
        if node is None:
            raise PublicExportError(f"evaluation ranking references a missing node: {node_id}")
        judgment = judgments.get(node_id)
        rows.append(
            {
                "rank": rank,
                "node_id": node_id,
                "qualified_name": node.qualified_name,
                "kind": node.kind,
                "path": node.path,
                "start_line": node.start_line,
                "end_line": node.end_line,
                "judgment_role": judgment.role if judgment is not None else None,
                "relevance": judgment.relevance if judgment is not None else None,
            }
        )
    return rows


def _answer_rank(result: QueryEvaluation) -> int | None:
    ranks = [
        judgment.rank
        for judgment in result.judgments
        if judgment.role == "answer" and judgment.rank is not None
    ]
    return min(ranks, default=None)


def _query_strategy_payload(
    result: QueryEvaluation,
    nodes: Mapping[str, CodeNode],
) -> dict[str, object]:
    return {
        "answer_rank": _answer_rank(result),
        "reciprocal_answer_rank_at_10": result.reciprocal_answer_rank_at_10,
        "recall_at_10": result.recall_at_10,
        "recall_at_20": result.recall_at_20,
        "supporting_recall_at_10": result.supporting_recall_at_10,
        "ranking": _ranking_rows(result, nodes),
    }


def _aggregate_metrics(result: StrategyEvaluation) -> dict[str, float]:
    return {
        "answer_mrr_at_10": result.answer_mrr_at_10,
        "recall_at_10": result.recall_at_10,
        "recall_at_20": result.recall_at_20,
        "supporting_recall_at_10": result.supporting_recall_at_10,
    }


def evaluation_payload(
    comparison: EvaluationComparison,
    *,
    provenance: Mapping[str, object],
    nodes: Mapping[str, CodeNode],
) -> dict[str, object]:
    """Build aggregate metrics and auditable per-query ranked results."""

    lexical_metrics = _aggregate_metrics(comparison.lexical)
    graph_metrics = _aggregate_metrics(comparison.graph_expanded)
    delta = {metric: graph_metrics[metric] - lexical_metrics[metric] for metric in lexical_metrics}
    rows: list[dict[str, object]] = []
    for lexical, graph, change in zip(
        comparison.lexical.queries,
        comparison.graph_expanded.queries,
        comparison.queries,
        strict=True,
    ):
        if lexical.id != graph.id or lexical.id != change.id:
            raise PublicExportError("evaluation query ids are not aligned")
        rows.append(
            {
                "id": lexical.id,
                "query": lexical.query,
                "lexical": _query_strategy_payload(lexical, nodes),
                "graph_expanded": _query_strategy_payload(graph, nodes),
                "comparison": {
                    "answer_rank_change": change.answer_rank_change,
                    "newly_retrieved_judgments_at_10": list(change.newly_retrieved_judgments_at_10),
                    "newly_missed_judgments_at_10": list(change.newly_missed_judgments_at_10),
                    "regression": change.regression,
                },
            }
        )
    payload = {
        "schema_version": WEB_DATA_SCHEMA_VERSION,
        "repository": asdict(comparison.repository),
        "provenance": dict(provenance),
        "ranking_budget": comparison.ranking_budget,
        "metric_definition": comparison.metric_definition,
        "aggregate": {
            "lexical": lexical_metrics,
            "graph_expanded": graph_metrics,
            "delta": delta,
            "conclusion": comparison.conclusion,
        },
        "queries": rows,
    }
    validate_public_payload(payload)
    return payload


def manifest_payload(
    knowledge: KnowledgeGraph,
    query_entries: Sequence[Mapping[str, str]],
    *,
    repository_id: str,
    repository_label: str,
) -> dict[str, object]:
    payload = {
        "schema_version": WEB_DATA_SCHEMA_VERSION,
        "provenance": provenance_payload(knowledge),
        "repositories": [
            {"id": repository_id, "label": repository_label, "file": "repository.json"}
        ],
        "queries": [dict(entry) for entry in query_entries],
        "evaluation": "evaluation.json",
    }
    validate_public_payload(payload)
    return payload


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [item for child in value.values() for item in _string_values(child)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for child in value for item in _string_values(child)]
    return []


def _looks_like_absolute_path(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if candidate.startswith("file:"):
        candidate = candidate.removeprefix("file:").split("::", 1)[0]
    candidate = candidate.strip("'\"")
    if (
        "\n" not in candidate
        and "\r" not in candidate
        and (PurePosixPath(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute())
    ):
        return True
    return bool(_LOCAL_POSIX_PATH.search(value) or _WINDOWS_PATH.search(value))


def _dynamic_credential_value(value: str) -> bool:
    candidate = value.strip().strip("'\"").strip()
    lowered = candidate.casefold()
    if not candidate or len(candidate) < 6:
        return True
    if any(marker in candidate for marker in ("${", "{{", "{", "<", "$")):
        return True
    dynamic_prefixes = (
        "config.",
        "env[",
        "getenv(",
        "load_",
        "os.environ",
        "os.getenv(",
        "process.env.",
        "request.",
        "response.",
        "secretmanager.",
        "secrets[",
        "settings.",
    )
    if lowered.startswith(dynamic_prefixes):
        return True
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", candidate))


def _contains_credential(value: str) -> bool:
    if (
        _TOKEN.search(value)
        or _BEARER.search(value)
        or _PRIVATE_KEY.search(value)
        or _URL_CREDENTIAL.search(value)
    ):
        return True
    return any(
        not _dynamic_credential_value(match.group("value"))
        for match in _CREDENTIAL_ASSIGNMENT.finditer(value)
    )


def _explicit_credential_placeholder(value: str) -> bool:
    candidate = value.strip().strip("'\"").strip()
    if any(marker in candidate for marker in ("${", "{{", "{", "<", "$")):
        return True
    return candidate.casefold() in {
        "dummy",
        "example",
        "none",
        "null",
        "placeholder",
        "redacted",
        "secret",
        "token",
    }


def _mapping_contains_credential(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                isinstance(key, str)
                and _CREDENTIAL_FIELD.fullmatch(key)
                and isinstance(child, str)
                and not _explicit_credential_placeholder(child)
            ):
                return True
            if _mapping_contains_credential(child):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_mapping_contains_credential(child) for child in value)
    return False


def validate_public_payload(payload: object) -> None:
    """Reject local filesystem paths and likely secret material in public data."""

    if _mapping_contains_credential(payload):
        raise PublicExportError("public export contains credential-like content")
    for value in _string_values(payload):
        if _looks_like_absolute_path(value):
            raise PublicExportError(f"public export contains an absolute path: {value}")
        if _contains_credential(value):
            raise PublicExportError("public export contains credential-like content")
        if value.startswith("file:"):
            raw_path = value.removeprefix("file:").split("::", 1)[0]
            if PurePosixPath(raw_path).is_absolute() or ".." in PurePosixPath(raw_path).parts:
                raise PublicExportError(f"public export contains an unsafe node path: {value}")


def verify_public_snapshot(knowledge: KnowledgeGraph, expected_commit: str) -> None:
    snapshot = knowledge.snapshot
    _require_publishable_snapshot(knowledge)
    if snapshot.commit_sha != expected_commit:
        raise PublicExportError(
            f"public export commit mismatch: {snapshot.commit_sha} != {expected_commit}"
        )


def write_public_json(path: Path, payload: object) -> None:
    """Validate and write deterministic JSON."""

    validate_public_payload(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_plotly_javascript(path: Path) -> Path:
    """Write the Plotly.js bundle shipped with the installed Python package."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(get_plotlyjs(), encoding="utf-8")
    return path


def _managed_output_path(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".json":
        raise PublicExportError(f"invalid managed public-data path: {value}")
    if relative.name in _MANAGED_ROOT_FILES and len(relative.parts) == 1:
        return relative
    if len(relative.parts) == 2 and relative.parts[0] == "queries":
        return relative
    raise PublicExportError(f"public-data file isn't in a managed location: {value}")


def _existing_managed_files(output_path: Path) -> set[PurePosixPath]:
    if output_path.is_symlink():
        raise PublicExportError("public-data output directory can't be a symbolic link")
    if not output_path.exists():
        return set()
    if not output_path.is_dir():
        raise PublicExportError("public-data output path must be a directory")

    root_paths = tuple(output_path.glob("*.json"))
    if any(path.is_symlink() for path in root_paths):
        raise PublicExportError("public-data root JSON files can't be symbolic links")
    root_json = {path.name for path in root_paths}
    unknown_root = root_json - _MANAGED_ROOT_FILES
    if unknown_root:
        raise PublicExportError(
            f"public-data output contains unmanaged JSON files: {sorted(unknown_root)}"
        )
    if root_json and "manifest.json" not in root_json:
        raise PublicExportError("refusing to replace JSON without a managed manifest")
    if "manifest.json" in root_json:
        try:
            manifest = json.loads((output_path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PublicExportError("existing public-data manifest isn't valid JSON") from exc
        required_manifest_fields = {
            "schema_version",
            "provenance",
            "repositories",
            "queries",
            "evaluation",
        }
        if not isinstance(manifest, Mapping) or not required_manifest_fields <= set(manifest):
            raise PublicExportError("existing JSON isn't a managed public-data dataset")

    query_directory = output_path / "queries"
    if query_directory.is_symlink():
        raise PublicExportError("public-data query directory can't be a symbolic link")
    query_files: set[PurePosixPath] = set()
    if query_directory.exists():
        if not query_directory.is_dir():
            raise PublicExportError("public-data queries path must be a directory")
        unexpected = [
            path.name
            for path in query_directory.iterdir()
            if path.is_dir() or path.suffix != ".json"
        ]
        if unexpected:
            raise PublicExportError(
                f"public-data query directory contains unmanaged entries: {sorted(unexpected)}"
            )
        query_files = {PurePosixPath("queries", path.name) for path in query_directory.iterdir()}
    return {PurePosixPath(name) for name in root_json} | query_files


def write_public_dataset(output_path: Path, payloads: Mapping[str, object]) -> None:
    """Stage and publish the complete managed JSON dataset without retaining stale queries."""

    normalized = {_managed_output_path(name): payload for name, payload in payloads.items()}
    if len(normalized) != len(payloads):
        raise PublicExportError("managed public-data paths must be unique")
    missing = _MANAGED_ROOT_FILES - {path.as_posix() for path in normalized}
    if missing:
        raise PublicExportError(f"public-data payload is missing required files: {sorted(missing)}")
    existing = _existing_managed_files(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{output_path.name}-", dir=output_path.parent) as temporary:
        staging = Path(temporary)
        for relative, payload in sorted(normalized.items(), key=lambda item: item[0].as_posix()):
            write_public_json(staging / relative.as_posix(), payload)

        output_path.mkdir(parents=True, exist_ok=True)
        query_directory = output_path / "queries"
        if query_directory.is_symlink():
            raise PublicExportError("public-data query directory can't be a symbolic link")
        query_directory.mkdir(parents=True, exist_ok=True)

        manifest = PurePosixPath("manifest.json")
        for relative in sorted(normalized, key=PurePosixPath.as_posix):
            if relative == manifest:
                continue
            destination = output_path / relative.as_posix()
            destination.parent.mkdir(parents=True, exist_ok=True)
            (staging / relative.as_posix()).replace(destination)

        for stale in sorted(existing - set(normalized), key=PurePosixPath.as_posix):
            (output_path / stale.as_posix()).unlink()
        (staging / manifest.as_posix()).replace(output_path / manifest.as_posix())
