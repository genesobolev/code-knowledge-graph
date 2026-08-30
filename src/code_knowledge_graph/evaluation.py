"""Versioned retrieval benchmark loading and deterministic metric calculation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

JudgmentRole = Literal["answer", "supporting"]


class BenchmarkError(ValueError):
    """Raised when a benchmark or submitted ranking is invalid."""


@dataclass(frozen=True)
class Judgment:
    """One manually reviewed relevant node for a benchmark question."""

    node_id: str
    role: JudgmentRole
    relevance: int


@dataclass(frozen=True)
class BenchmarkQuery:
    """One natural-language question and its reviewed judgments."""

    id: str
    query: str
    judgments: tuple[Judgment, ...]


@dataclass(frozen=True)
class BenchmarkRepository:
    """Public identity of the exact repository snapshot under evaluation."""

    name: str
    url: str
    commit: str


@dataclass(frozen=True)
class Benchmark:
    """A versioned set of manually reviewed retrieval questions."""

    schema_version: int
    repository: BenchmarkRepository
    review_status: str
    queries: tuple[BenchmarkQuery, ...]


@dataclass(frozen=True)
class JudgmentResult:
    """The retrieval outcome for one judgment at configured cutoffs."""

    node_id: str
    role: JudgmentRole
    relevance: int
    rank: int | None
    retrieved_at_10: bool
    retrieved_at_20: bool


@dataclass(frozen=True)
class QueryEvaluation:
    """Metrics and misses for one benchmark question."""

    id: str
    query: str
    reciprocal_answer_rank_at_10: float
    recall_at_10: float
    recall_at_20: float
    supporting_recall_at_10: float
    judgments: tuple[JudgmentResult, ...]
    missed_at_10: tuple[str, ...]
    missed_at_20: tuple[str, ...]


@dataclass(frozen=True)
class StrategyEvaluation:
    """Aggregate and per-query metrics for one retrieval strategy."""

    strategy: str
    answer_mrr_at_10: float
    recall_at_10: float
    recall_at_20: float
    supporting_recall_at_10: float
    queries: tuple[QueryEvaluation, ...]


@dataclass(frozen=True)
class QueryComparison:
    """A graph-expanded query outcome compared with its lexical baseline."""

    id: str
    answer_rank_change: int | None
    newly_retrieved_at_10: tuple[str, ...]
    newly_missed_at_10: tuple[str, ...]
    regression: bool


@dataclass(frozen=True)
class EvaluationComparison:
    """Equal-budget comparison without an inferred improvement claim."""

    schema_version: int
    repository: BenchmarkRepository
    ranking_budget: int
    metric_definition: str
    lexical: StrategyEvaluation
    graph_expanded: StrategyEvaluation
    queries: tuple[QueryComparison, ...]
    conclusion: str


def _require_object(value: object, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BenchmarkError(f"{location} must be an object")
    return cast(dict[str, Any], value)


def _require_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(f"{location} must be a non-empty string")
    return value


def load_benchmark(
    path: Path,
    *,
    available_node_ids: set[str] | None = None,
) -> Benchmark:
    """Load and validate a benchmark, optionally checking every judged node."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(f"Could not read benchmark {path}: {error}") from error
    root = _require_object(payload, "benchmark")
    if root.get("schema_version") != 1:
        raise BenchmarkError("benchmark.schema_version must be 1")

    raw_repository = _require_object(root.get("repository"), "benchmark.repository")
    commit = _require_string(raw_repository.get("commit"), "benchmark.repository.commit")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise BenchmarkError("benchmark.repository.commit must be a full lowercase Git SHA")
    repository = BenchmarkRepository(
        name=_require_string(raw_repository.get("name"), "benchmark.repository.name"),
        url=_require_string(raw_repository.get("url"), "benchmark.repository.url"),
        commit=commit,
    )
    review_status = _require_string(root.get("review_status"), "benchmark.review_status")

    raw_queries = root.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise BenchmarkError("benchmark.queries must be a non-empty array")
    queries: list[BenchmarkQuery] = []
    query_ids: set[str] = set()
    missing_nodes: set[str] = set()
    for query_index, raw_query_value in enumerate(raw_queries):
        location = f"benchmark.queries[{query_index}]"
        raw_query = _require_object(raw_query_value, location)
        query_id = _require_string(raw_query.get("id"), f"{location}.id")
        if query_id in query_ids:
            raise BenchmarkError(f"duplicate benchmark query id: {query_id}")
        query_ids.add(query_id)
        query_text = _require_string(raw_query.get("query"), f"{location}.query")
        raw_judgments = raw_query.get("judgments")
        if not isinstance(raw_judgments, list) or not raw_judgments:
            raise BenchmarkError(f"{location}.judgments must be a non-empty array")
        judgments: list[Judgment] = []
        judged_nodes: set[str] = set()
        for judgment_index, raw_judgment_value in enumerate(raw_judgments):
            judgment_location = f"{location}.judgments[{judgment_index}]"
            raw_judgment = _require_object(raw_judgment_value, judgment_location)
            node_id = _require_string(raw_judgment.get("node_id"), f"{judgment_location}.node_id")
            if node_id in judged_nodes:
                raise BenchmarkError(f"duplicate judgment for {query_id}: {node_id}")
            judged_nodes.add(node_id)
            role = raw_judgment.get("role")
            if role not in ("answer", "supporting"):
                raise BenchmarkError(f"{judgment_location}.role must be answer or supporting")
            relevance = raw_judgment.get("relevance")
            if type(relevance) is not int or relevance not in (1, 2, 3):
                raise BenchmarkError(f"{judgment_location}.relevance must be 1, 2, or 3")
            judgments.append(
                Judgment(node_id=node_id, role=cast(JudgmentRole, role), relevance=relevance)
            )
            if available_node_ids is not None and node_id not in available_node_ids:
                missing_nodes.add(node_id)
        if not any(judgment.role == "answer" for judgment in judgments):
            raise BenchmarkError(f"benchmark query {query_id} has no answer judgment")
        if not any(judgment.role == "supporting" for judgment in judgments):
            raise BenchmarkError(f"benchmark query {query_id} has no supporting judgment")
        queries.append(BenchmarkQuery(id=query_id, query=query_text, judgments=tuple(judgments)))
    if missing_nodes:
        formatted = ", ".join(sorted(missing_nodes))
        raise BenchmarkError(f"benchmark judgments reference missing nodes: {formatted}")
    return Benchmark(
        schema_version=1,
        repository=repository,
        review_status=review_status,
        queries=tuple(queries),
    )


