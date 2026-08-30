from __future__ import annotations

import json
from base64 import b64decode
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

NOTEBOOK_PATH = Path(__file__).parents[1] / "knowledge_code_graph.ipynb"


def read_notebook() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8")))


def notebook_source(notebook: Mapping[str, Any]) -> str:
    return "\n\n".join(
        "".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"
    )


def test_notebook_is_a_portable_import_based_walkthrough() -> None:
    notebook = read_notebook()
    source = notebook_source(notebook)
    outputs = [output for cell in notebook["cells"] for output in cell.get("outputs", [])]
    graph_outputs = [
        output
        for output in outputs
        if {"application/vnd.plotly.v1+json", "image/png"} <= output.get("data", {}).keys()
    ]
    graph_pngs = [b64decode(output["data"]["image/png"]) for output in graph_outputs]
    serialized = json.dumps(notebook)

    assert notebook["nbformat"] == 4
    assert "from code_knowledge_graph import" in source
    assert "GraphConfig.resolve" in source
    assert "build_knowledge_graph(config)" in source
    assert "build_search_index" in source
    assert "query_graph" in source
    assert "repository_overview_figure" in source
    assert "query_result_figure" in source
    assert "display_graph(overview_figure, width=1340, height=720)" in source
    assert "display_graph(query_figure, width=1340, height=760)" in source
    assert "class CodeNode" not in source
    assert "def parse_repository" not in source
    assert "/home/" not in serialized
    assert "<iframe" not in serialized
    assert len(graph_outputs) == 2
    assert all(png.startswith(b"\x89PNG\r\n\x1a\n") for png in graph_pngs)
    assert all(len(png) > 10_000 for png in graph_pngs)
    assert all(output.get("output_type") != "error" for output in outputs)
