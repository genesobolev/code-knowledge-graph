"""Command-line access to reproducible graph artifacts and agent context bundles."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from .artifact import read_graph_artifact, write_graph_artifact
from .configuration import GraphConfig, GraphMode, RetrievalConfig
from .context import (
    build_query_bundle,
    query_bundle_to_json,
    query_bundle_to_markdown,
)
from .extraction import build_knowledge_graph
from .models import KnowledgeGraph
from .retrieval import build_search_index, query_graph


def _retrieval_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed-count", type=int, default=6)
    parser.add_argument("--hops", type=int, default=2)
    parser.add_argument("--related-count", type=int, default=25)
    parser.add_argument("--hop-decay", type=float, default=0.85)


def _retrieval_config(arguments: argparse.Namespace) -> RetrievalConfig:
    return RetrievalConfig(
        seed_count=arguments.seed_count,
        hops=arguments.hops,
        related_count=arguments.related_count,
        hop_decay=arguments.hop_decay,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-knowledge-graph",
        description="Build and query a local, explainable code knowledge graph.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser("index", help="Build a deterministic graph artifact.")
    index.add_argument("--repo", type=Path, required=True)
    index.add_argument("--out", type=Path, required=True)
    index.add_argument("--mode", choices=("dev", "public", "demo"), default="dev")
    index.add_argument("--expected-commit")
    _retrieval_arguments(index)

    query = commands.add_parser("query", help="Export a versioned query context bundle.")
    source = query.add_mutually_exclusive_group(required=True)
    source.add_argument("--graph", type=Path)
    source.add_argument("--repo", type=Path)
    query.add_argument("--expected-commit")
    query.add_argument("--query", required=True)
    query.add_argument("--format", choices=("json", "markdown"), default="json")
    query.add_argument(
        "--include-source",
        action="append",
        default=[],
        metavar="NODE_ID",
        help="Include a reviewed excerpt for this ranked node; may be repeated.",
    )
    _retrieval_arguments(query)
    return parser


def _build_from_arguments(arguments: argparse.Namespace) -> KnowledgeGraph:
    assert arguments.repo is not None
    mode: GraphMode = "public" if arguments.expected_commit else "dev"
    return build_knowledge_graph(
        GraphConfig(
            repository_root=arguments.repo,
            mode=mode,
            expected_commit=arguments.expected_commit,
            retrieval=_retrieval_config(arguments),
        )
    )


def _run_index(arguments: argparse.Namespace) -> int:
    knowledge = build_knowledge_graph(
        GraphConfig(
            repository_root=arguments.repo,
            mode=cast(GraphMode, arguments.mode),
            expected_commit=arguments.expected_commit,
            retrieval=_retrieval_config(arguments),
        )
    )
    output = write_graph_artifact(knowledge, arguments.out)
    print(
        f"Indexed {len(knowledge.nodes)} nodes and {len(knowledge.edges)} edges at "
        f"{knowledge.snapshot.commit_sha}; wrote {output}",
        file=sys.stderr,
    )
    return 0


def _run_query(arguments: argparse.Namespace) -> int:
    query_text = str(arguments.query).strip()
    if not query_text:
        raise ValueError("--query must not be blank")
    if arguments.expected_commit is not None and not re.fullmatch(
        r"[0-9a-fA-F]{40}", arguments.expected_commit
    ):
        raise ValueError("--expected-commit must be a full 40-character Git SHA")
    if arguments.graph is not None:
        knowledge = read_graph_artifact(arguments.graph)
        if arguments.expected_commit is not None:
            expected_commit = arguments.expected_commit.lower()
            if knowledge.snapshot.commit_sha != expected_commit:
                raise ValueError(
                    f"Expected commit {expected_commit}, found {knowledge.snapshot.commit_sha}"
                )
            if knowledge.snapshot.dirty:
                raise ValueError("Expected-commit graph artifacts must record a clean worktree")
            if knowledge.snapshot.shallow:
                raise ValueError("Expected-commit graph artifacts must record complete Git history")
    else:
        knowledge = _build_from_arguments(arguments)
    retrieval = _retrieval_config(arguments)
    index = build_search_index(knowledge.nodes)
    result = query_graph(knowledge, index, query_text, config=retrieval)
    bundle = build_query_bundle(
        knowledge,
        result,
        retrieval=retrieval,
        include_source=bool(arguments.include_source),
        reviewed_snippet_node_ids=tuple(arguments.include_source),
    )
    content = (
        query_bundle_to_json(bundle)
        if arguments.format == "json"
        else query_bundle_to_markdown(bundle)
    )
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    parsed = parser.parse_args(arguments)
    try:
        if parsed.command == "index":
            return _run_index(parsed)
        if parsed.command == "query":
            return _run_query(parsed)
    except (FileNotFoundError, OSError, ValueError) as error:
        print(f"code-knowledge-graph: {error}", file=sys.stderr)
        return 2
    parser.error(f"unsupported command: {parsed.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
