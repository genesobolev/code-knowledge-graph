from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from code_knowledge_graph.evaluation import (
    Benchmark,
    BenchmarkError,
    BenchmarkQuery,
    BenchmarkRepository,
    Judgment,
    compare_rankings,
    evaluate_rankings,
    evaluation_to_json,
    evaluation_to_markdown,
    load_benchmark,
)

BENCHMARK_PATH = Path(__file__).parents[1] / "benchmarks" / "implicit-decision-gate-v1.json"


def small_benchmark() -> Benchmark:
    return Benchmark(
        schema_version=1,
        repository=BenchmarkRepository(
            name="example",
            url="https://example.test/repository",
            commit="1" * 40,
        ),
        review_status="reviewed",
        queries=(
            BenchmarkQuery(
                id="q1",
                query="first question",
                judgments=(
                    Judgment("answer:one", "answer", 3),
                    Judgment("support:one", "supporting", 2),
                ),
            ),
            BenchmarkQuery(
                id="q2",
                query="second question",
                judgments=(
                    Judgment("answer:two", "answer", 3),
                    Judgment("support:two", "supporting", 2),
                ),
            ),
        ),
    )


def padded(*node_ids: str) -> list[str]:
    return [*node_ids, *(f"irrelevant:{index}" for index in range(20 - len(node_ids)))]


def test_versioned_benchmark_has_manually_reviewed_answer_and_supporting_judgments() -> None:
    benchmark = load_benchmark(BENCHMARK_PATH)

    assert benchmark.schema_version == 1
    assert benchmark.repository.commit == "2fdbb3deab2967a545d8a898a17a380974e6bb17"
    assert 12 <= len(benchmark.queries) <= 15
    assert all(
        any(item.role == "answer" for item in query.judgments) for query in benchmark.queries
    )
    assert all(
        any(item.role == "supporting" for item in query.judgments) for query in benchmark.queries
    )


def test_metric_formulas_use_answer_rank_and_macro_recall() -> None:
    rankings = {
        "q1": padded("answer:one", "support:one"),
        "q2": padded("irrelevant:special", "answer:two"),
    }

    result = evaluate_rankings(small_benchmark(), rankings, strategy="lexical")

    assert result.answer_mrr_at_10 == pytest.approx(0.75)
    assert result.recall_at_10 == pytest.approx(0.75)
    assert result.recall_at_20 == pytest.approx(0.75)
    assert result.supporting_recall_at_10 == pytest.approx(0.5)
    assert result.queries[1].missed_at_10 == ("support:two",)


def test_rankings_must_be_deduplicated_and_use_equal_fixed_budgets() -> None:
    duplicate = padded("answer:one")
    duplicate[-1] = "answer:one"
    with pytest.raises(BenchmarkError, match="duplicate nodes"):
        evaluate_rankings(
            small_benchmark(),
            {"q1": duplicate, "q2": padded("answer:two")},
            strategy="lexical",
        )

    with pytest.raises(BenchmarkError, match="unequal retrieval budgets"):
        compare_rankings(
            small_benchmark(),
            {"q1": padded("answer:one"), "q2": padded("answer:two")},
            {"q1": padded("answer:one")[:-1], "q2": padded("answer:two")},
        )


def test_comparison_reports_query_regressions_without_overclaiming() -> None:
    lexical = {
        "q1": padded("answer:one", "support:one"),
        "q2": padded("answer:two", "support:two"),
    }
    graph = {
        "q1": padded("support:one", "answer:one"),
        "q2": padded("answer:two", "support:two"),
    }

    comparison = compare_rankings(small_benchmark(), lexical, graph)

    assert comparison.queries[0].answer_rank_change == -1
    assert comparison.queries[0].regression is True
    assert "lower answer MRR@10" in comparison.conclusion
    assert "illustrative benchmark" in comparison.conclusion


def test_evaluation_serialization_is_deterministic_and_shared_with_markdown() -> None:
    rankings = {
        "q1": padded("answer:one", "support:one"),
        "q2": padded("answer:two", "support:two"),
    }
    comparison = compare_rankings(small_benchmark(), rankings, rankings)

    first_json = evaluation_to_json(comparison)
    second_json = evaluation_to_json(comparison)
    markdown = evaluation_to_markdown(comparison)

    assert first_json == second_json
    assert json.loads(first_json)["lexical"]["answer_mrr_at_10"] == 1.0
    assert "| lexical | 1.000 |" in markdown
    assert "Graph expansion and lexical retrieval tie" in markdown


def test_malformed_judgments_and_missing_expected_nodes_are_rejected(tmp_path: Path) -> None:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["queries"][0]["judgments"][0]["role"] = "maybe"
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkError, match="answer or supporting"):
        load_benchmark(malformed)

    benchmark = load_benchmark(BENCHMARK_PATH)
    judged = {judgment.node_id for query in benchmark.queries for judgment in query.judgments}
    with pytest.raises(BenchmarkError, match="missing nodes"):
        load_benchmark(BENCHMARK_PATH, available_node_ids=judged - {next(iter(judged))})

    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["queries"][0]["judgments"] = [
        judgment for judgment in payload["queries"][0]["judgments"] if judgment["role"] == "answer"
    ]
    malformed.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="no supporting judgment"):
        load_benchmark(malformed)


def test_every_query_requires_an_answer_judgment() -> None:
    query = small_benchmark().queries[0]
    without_answer = replace(
        small_benchmark(),
        queries=(replace(query, judgments=(Judgment("support:one", "supporting", 2),)),),
    )

    with pytest.raises(BenchmarkError, match="no answer judgment"):
        evaluate_rankings(without_answer, {"q1": padded("support:one")}, strategy="test")


def test_every_query_requires_a_supporting_judgment() -> None:
    query = small_benchmark().queries[0]
    without_support = replace(
        small_benchmark(),
        queries=(replace(query, judgments=(Judgment("answer:one", "answer", 3),)),),
    )

    with pytest.raises(BenchmarkError, match="no supporting judgment"):
        evaluate_rankings(without_support, {"q1": padded("answer:one")}, strategy="test")
