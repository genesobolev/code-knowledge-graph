# Code knowledge graph notebook

This project builds a local, interactive code knowledge graph for
`/home/user/projects/implicit-decision-gate`. It doesn't use Oracle or execute code from
the target repository.

The graph includes Python files and symbols, other tracked source files, typed static
relationships, and Git co-change evidence. Natural-language queries return text-relevant
anchors and graph-related nodes ordered by explicit relationship strength. The notebook
renders both repository-level and query-focused interactive Plotly graphs. Its rich MIME
output works in VS Code notebooks and JupyterLab without loading a relative iframe.

## Run it

```bash
UV_CACHE_DIR=/tmp/code-knowledge-graph-uv-cache uv sync --group dev
UV_CACHE_DIR=/tmp/code-knowledge-graph-uv-cache uv run jupyter lab knowledge_code_graph.ipynb
```

Set `CODE_GRAPH_TARGET` before launching Jupyter to index a different repository.

The [notebook](knowledge_code_graph.ipynb) is the canonical source. It isn't paired with a
second text representation, so selecting or saving a kernel can't create a Jupytext load
conflict. Standalone, self-contained Plotly HTML files are written under `artifacts/`.