def _validate_ranking(query_id: str, ranking: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(ranking)
    if any(not isinstance(node_id, str) or not node_id for node_id in normalized):
        raise BenchmarkError(f"ranking for {query_id} contains an invalid node id")
    if len(set(normalized)) != len(normalized):
        raise BenchmarkError(f"ranking for {query_id} contains duplicate nodes")
    return normalized


def evaluate_rankings(
    benchmark: Benchmark,
    rankings: Mapping[str, Sequence[str]],
    *,
    strategy: str,
) -> StrategyEvaluation:
    """Evaluate ranked node IDs with macro-averaged query metrics."""

    expected_ids = {query.id for query in benchmark.queries}
    if set(rankings) != expected_ids:
        missing = sorted(expected_ids - set(rankings))
        extra = sorted(set(rankings) - expected_ids)
        raise BenchmarkError(f"ranking query ids differ; missing={missing}, extra={extra}")
    query_results: list[QueryEvaluation] = []
    for query in benchmark.queries:
        if not any(judgment.role == "answer" for judgment in query.judgments):
            raise BenchmarkError(f"benchmark query {query.id} has no answer judgment")
        if not any(judgment.role == "supporting" for judgment in query.judgments):
            raise BenchmarkError(f"benchmark query {query.id} has no supporting judgment")
        ranking = _validate_ranking(query.id, rankings[query.id])
        rank_by_node = {node_id: index for index, node_id in enumerate(ranking, start=1)}
        answer_ranks = [
            rank_by_node[judgment.node_id]
            for judgment in query.judgments
            if judgment.role == "answer" and judgment.node_id in rank_by_node
        ]
        best_answer_rank = min(answer_ranks, default=None)
        reciprocal_rank = (
            1.0 / best_answer_rank
            if best_answer_rank is not None and best_answer_rank <= 10
            else 0.0
        )
        judgment_results = tuple(
            JudgmentResult(
                node_id=judgment.node_id,
                role=judgment.role,
                relevance=judgment.relevance,
                rank=rank_by_node.get(judgment.node_id),
                retrieved_at_10=rank_by_node.get(judgment.node_id, 11) <= 10,
                retrieved_at_20=rank_by_node.get(judgment.node_id, 21) <= 20,
            )
            for judgment in query.judgments
        )
        supporting = [result for result in judgment_results if result.role == "supporting"]
        query_results.append(
            QueryEvaluation(
                id=query.id,
                query=query.query,
                reciprocal_answer_rank_at_10=reciprocal_rank,
                recall_at_10=sum(result.retrieved_at_10 for result in judgment_results)
                / len(judgment_results),
                recall_at_20=sum(result.retrieved_at_20 for result in judgment_results)
                / len(judgment_results),
                supporting_recall_at_10=(
                    sum(result.retrieved_at_10 for result in supporting) / len(supporting)
                    if supporting
                    else 0.0
                ),
                judgments=judgment_results,
                missed_at_10=tuple(
                    result.node_id for result in judgment_results if not result.retrieved_at_10
                ),
                missed_at_20=tuple(
                    result.node_id for result in judgment_results if not result.retrieved_at_20
                ),
            )
        )
    count = len(query_results)
    return StrategyEvaluation(
        strategy=strategy,
        answer_mrr_at_10=sum(result.reciprocal_answer_rank_at_10 for result in query_results)
        / count,
        recall_at_10=sum(result.recall_at_10 for result in query_results) / count,
        recall_at_20=sum(result.recall_at_20 for result in query_results) / count,
        supporting_recall_at_10=sum(result.supporting_recall_at_10 for result in query_results)
        / count,
        queries=tuple(query_results),
    )


def _answer_rank(result: QueryEvaluation) -> int | None:
    ranks = [
        judgment.rank
        for judgment in result.judgments
        if judgment.role == "answer" and judgment.rank is not None
    ]
    return min(ranks, default=None)


def compare_rankings(
    benchmark: Benchmark,
    lexical_rankings: Mapping[str, Sequence[str]],
    graph_rankings: Mapping[str, Sequence[str]],
) -> EvaluationComparison:
    """Compare lexical and graph-expanded rankings with identical per-query budgets."""

    budgets: set[int] = set()
    for query in benchmark.queries:
        lexical = _validate_ranking(query.id, lexical_rankings.get(query.id, ()))
        graph = _validate_ranking(query.id, graph_rankings.get(query.id, ()))
        if len(lexical) != len(graph):
            raise BenchmarkError(
                f"unequal retrieval budgets for {query.id}: {len(lexical)} != {len(graph)}"
            )
        budgets.add(len(lexical))
    if len(budgets) != 1:
        raise BenchmarkError("every query must use one fixed retrieval budget")
    budget = next(iter(budgets))
    if budget < 20:
        raise BenchmarkError("evaluation ranking budget must be at least 20")

    lexical_result = evaluate_rankings(benchmark, lexical_rankings, strategy="lexical")
    graph_result = evaluate_rankings(benchmark, graph_rankings, strategy="graph_expanded")
    comparisons: list[QueryComparison] = []
    for lexical_query, graph_query in zip(
        lexical_result.queries, graph_result.queries, strict=True
    ):
        lexical_retrieved = {
            judgment.node_id for judgment in lexical_query.judgments if judgment.retrieved_at_10
        }
        graph_retrieved = {
            judgment.node_id for judgment in graph_query.judgments if judgment.retrieved_at_10
        }
        lexical_answer_rank = _answer_rank(lexical_query)
        graph_answer_rank = _answer_rank(graph_query)
        if lexical_answer_rank is None or graph_answer_rank is None:
            rank_change = None
        else:
            rank_change = lexical_answer_rank - graph_answer_rank
        regression = (
            lexical_answer_rank is not None
            and (graph_answer_rank is None or graph_answer_rank > lexical_answer_rank)
        ) or bool(lexical_retrieved - graph_retrieved)
        comparisons.append(
            QueryComparison(
                id=lexical_query.id,
                answer_rank_change=rank_change,
                newly_retrieved_at_10=tuple(sorted(graph_retrieved - lexical_retrieved)),
                newly_missed_at_10=tuple(sorted(lexical_retrieved - graph_retrieved)),
                regression=regression,
            )
        )
    delta = graph_result.answer_mrr_at_10 - lexical_result.answer_mrr_at_10
    if delta > 0:
        conclusion = "Graph expansion has higher answer MRR@10 on this illustrative benchmark."
    elif delta < 0:
        conclusion = "Graph expansion has lower answer MRR@10 on this illustrative benchmark."
    else:
        conclusion = "Graph expansion and lexical retrieval tie on answer MRR@10."
    return EvaluationComparison(
        schema_version=1,
        repository=benchmark.repository,
        ranking_budget=budget,
        metric_definition=(
            "Unweighted macro mean across reviewed queries; answer MRR uses the best-ranked answer "
            "judgment and recall treats all reviewed judgments equally."
        ),
        lexical=lexical_result,
        graph_expanded=graph_result,
        queries=tuple(comparisons),
        conclusion=conclusion,
    )


def evaluation_to_json(comparison: EvaluationComparison) -> str:
    """Serialize an evaluation comparison deterministically."""

    return json.dumps(asdict(comparison), indent=2, sort_keys=True) + "\n"


def evaluation_to_markdown(comparison: EvaluationComparison) -> str:
    """Render the same evaluation comparison as concise Markdown."""

    lines = [
        "# Retrieval evaluation",
        "",
        (
            f"Repository: `{comparison.repository.name}` at "
            f"`{comparison.repository.commit}`. Ranking budget: {comparison.ranking_budget}."
        ),
        "",
        comparison.metric_definition,
        "",
        "| Strategy | Answer MRR@10 | Recall@10 | Recall@20 | Supporting recall@10 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for strategy_result in (comparison.lexical, comparison.graph_expanded):
        lines.append(
            f"| {strategy_result.strategy} | {strategy_result.answer_mrr_at_10:.3f} | "
            f"{strategy_result.recall_at_10:.3f} | {strategy_result.recall_at_20:.3f} | "
            f"{strategy_result.supporting_recall_at_10:.3f} |"
        )
    lines.extend(["", comparison.conclusion, "", "## Per-query changes", ""])
    for query_result in comparison.queries:
        change = (
            "not comparable"
            if query_result.answer_rank_change is None
            else str(query_result.answer_rank_change)
        )
        lines.extend(
            [
                f"### {query_result.id}",
                "",
                f"- Answer rank change, positive is better: {change}",
                "- Newly retrieved at 10: "
                f"{', '.join(query_result.newly_retrieved_at_10) or 'none'}",
                f"- Newly missed at 10: {', '.join(query_result.newly_missed_at_10) or 'none'}",
                f"- Regression: {'yes' if query_result.regression else 'no'}",
                "",
            ]
        )
    return "\n".join(lines)
