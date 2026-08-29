from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

TARGET_REPOSITORY = Path("/home/user/projects/implicit-decision-gate")
NOTEBOOK_PATH = Path(__file__).parents[1] / "knowledge_code_graph.ipynb"


def read_notebook() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8")),
    )


def notebook_source(notebook: Mapping[str, Any]) -> str:
    return "\n\n".join(
        "".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"
    )


def execute_notebook(monkeypatch: pytest.MonkeyPatch) -> Mapping[str, Any]:
    module = ModuleType("executed_code_knowledge_graph_notebook")
    module.__file__ = str(NOTEBOOK_PATH)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    source = notebook_source(read_notebook())
    exec(compile(source, str(NOTEBOOK_PATH), "exec"), module.__dict__)
    return module.__dict__


def test_notebook_is_canonical_and_has_portable_plotly_outputs() -> None:
    notebook = read_notebook()

    assert "jupytext" not in notebook["metadata"]
    assert not NOTEBOOK_PATH.with_suffix(".py").exists()

    outputs = [output for cell in notebook["cells"] for output in cell.get("outputs", [])]
    plotly_outputs = [
        output for output in outputs if "application/vnd.plotly.v1+json" in output.get("data", {})
    ]
    serialized_outputs = json.dumps(outputs)

    assert len(plotly_outputs) >= 2
    assert all(output.get("output_type") != "error" for output in outputs)
    assert "<iframe" not in serialized_outputs
    assert 'src="artifacts/' not in serialized_outputs


@pytest.mark.skipif(
    not TARGET_REPOSITORY.is_dir(),
    reason="the configured target repository isn't available",
)
def test_notebook_pipeline_builds_and_queries_target_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    namespace = execute_notebook(monkeypatch)

    knowledge = namespace["knowledge"]
    result = namespace["result"]

    assert len(knowledge.nodes) > 200
    assert len(knowledge.edges) > 500
    assert not knowledge.issues
    assert any(node.language == "javascript" for node in knowledge.nodes.values())
    assert {"CALLS", "IMPORTS", "CO_CHANGES"} <= {edge.kind for edge in knowledge.edges}
    assert not result.relevant.empty
    assert not result.related.empty
    assert result.related["relationship_strength"].is_monotonic_decreasing
    assert (tmp_path / "artifacts" / "repository_overview.html").is_file()
    assert (tmp_path / "artifacts" / "query_graph.html").is_file()
