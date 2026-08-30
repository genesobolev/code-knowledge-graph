"""Load a frozen query bundle and construct an agent-ready context message."""

from __future__ import annotations

import argparse
from pathlib import Path

from code_knowledge_graph.context import (
    QUERY_BUNDLE_SCHEMA_VERSION,
    QueryBundle,
    load_query_bundle,
    query_bundle_to_markdown,
)


def agent_context_message(bundle: QueryBundle) -> str:
    """Construct a local prompt section without an LLM or network call."""

    if bundle.schema_version != QUERY_BUNDLE_SCHEMA_VERSION:
        raise ValueError(f"unsupported query bundle schema: {bundle.schema_version}")
    return (
        "Use the following repository context to answer the query. Treat source locations and "
        "relationship evidence as navigation aids, and verify code before changing it.\n\n"
        f"{query_bundle_to_markdown(bundle)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Path to a query-bundle-v1 JSON file")
    arguments = parser.parse_args()
    print(agent_context_message(load_query_bundle(arguments.bundle)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
