"""Generate deterministic static data from the clean pinned demonstration snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path

from code_knowledge_graph.configuration import GraphConfig, RetrievalConfig
from code_knowledge_graph.context import build_query_bundle
from code_knowledge_graph.evaluation import (
    Benchmark,
    compare_rankings,
    load_benchmark,
)
from code_knowledge_graph.extraction import build_knowledge_graph
from code_knowledge_graph.retrieval import build_search_index, direct_query_scores, query_graph
from code_knowledge_graph.web_export import (
    evaluation_payload,
    manifest_payload,
    query_payload,
    repository_payload,
    verify_public_snapshot,
    write_public_dataset,
)

EXPECTED_COMMIT = "2fdbb3deab2967a545d8a898a17a380974e6bb17"
DEFAULT_BENCHMARK = Path("benchmarks/implicit-decision-gate-v1.json")
DEFAULT_OUTPUT = Path("web/public/data")
REPOSITORY_ID = "implicit-decision-gate"
RANKING_BUDGET = 20


def lexical_ranking(scores: dict[str, float]) -> list[str]:
    """Return the deterministic direct-score baseline at the fixed budget."""

    return sorted(scores, key=lambda node_id: (-scores[node_id], node_id))[:RANKING_BUDGET]


def graph_ranking(
    anchors: tuple[str, ...], related_ids: list[str], lexical: list[str]
) -> list[str]:
    """Fill the same budget with anchors, graph-expanded nodes, then direct fallbacks."""

    ranking = list(dict.fromkeys((*anchors, *related_ids)))
    ranking.extend(node_id for node_id in lexical if node_id not in ranking)
    return ranking[:RANKING_BUDGET]


def build_data(
    repository_path: Path,
    benchmark_path: Path,
    output_path: Path,
) -> None:
    retrieval = RetrievalConfig(seed_count=6, hops=2, related_count=25, hop_decay=0.85)
    knowledge = build_knowledge_graph(
        GraphConfig(
            repository_root=repository_path,
            mode="public",
            expected_commit=EXPECTED_COMMIT,
            retrieval=retrieval,
        )
    )
    verify_public_snapshot(knowledge, EXPECTED_COMMIT)
    benchmark: Benchmark = load_benchmark(benchmark_path, available_node_ids=set(knowledge.nodes))
    if benchmark.repository.commit != EXPECTED_COMMIT:
        raise ValueError("benchmark commit doesn't match the configured public snapshot")

    index = build_search_index(knowledge.nodes)
    lexical_rankings: dict[str, list[str]] = {}
    graph_rankings: dict[str, list[str]] = {}
    entries: list[dict[str, str]] = []
    bundles = []
    payloads: dict[str, object] = {}
    for benchmark_query in benchmark.queries:
        scores = direct_query_scores(index, benchmark_query.query)
        lexical = lexical_ranking(scores)
        result = query_graph(knowledge, index, benchmark_query.query, config=retrieval)
        related_ids = [str(node_id) for node_id in result.related["node_id"].tolist()]
        graph = graph_ranking(result.anchors, related_ids, lexical)
        if len(lexical) != RANKING_BUDGET or len(graph) != RANKING_BUDGET:
            raise ValueError(f"query {benchmark_query.id} couldn't fill the evaluation budget")
        lexical_rankings[benchmark_query.id] = lexical
        graph_rankings[benchmark_query.id] = graph

        ranked_ids = set(result.anchors) | set(related_ids)
        reviewed_answers = tuple(
            judgment.node_id
            for judgment in benchmark_query.judgments
            if judgment.role == "answer" and judgment.node_id in ranked_ids
        )[:1]
        bundle = build_query_bundle(
            knowledge,
            result,
            retrieval=retrieval,
            include_source=bool(reviewed_answers),
            reviewed_snippet_node_ids=reviewed_answers,
        )
        bundles.append(bundle)
        label = benchmark_query.id.replace("-", " ").title()
        file_name = f"queries/{benchmark_query.id}.json"
        entries.append({"id": benchmark_query.id, "label": label, "file": file_name})
        payloads[file_name] = query_payload(
            knowledge,
            bundle,
            query_id=benchmark_query.id,
            label=label,
            description=(
                "Recorded retrieval for a manually reviewed benchmark question with "
                "answer and supporting-context judgments."
            ),
        )

    comparison = compare_rankings(benchmark, lexical_rankings, graph_rankings)
    provenance = {
        "repository": knowledge.snapshot.repository_name,
        "commit": knowledge.snapshot.commit_sha,
        "tree": knowledge.snapshot.tree_sha,
        "branch": knowledge.snapshot.branch,
        "detached": knowledge.snapshot.detached,
        "clean": True,
        "shallow": knowledge.snapshot.shallow,
        "schema_version": knowledge.snapshot.schema_version,
        "extractor_version": knowledge.snapshot.extractor_version,
        "indexed_source_sha256": knowledge.snapshot.indexed_source_sha256,
    }
    payloads["repository.json"] = repository_payload(knowledge, repository_id=REPOSITORY_ID)
    payloads["evaluation.json"] = evaluation_payload(comparison, provenance=provenance)
    payloads["manifest.json"] = manifest_payload(
        bundles[0],
        entries,
        repository_id=REPOSITORY_ID,
        repository_label="Implicit Decision Gate",
    )
    write_public_dataset(output_path, payloads)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    build_data(arguments.repository, arguments.benchmark, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
