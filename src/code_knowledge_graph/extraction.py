from __future__ import annotations

import ast
import importlib.util
import itertools
import math
import os
import subprocess
import tokenize
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd

from .configuration import GraphConfig
from .models import CodeEdge, CodeNode, KnowledgeGraph, ParseIssue
from .snapshot import create_repository_snapshot, validate_source_path, verify_repository_snapshot

EXCLUDED_DIRECTORIES = {
    ".git",
    ".idg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
SOURCE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".mjs",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
LANGUAGE_BY_SUFFIX: Mapping[str, str] = {
    ".css": "css",
    ".html": "html",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".py": "python",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".yaml": "yaml",
    ".yml": "yaml",
}
EDGE_PRIORS: Mapping[str, float] = {
    "CALLS": 1.00,
    "INHERITS": 0.95,
    "INSTANTIATES": 0.90,
    "TESTS": 0.85,
    "CO_CHANGES": 0.80,
    "IMPORTS": 0.65,
    "CONTAINS": 0.45,
}


@dataclass(frozen=True)
class RawRelation:
    source_id: str
    target_id: str
    kind: str
    confidence: float
    evidence: str
    count: int = 1


@dataclass(frozen=True)
class PendingRelation:
    source_id: str
    target_text: str
    kind: str
    confidence: float
    evidence: str


@dataclass(frozen=True)
class ImportBinding:
    kind: str
    module: str
    symbol: str = ""


@dataclass
class ExtractionState:
    nodes: dict[str, CodeNode] = field(default_factory=dict)
    raw_relations: list[RawRelation] = field(default_factory=list)
    pending_relations: list[PendingRelation] = field(default_factory=list)
    aliases_by_path: dict[str, dict[str, ImportBinding]] = field(default_factory=dict)
    issues: list[ParseIssue] = field(default_factory=list)


def discover_source_files(repository: Path) -> tuple[Path, ...]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "ls-files",
            "-co",
            "--exclude-standard",
            "-z",
        ],
        check=False,
        capture_output=True,
        text=False,
    )
    if completed.returncode == 0:
        relative_paths = [
            Path(item.decode("utf-8", errors="surrogateescape"))
            for item in completed.stdout.split(b"\0")
            if item
        ]
        source_files = []
        for relative_path in relative_paths:
            if relative_path.suffix.lower() not in SOURCE_SUFFIXES or any(
                part in EXCLUDED_DIRECTORIES for part in relative_path.parts
            ):
                continue
            path = repository / relative_path
            validate_source_path(repository, path)
            if path.is_file():
                source_files.append(path)
        return tuple(sorted(source_files))

    source_files = []
    for root, directories, file_names in os.walk(repository, followlinks=False):
        root_path = Path(root)
        directories[:] = sorted(
            name
            for name in directories
            if name not in EXCLUDED_DIRECTORIES and not (root_path / name).is_symlink()
        )
        for file_name in sorted(file_names):
            path = root_path / file_name
            relative_path = path.relative_to(repository)
            if path.suffix.lower() not in SOURCE_SUFFIXES or any(
                part in EXCLUDED_DIRECTORIES for part in relative_path.parts
            ):
                continue
            validate_source_path(repository, path)
            if path.is_file():
                source_files.append(path)
    return tuple(sorted(source_files))


