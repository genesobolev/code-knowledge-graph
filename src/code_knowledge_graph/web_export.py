"""Deterministic, validated data exports for the same-origin static web app."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Any

import networkx as nx

from .context import QueryBundle, query_bundle_to_json, query_bundle_to_markdown
from .evaluation import EvaluationComparison
from .models import KnowledgeGraph

WEB_DATA_SCHEMA_VERSION = 1
LAYOUT_SEED = 17
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


def provenance_payload(bundle: QueryBundle) -> dict[str, object]:
    return {
        "repository": bundle.repository.name,
        "commit": bundle.repository.commit_sha,
        "tree": bundle.repository.tree_sha,
        "branch": bundle.repository.branch,
        "detached": bundle.repository.detached,
        "clean": bundle.repository.clean,
        "shallow": bundle.repository.shallow,
        "schema_version": bundle.repository.graph_schema_version,
        "extractor_version": bundle.repository.extractor_version,
        "indexed_source_sha256": bundle.repository.indexed_source_sha256,
        "retrieval": asdict(bundle.retrieval),
    }


def _positions(graph: nx.Graph[str], node_ids: Sequence[str]) -> dict[str, tuple[float, float]]:
    ordered = tuple(sorted(set(node_ids)))
    if not ordered:
        return {}

    layout_graph: nx.Graph[str] = nx.Graph()
    layout_graph.add_nodes_from(ordered)
    edge_records = sorted(
        (
            min(str(source), str(target)),
            max(str(source), str(target)),
            float(data.get("strength", data.get("weight", 1.0))),
        )
        for source, target, data in graph.subgraph(ordered).edges(data=True)
    )
    for source, target, strength in edge_records:
        current = float(layout_graph.get_edge_data(source, target, {}).get("strength", 0.0))
        combined = 1.0 - (1.0 - current) * (1.0 - strength)
        layout_graph.add_edge(source, target, strength=combined)

    raw = nx.spring_layout(
        layout_graph,
        seed=LAYOUT_SEED,
        weight="strength",
        iterations=100,
    )
    return {
        node_id: (
            round(500.0 + float(raw[node_id][0]) * 400.0, 3),
            round(300.0 + float(raw[node_id][1]) * 240.0, 3),
        )
        for node_id in ordered
    }


def repository_payload(knowledge: KnowledgeGraph, *, repository_id: str) -> dict[str, object]:
    """Build repository counts and sanitized provenance."""

    snapshot = knowledge.snapshot
    _require_publishable_snapshot(knowledge)
    return {
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
        "provenance": {
            "repository": snapshot.repository_name,
            "commit": snapshot.commit_sha,
            "tree": snapshot.tree_sha,
            "branch": snapshot.branch,
            "detached": snapshot.detached,
            "clean": True,
            "shallow": snapshot.shallow,
            "schema_version": snapshot.schema_version,
            "extractor_version": snapshot.extractor_version,
            "indexed_source_sha256": snapshot.indexed_source_sha256,
            "retrieval": asdict(snapshot.retrieval),
        },
    }


def query_payload(
    knowledge: KnowledgeGraph,
    bundle: QueryBundle,
    *,
    query_id: str,
    label: str,
    description: str,
) -> dict[str, object]:
    """Build one recorded query graph and both bundle representations."""

    _require_publishable_snapshot(knowledge)
    if not bundle.repository.clean:
        raise PublicExportError("public query export requires a clean repository snapshot")
    if bundle.repository.commit_sha != knowledge.snapshot.commit_sha:
        raise PublicExportError("query bundle commit doesn't match the repository graph")
    ranked = (*bundle.anchors, *bundle.related_nodes)
    positions = _positions(knowledge.graph, [item.node.node_id for item in ranked])
    anchor_ids = {item.node.node_id for item in bundle.anchors}
    nodes = []
    for item in ranked:
        x, y = positions[item.node.node_id]
        nodes.append(
            {
                "id": item.node.node_id,
                "label": item.node.qualified_name,
                "kind": item.node.kind,
                "language": item.node.language,
                "path": item.node.location.path,
                "start_line": item.node.location.start_line,
                "end_line": item.node.location.end_line,
                "rank_group": "anchor" if item.node.node_id in anchor_ids else "related",
                "score": max(item.direct_score, item.relationship_score),
                "direct_score": item.direct_score,
                "relationship_score": item.relationship_score,
                "pagerank_score": item.pagerank_score,
                "x": x,
                "y": y,
            }
        )
    edges = [
        asdict(edge) | {"id": edge.edge_id, "type": edge.kind} for edge in bundle.selected_edges
    ]
    paths = [
        {
            "id": f"path-{index}",
            "label": (
                f"{knowledge.nodes[path.source_id].qualified_name} → "
                f"{knowledge.nodes[path.target_id].qualified_name}"
            ),
            "source_id": path.source_id,
            "target_id": path.target_id,
            "score": path.relationship_score,
            "nodes": list(path.node_ids),
            "steps": [asdict(step) for step in path.steps],
        }
        for index, path in enumerate(bundle.strongest_paths, 1)
    ]

    def ranked_row(item: Any) -> dict[str, object]:
        return {
            "rank": item.rank,
            "node_id": item.node.node_id,
            "label": item.node.qualified_name,
            "path": item.node.location.path,
            "score": item.direct_score
            if item.node.node_id in anchor_ids
            else item.relationship_score,
            "direct_score": item.direct_score,
            "relationship_score": item.relationship_score,
            "pagerank_score": item.pagerank_score,
        }

    payload = {
        "schema_version": WEB_DATA_SCHEMA_VERSION,
        "id": query_id,
        "label": label,
        "query": bundle.query,
        "description": description,
        "provenance": provenance_payload(bundle),
        "nodes": nodes,
        "edges": edges,
        "paths": paths,
        "relevant": [ranked_row(item) for item in bundle.anchors],
        "related": [ranked_row(item) for item in bundle.related_nodes],
        "context": {
            "json": json.loads(query_bundle_to_json(bundle)),
            "markdown": query_bundle_to_markdown(bundle),
        },
    }
    validate_public_payload(payload)
    return payload


def evaluation_payload(
    comparison: EvaluationComparison,
    *,
    provenance: Mapping[str, object],
) -> dict[str, object]:
    """Adapt full evaluation detail to the static app's metric contract."""

    lexical_by_id = {item.id: item for item in comparison.lexical.queries}
    graph_by_id = {item.id: item for item in comparison.graph_expanded.queries}
    comparisons = {item.id: item for item in comparison.queries}
    rows = []
    misses = []
    for query_id in lexical_by_id:
        lexical = lexical_by_id[query_id]
        graph = graph_by_id[query_id]
        change = comparisons[query_id]
        rows.append(
            {
                "id": query_id,
                "query": lexical.query,
                "lexical": {"score": lexical.reciprocal_answer_rank_at_10},
                "graph": {"score": graph.reciprocal_answer_rank_at_10},
                "lexical_recall_at_10": lexical.recall_at_10,
                "graph_recall_at_10": graph.recall_at_10,
                "newly_retrieved_at_10": list(change.newly_retrieved_at_10),
                "newly_missed_at_10": list(change.newly_missed_at_10),
                "regression": change.regression,
            }
        )
        if lexical.missed_at_10 or graph.missed_at_10 or change.regression:
            prefix = "Regression: " if change.regression else ""
            misses.append(
                {
                    "id": query_id,
                    "query": lexical.query,
                    "reason": prefix
                    + (
                        f"Lexical missed {len(lexical.missed_at_10)} and graph-expanded missed "
                        f"{len(graph.missed_at_10)} reviewed judgments at rank 10."
                    ),
                    "lexical_missed_at_10": list(lexical.missed_at_10),
                    "graph_missed_at_10": list(graph.missed_at_10),
                    "newly_retrieved_at_10": list(change.newly_retrieved_at_10),
                    "newly_missed_at_10": list(change.newly_missed_at_10),
                    "regression": change.regression,
                }
            )
    payload = {
        "schema_version": WEB_DATA_SCHEMA_VERSION,
        "provenance": dict(provenance),
        "ranking_budget": comparison.ranking_budget,
        "metric_definition": comparison.metric_definition,
        "summary": {
            "lexical": {"score": comparison.lexical.answer_mrr_at_10},
            "graph": {"score": comparison.graph_expanded.answer_mrr_at_10},
            "lexical_metrics": asdict(comparison.lexical) | {"queries": []},
            "graph_metrics": asdict(comparison.graph_expanded) | {"queries": []},
            "conclusion": comparison.conclusion,
        },
        "queries": rows,
        "misses": misses,
        "detail": asdict(comparison),
    }
    validate_public_payload(payload)
    return payload


def manifest_payload(
    bundle: QueryBundle,
    query_entries: Sequence[Mapping[str, str]],
    *,
    repository_id: str,
    repository_label: str,
) -> dict[str, object]:
    payload = {
        "schema_version": WEB_DATA_SCHEMA_VERSION,
        "provenance": provenance_payload(bundle),
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