def module_name_for_path(relative_path: Path) -> str:
    parts = list(relative_path.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or relative_path.stem


def dotted_name(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        parent = dotted_name(expression.value)
        return f"{parent}.{expression.attr}" if parent else expression.attr
    return None


def function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix}{node.name}({ast.unparse(node.args)}){returns}"


def class_signature(node: ast.ClassDef) -> str:
    bases = ", ".join(ast.unparse(base) for base in node.bases)
    return f"class {node.name}({bases})" if bases else f"class {node.name}"


def resolve_relative_module(
    current_module: str,
    relative_path: Path,
    imported_module: str | None,
    level: int,
) -> str:
    if level == 0:
        return imported_module or ""
    package = (
        current_module if relative_path.stem == "__init__" else current_module.rpartition(".")[0]
    )
    request = "." * level + (imported_module or "")
    if not package:
        return imported_module or ""
    try:
        return importlib.util.resolve_name(request, package)
    except (ImportError, ValueError):
        return imported_module or ""


class PythonFileExtractor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        repository: Path,
        path: Path,
        source: str,
        state: ExtractionState,
    ) -> None:
        self.repository = repository
        self.path = path
        self.relative_path = path.relative_to(repository)
        self.path_text = self.relative_path.as_posix()
        self.source = source
        self.lines = source.splitlines()
        self.state = state
        self.module = module_name_for_path(self.relative_path)
        self.file_id = f"file:{self.path_text}"
        self.scope: list[tuple[str, str, str]] = []
        self.aliases: dict[str, ImportBinding] = {}

    def extract(self, tree: ast.Module) -> None:
        self.state.nodes[self.file_id] = CodeNode(
            id=self.file_id,
            kind="file",
            name=self.relative_path.name,
            qualified_name=self.module,
            path=self.path_text,
            module=self.module,
            language="python",
            start_line=1,
            end_line=max(1, len(self.lines)),
            docstring=ast.get_docstring(tree, clean=True) or "",
            source=self.source,
        )
        self.visit(tree)
        self.state.aliases_by_path[self.path_text] = self.aliases

    @property
    def current_source_id(self) -> str:
        return self.scope[-1][2] if self.scope else self.file_id

    @property
    def current_qualified_name(self) -> str:
        return ".".join(name for name, _kind, _node_id in self.scope)

    def source_segment(self, node: ast.AST) -> str:
        start = max(1, getattr(node, "lineno", 1))
        end = max(start, getattr(node, "end_lineno", start))
        return "\n".join(self.lines[start - 1 : end])

    def add_symbol(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: str,
        signature: str,
    ) -> tuple[str, str]:
        parent_id = self.current_source_id
        qualified_name = ".".join(
            [part for part in (self.current_qualified_name, node.name) if part]
        )
        node_id = f"symbol:{self.path_text}::{qualified_name}"
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start)
        self.state.nodes[node_id] = CodeNode(
            id=node_id,
            kind=kind,
            name=node.name,
            qualified_name=qualified_name,
            path=self.path_text,
            module=self.module,
            language="python",
            start_line=start,
            end_line=end,
            signature=signature,
            docstring=ast.get_docstring(node, clean=True) or "",
            source=self.source_segment(node),
        )
        self.state.raw_relations.append(
            RawRelation(
                source_id=parent_id,
                target_id=node_id,
                kind="CONTAINS",
                confidence=1.0,
                evidence=f"{self.path_text}:{start}",
            )
        )
        return node_id, qualified_name

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        node_id, _qualified_name = self.add_symbol(node, "class", class_signature(node))
        for base in node.bases:
            target = dotted_name(base)
            if target:
                self.state.pending_relations.append(
                    PendingRelation(
                        source_id=node_id,
                        target_text=target,
                        kind="INHERITS",
                        confidence=1.0,
                        evidence=f"{self.path_text}:{node.lineno}",
                    )
                )
        self.scope.append((node.name, "class", node_id))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent_kind = self.scope[-1][1] if self.scope else ""
        is_test = self.path_text.startswith("tests/") and node.name.startswith("test_")
        decorator_names = {dotted_name(decorator) for decorator in node.decorator_list}
        is_fixture = any(name and name.endswith("fixture") for name in decorator_names)
        if is_test:
            kind = "test"
        elif is_fixture:
            kind = "fixture"
        elif parent_kind == "class":
            kind = "method"
        else:
            kind = "function"
        node_id, _qualified_name = self.add_symbol(node, kind, function_signature(node))
        self.scope.append((node.name, kind, node_id))
        self.generic_visit(node)
        self.scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".")[0]
            bound_module = alias.name if alias.asname else alias.name.split(".")[0]
            self.aliases[bound_name] = ImportBinding("module", bound_module)
            self.state.pending_relations.append(
                PendingRelation(
                    source_id=self.file_id,
                    target_text=f"@module:{alias.name}",
                    kind="IMPORTS",
                    confidence=1.0,
                    evidence=f"{self.path_text}:{node.lineno}",
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = resolve_relative_module(
            self.module,
            self.relative_path,
            node.module,
            node.level,
        )
        if module:
            self.state.pending_relations.append(
                PendingRelation(
                    source_id=self.file_id,
                    target_text=f"@module:{module}",
                    kind="IMPORTS",
                    confidence=1.0,
                    evidence=f"{self.path_text}:{node.lineno}",
                )
            )
        for alias in node.names:
            if alias.name == "*":
                continue
            bound_name = alias.asname or alias.name
            self.aliases[bound_name] = ImportBinding("symbol", module, alias.name)

    def visit_Call(self, node: ast.Call) -> None:
        target = dotted_name(node.func)
        if target:
            self.state.pending_relations.append(
                PendingRelation(
                    source_id=self.current_source_id,
                    target_text=target,
                    kind="CALLS",
                    confidence=1.0,
                    evidence=f"{self.path_text}:{node.lineno}",
                )
            )
        self.generic_visit(node)


def parse_repository(
    repository: Path,
    source_files: Sequence[Path] | None = None,
) -> ExtractionState:
    state = ExtractionState()
    files = discover_source_files(repository) if source_files is None else source_files
    for path in files:
        relative_path = validate_source_path(repository, path).as_posix()
        try:
            with tokenize.open(path) as stream:
                source = stream.read()
            if path.suffix.lower() == ".py":
                tree = ast.parse(source, filename=relative_path, type_comments=True)
                PythonFileExtractor(
                    repository=repository,
                    path=path,
                    source=source,
                    state=state,
                ).extract(tree)
            else:
                relative = path.relative_to(repository)
                path_text = relative.as_posix()
                file_id = f"file:{path_text}"
                state.nodes[file_id] = CodeNode(
                    id=file_id,
                    kind="file",
                    name=relative.name,
                    qualified_name=module_name_for_path(relative),
                    path=path_text,
                    module=module_name_for_path(relative),
                    language=LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text"),
                    start_line=1,
                    end_line=max(1, len(source.splitlines())),
                    source=source,
                )
        except (OSError, SyntaxError, UnicodeError) as exc:
            state.issues.append(ParseIssue(relative_path, str(exc)))
    return state


def module_and_symbol_indexes(
    nodes: Mapping[str, CodeNode],
) -> tuple[
    dict[str, str],
    dict[tuple[str, str], str],
    dict[tuple[str, str], list[str]],
    dict[str, list[str]],
]:
    module_to_file = {node.module: node.id for node in nodes.values() if node.kind == "file"}
    exact_symbols: dict[tuple[str, str], str] = {}
    module_short_names: dict[tuple[str, str], list[str]] = defaultdict(list)
    global_short_names: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        if node.kind == "file":
            continue
        exact_symbols[(node.module, node.qualified_name)] = node.id
        module_short_names[(node.module, node.name)].append(node.id)
        global_short_names[node.name].append(node.id)
    return module_to_file, exact_symbols, module_short_names, global_short_names


def enclosing_class_name(
    source: CodeNode,
    exact_symbols: Mapping[tuple[str, str], str],
    nodes: Mapping[str, CodeNode],
) -> str | None:
    parts = source.qualified_name.split(".")
    for end in range(len(parts) - 1, 0, -1):
        candidate_name = ".".join(parts[:end])
        candidate_id = exact_symbols.get((source.module, candidate_name))
        if candidate_id and nodes[candidate_id].kind == "class":
            return candidate_name
    return None


def resolve_symbol_text(
    *,
    source: CodeNode,
    target_text: str,
    nodes: Mapping[str, CodeNode],
    aliases: Mapping[str, ImportBinding],
    module_to_file: Mapping[str, str],
    exact_symbols: Mapping[tuple[str, str], str],
    module_short_names: Mapping[tuple[str, str], list[str]],
    global_short_names: Mapping[str, list[str]],
) -> tuple[str, float] | None:
    if target_text.startswith("@module:"):
        module = target_text.removeprefix("@module:")
        target_id = module_to_file.get(module)
        return (target_id, 1.0) if target_id else None

    parts = target_text.split(".")
    root = parts[0]
    remainder = ".".join(parts[1:])

    if root in {"self", "cls"} and remainder:
        class_name = enclosing_class_name(source, exact_symbols, nodes)
        if class_name:
            target_id = exact_symbols.get((source.module, f"{class_name}.{remainder}"))
            if target_id:
                return target_id, 0.95

    binding = aliases.get(root)
    if binding:
        if binding.kind == "module":
            candidate_module = binding.module
            candidate_symbol = remainder
        else:
            candidate_module = binding.module
            candidate_symbol = ".".join(part for part in (binding.symbol, remainder) if part)
        if candidate_symbol:
            target_id = exact_symbols.get((candidate_module, candidate_symbol))
            if target_id:
                return target_id, 0.95
            candidates = module_short_names.get(
                (candidate_module, candidate_symbol.rsplit(".", 1)[-1]), []
            )
            if len(candidates) == 1:
                return candidates[0], 0.85
        elif candidate_module in module_to_file:
            return module_to_file[candidate_module], 0.95

    if "." in target_text:
        target_id = exact_symbols.get((source.module, target_text))
        if target_id:
            return target_id, 0.95
    else:
        class_name = enclosing_class_name(source, exact_symbols, nodes)
        if class_name:
            target_id = exact_symbols.get((source.module, f"{class_name}.{target_text}"))
            if target_id:
                return target_id, 0.95
        candidates = module_short_names.get((source.module, target_text), [])
        top_level = [
            candidate for candidate in candidates if "." not in nodes[candidate].qualified_name
        ]
        if len(top_level) == 1:
            return top_level[0], 0.95
        if len(candidates) == 1:
            return candidates[0], 0.85

    for module in sorted(module_to_file, key=len, reverse=True):
        prefix = f"{module}."
        if target_text.startswith(prefix):
            target_id = exact_symbols.get((module, target_text.removeprefix(prefix)))
            if target_id:
                return target_id, 0.90

    candidates = global_short_names.get(parts[-1], [])
    if len(candidates) == 1:
        return candidates[0], 0.60
    return None


def resolve_pending_relations(state: ExtractionState) -> tuple[list[RawRelation], Counter[str]]:
    module_to_file, exact_symbols, module_short_names, global_short_names = (
        module_and_symbol_indexes(state.nodes)
    )
    resolved = list(state.raw_relations)
    counts: Counter[str] = Counter()
    for relation in state.pending_relations:
        source = state.nodes[relation.source_id]
        aliases = state.aliases_by_path.get(source.path, {})
        match = resolve_symbol_text(
            source=source,
            target_text=relation.target_text,
            nodes=state.nodes,
            aliases=aliases,
            module_to_file=module_to_file,
            exact_symbols=exact_symbols,
            module_short_names=module_short_names,
            global_short_names=global_short_names,
        )
        if match is None:
            label = (
                "external_imports"
                if relation.kind == "IMPORTS"
                else f"unresolved_{relation.kind.lower()}"
            )
            counts[label] += 1
            continue
        target_id, resolution_confidence = match
        if target_id == relation.source_id:
            counts[f"self_{relation.kind.lower()}"] += 1
            continue
        target = state.nodes[target_id]
        kind = relation.kind
        if kind == "CALLS" and target.kind == "class":
            kind = "INSTANTIATES"
        elif kind == "CALLS" and source.kind == "test" and not target.path.startswith("tests/"):
            kind = "TESTS"
        resolved.append(
            RawRelation(
                source_id=relation.source_id,
                target_id=target_id,
                kind=kind,
                confidence=relation.confidence * resolution_confidence,
                evidence=relation.evidence,
            )
        )
        counts[f"resolved_{kind.lower()}"] += 1
    return resolved, counts


def git_cochange_relations(
    repository: Path,
    nodes: Mapping[str, CodeNode],
    *,
    minimum_support: int = 2,
    maximum_files_per_commit: int = 20,
) -> list[RawRelation]:
    file_ids = {node.path: node.id for node in nodes.values() if node.kind == "file"}
    completed = subprocess.run(
        ["git", "-C", str(repository), "log", "--no-merges", "--format=@@%H", "--name-only"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []

    commits: list[tuple[str, tuple[str, ...]]] = []
    commit_hash = ""
    changed_paths: set[str] = set()

    def finish_commit() -> None:
        if commit_hash and changed_paths:
            commits.append((commit_hash, tuple(sorted(changed_paths))))

    for line in completed.stdout.splitlines():
        if line.startswith("@@"):
            finish_commit()
            commit_hash = line.removeprefix("@@")
            changed_paths = set()
        elif line and line in file_ids:
            changed_paths.add(line)
    finish_commit()

    file_commit_counts: Counter[str] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_commits: dict[tuple[str, str], list[str]] = defaultdict(list)
    for current_hash, paths in commits:
        file_commit_counts.update(paths)
        if len(paths) > maximum_files_per_commit:
            continue
        for pair in itertools.combinations(paths, 2):
            pair_counts[pair] += 1
            pair_commits[pair].append(current_hash)

    relations: list[RawRelation] = []
    for (left, right), count in pair_counts.items():
        if count < minimum_support:
            continue
        union_count = file_commit_counts[left] + file_commit_counts[right] - count
        jaccard = count / union_count if union_count else 0.0
        support = 1.0 - math.exp(-count / 2.0)
        confidence = min(1.0, jaccard * support)
        example_commits = ", ".join(value[:8] for value in pair_commits[(left, right)])
        relations.append(
            RawRelation(
                source_id=file_ids[left],
                target_id=file_ids[right],
                kind="CO_CHANGES",
                confidence=confidence,
                evidence=(
                    f"{count} non-merge commits; Jaccard={jaccard:.3f}; commits={example_commits}"
                ),
                count=count,
            )
        )
    return relations


def aggregate_relations(relations: Iterable[RawRelation]) -> tuple[CodeEdge, ...]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for relation in relations:
        key = (relation.source_id, relation.target_id, relation.kind)
        group = grouped.setdefault(key, {"count": 0, "weighted_confidence": 0.0, "evidence": set()})
        group["count"] += relation.count
        group["weighted_confidence"] += relation.confidence * relation.count
        group["evidence"].add(relation.evidence)

    maximum_count_by_kind: Counter[str] = Counter()
    for (_source, _target, kind), group in grouped.items():
        maximum_count_by_kind[kind] = max(maximum_count_by_kind[kind], group["count"])

    edges: list[CodeEdge] = []
    for (source_id, target_id, kind), group in sorted(grouped.items()):
        count = int(group["count"])
        confidence = float(group["weighted_confidence"]) / count
        frequency = math.log1p(count) / math.log1p(maximum_count_by_kind[kind])
        strength = min(
            1.0,
            EDGE_PRIORS.get(kind, 0.50) * confidence * (0.5 + 0.5 * frequency),
        )
        edges.append(
            CodeEdge(
                source_id=source_id,
                target_id=target_id,
                kind=kind,
                count=count,
                confidence=confidence,
                strength=strength,
                evidence=tuple(sorted(group["evidence"])),
            )
        )
    return tuple(edges)


def create_networkx_graph(
    nodes: Mapping[str, CodeNode], edges: Sequence[CodeEdge]
) -> nx.MultiDiGraph[str]:
    graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
    for node in nodes.values():
        graph.add_node(node.id, **vars(node))
    for edge in edges:
        graph.add_edge(edge.source_id, edge.target_id, key=edge.kind, **vars(edge))
    return graph


def validate_knowledge_graph(nodes: Mapping[str, CodeNode], edges: Sequence[CodeEdge]) -> None:
    missing_endpoints = [
        edge for edge in edges if edge.source_id not in nodes or edge.target_id not in nodes
    ]
    invalid_strengths = [edge for edge in edges if not 0.0 <= edge.strength <= 1.0]
    invalid_spans = [
        node for node in nodes.values() if node.start_line < 1 or node.end_line < node.start_line
    ]
    if missing_endpoints:
        raise ValueError(f"Edges with missing endpoints: {missing_endpoints[:3]}")
    if invalid_strengths:
        raise ValueError(f"Edges with invalid strengths: {invalid_strengths[:3]}")
    if invalid_spans:
        raise ValueError(f"Nodes with invalid source spans: {invalid_spans[:3]}")


def build_knowledge_graph(config: GraphConfig | Path) -> KnowledgeGraph:
    resolved_config = config if isinstance(config, GraphConfig) else GraphConfig(config)
    repository = resolved_config.repository_root
    source_files = discover_source_files(repository)
    snapshot = create_repository_snapshot(resolved_config, source_files)
    state = parse_repository(repository, source_files)
    resolved, resolution_counts = resolve_pending_relations(state)
    resolved.extend(
        git_cochange_relations(
            repository,
            state.nodes,
            minimum_support=resolved_config.cochange_minimum_support,
            maximum_files_per_commit=resolved_config.cochange_maximum_files_per_commit,
        )
    )
    verify_repository_snapshot(resolved_config, source_files, snapshot)
    edges = aggregate_relations(resolved)
    validate_knowledge_graph(state.nodes, edges)
    return KnowledgeGraph(
        snapshot=snapshot,
        nodes=dict(state.nodes),
        edges=edges,
        graph=create_networkx_graph(state.nodes, edges),
        issues=tuple(state.issues),
        resolution_counts=dict(resolution_counts),
    )


def graph_summary(knowledge: KnowledgeGraph) -> tuple[pd.DataFrame, pd.DataFrame]:
    node_counts = Counter(node.kind for node in knowledge.nodes.values())
    edge_counts = Counter(edge.kind for edge in knowledge.edges)
    node_frame = pd.DataFrame(
        [{"node_kind": kind, "count": count} for kind, count in node_counts.most_common()]
    )
    edge_frame = pd.DataFrame(
        [
            {
                "edge_kind": kind,
                "count": count,
                "mean_strength": round(
                    sum(edge.strength for edge in knowledge.edges if edge.kind == kind) / count,
                    3,
                ),
            }
            for kind, count in edge_counts.most_common()
        ]
    )
    return node_frame, edge_frame
