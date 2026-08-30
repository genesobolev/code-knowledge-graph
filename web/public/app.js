"use strict";

const $ = (selector) => document.querySelector(selector);
const svgNs = "http://www.w3.org/2000/svg";
const state = {
    manifest: null,
    repository: null,
    evaluation: null,
    query: null,
    repositories: [],
    queries: [],
    view: "explore",
    repositoryId: "",
    queryId: "",
    selectedNode: "",
    selectedEdge: "",
    selectedPath: "",
    edgeTypes: new Set(),
    edgeTypesFromHash: false,
    minimumScore: 0,
    contextFormat: "json",
    pan: { x: 0, y: 0 },
    zoom: 1,
    drag: null,
};

function element(name, className, text) {
    const item = document.createElement(name);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
}

function svgElement(name, attributes = {}, text) {
    const item = document.createElementNS(svgNs, name);
    Object.entries(attributes).forEach(([key, value]) => item.setAttribute(key, String(value)));
    if (text !== undefined) item.textContent = String(text);
    return item;
}

function clear(item) {
    item.replaceChildren();
    return item;
}

function safeArray(value) {
    return Array.isArray(value) ? value : [];
}

function text(value, fallback = "—") {
    if (value === undefined || value === null || value === "") return fallback;
    return String(value);
}

function number(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
}

function formatScore(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(2) : "—";
}

async function loadJson(url) {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`Unable to load ${url}: ${response.status}`);
    return response.json();
}

function dataPath(value, fallback) {
    const path = typeof value === "string" ? value : value?.file || value?.path || value?.url || value?.data_file;
    if (!path) return fallback;
    return path.startsWith("/") ? path : `/data/${path.replace(/^data\//, "")}`;
}

function normaliseEntries(value, kind) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === "object") {
        if (value.file || value.path || value.url || value.data_file) return [value];
        return Object.entries(value).map(([id, item]) => typeof item === "string" ? { id, file: item } : { id, ...item });
    }
    return kind ? [] : value;
}

function manifestRepositories() {
    const candidates = normaliseEntries(state.manifest.repositories || state.manifest.repository, "repository");
    const repositories = candidates.length ? candidates : [{ id: "repository", label: "Repository", file: state.manifest.repository || "repository.json" }];
    return repositories.map((entry, index) => ({
        ...entry,
        id: text(entry.id || entry.repository_id || entry.slug, `repository-${index + 1}`),
        label: text(entry.label || entry.name || entry.repository, `Repository ${index + 1}`),
        file: entry.file || entry.path || entry.repository_file || entry.url || entry.data_file,
    }));
}

function manifestQueries() {
    const candidates = normaliseEntries(state.manifest.queries || state.manifest.recorded_queries, "query");
    return candidates.map((entry, index) => ({
        ...entry,
        id: text(entry.id || entry.query_id || entry.slug, `query-${index + 1}`),
        label: text(entry.label || entry.query || entry.name, `Recorded query ${index + 1}`),
        file: entry.file || entry.path || entry.query_file || entry.url || entry.data_file,
    }));
}

function readHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    state.view = ["explore", "evaluation", "context"].includes(params.get("view")) ? params.get("view") : "explore";
    state.repositoryId = params.get("repo") || state.repositories[0]?.id || "";
    state.queryId = params.get("query") || state.queries[0]?.id || "";
    state.selectedNode = params.get("node") || "";
    state.selectedPath = params.get("path") || "";
    state.minimumScore = Math.max(0, Math.min(1, number(params.get("score"), 0)));
    state.contextFormat = params.get("format") === "markdown" ? "markdown" : "json";
    state.edgeTypesFromHash = params.has("types");
    if (state.edgeTypesFromHash) {
        const requestedTypes = (params.get("types") || "").split(",").filter(Boolean);
        state.edgeTypes = new Set(requestedTypes.filter((type) => type !== "_none"));
    } else {
        state.edgeTypes = state.query ? new Set(edgeTypes()) : new Set();
    }
}

function writeHash() {
    const params = new URLSearchParams({ view: state.view });
    if (state.repositoryId) params.set("repo", state.repositoryId);
    if (state.queryId) params.set("query", state.queryId);
    if (state.selectedNode) params.set("node", state.selectedNode);
    if (state.selectedPath) params.set("path", state.selectedPath);
    if (state.minimumScore > 0) params.set("score", state.minimumScore.toFixed(2));
    const availableTypes = edgeTypes();
    const selectedTypes = [...state.edgeTypes].sort();
    if (selectedTypes.join(",") !== availableTypes.join(",")) {
        params.set("types", selectedTypes.join(",") || "_none");
    }
    if (state.contextFormat !== "json") params.set("format", state.contextFormat);
    history.replaceState(null, "", `#${params.toString()}`);
}

function selectedRepositoryEntry() {
    return state.repositories.find((entry) => entry.id === state.repositoryId) || state.repositories[0];
}

function selectedQueryEntry() {
    return state.queries.find((entry) => entry.id === state.queryId) || state.queries[0];
}

async function loadSelectedData({ preserveHashFilters = false, preserveSelection = false } = {}) {
    const repositoryEntry = selectedRepositoryEntry();
    const queryEntry = selectedQueryEntry();
    if (!repositoryEntry || !queryEntry) throw new Error("The manifest doesn't declare a repository and recorded query.");
    const repositoryFile = dataPath(repositoryEntry.file || repositoryEntry, "repository.json");
    const queryFile = dataPath(queryEntry.file, null);
    if (!queryFile) throw new Error(`Recorded query '${queryEntry.id}' doesn't declare a JSON file.`);
    const [repository, query] = await Promise.all([loadJson(repositoryFile), loadJson(queryFile)]);
    state.repository = repository;
    state.query = query;
    state.selectedEdge = "";
    if (!preserveSelection) {
        state.selectedNode = "";
        state.selectedPath = "";
    } else {
        if (state.selectedNode && !findNode(state.selectedNode)) state.selectedNode = "";
        if (state.selectedPath && !findPath(state.selectedPath)) state.selectedPath = "";
    }
    const types = edgeTypes();
    if (preserveHashFilters && state.edgeTypesFromHash) {
        state.edgeTypes = new Set([...state.edgeTypes].filter((type) => types.includes(type)));
    } else {
        state.edgeTypes = new Set(types);
        state.edgeTypesFromHash = false;
    }
}

function nodes() {
    return safeArray(state.query?.nodes || state.query?.graph?.nodes || state.query?.result?.nodes);
}

function edges() {
    return safeArray(state.query?.edges || state.query?.graph?.edges || state.query?.result?.edges);
}

function paths() {
    return safeArray(state.query?.paths || state.query?.ranked_paths || state.query?.graph?.paths);
}

function nodeId(node, index) {
    return text(node.id || node.node_id || node.key || node.name, `node-${index + 1}`);
}

function nodeLabel(node, index) {
    return text(node.label || node.name || node.symbol || node.path || node.id, `Node ${index + 1}`);
}

function edgeId(edge, index) {
    return text(edge.id || edge.edge_id, `${edgeSource(edge)}:${edgeType(edge)}:${edgeTarget(edge)}:${index}`);
}

function edgeReference(value) { return typeof value === "object" ? value.id || value.node_id || value.key || value.name : value; }
function edgeSource(edge) { return text(edgeReference(edge.source || edge.from || edge.source_id), ""); }
function edgeTarget(edge) { return text(edgeReference(edge.target || edge.to || edge.target_id), ""); }
function edgeType(edge) { return text(edge.type || edge.kind || edge.edge_type || edge.relationship, "related"); }
function edgeScore(edge) { return number(edge.score ?? edge.strength ?? edge.weight ?? edge.relevance, 1); }
function edgeTypes() { return [...new Set(edges().map(edgeType))].sort(); }
function findNode(id) { return nodes().find((item, index) => nodeId(item, index) === id); }
function visibleEdges() { return edges().filter((edge) => state.edgeTypes.has(edgeType(edge)) && edgeScore(edge) >= state.minimumScore); }

function bindEvents() {
    document.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (!button) return;
        if (button.dataset.view) {
            state.view = button.dataset.view;
            writeHash(); render();
        } else if (button.dataset.pathId !== undefined) {
            state.selectedPath = button.dataset.pathId;
            state.selectedNode = "";
            state.selectedEdge = "";
            writeHash(); renderGraph(); renderInspector(); renderPaths();
        } else if (button.dataset.nodeId) {
            selectNode(button.dataset.nodeId);
        } else if (button.dataset.contextFormat) {
            state.contextFormat = button.dataset.contextFormat;
            writeHash(); renderContext();
        } else if (button.id === "copy-context") {
            copyContext();
        } else if (button.id === "zoom-in") {
            state.zoom = Math.min(2.4, state.zoom + 0.15); renderGraph();
        } else if (button.id === "zoom-out") {
            state.zoom = Math.max(0.45, state.zoom - 0.15); renderGraph();
        } else if (button.id === "zoom-reset") {
            state.zoom = 1; state.pan = { x: 0, y: 0 }; renderGraph();
        }
    });
    $("#repository-select").addEventListener("change", async (event) => {
        state.repositoryId = event.target.value;
        state.pan = { x: 0, y: 0 }; state.zoom = 1;
        await refreshSelection();
    });
    $("#query-select").addEventListener("change", async (event) => {
        state.queryId = event.target.value;
        state.pan = { x: 0, y: 0 }; state.zoom = 1;
        await refreshSelection();
    });
    $("#score-filter").addEventListener("input", (event) => {
        state.minimumScore = number(event.target.value);
        writeHash(); renderGraph(); renderTables();
    });
    $("#edge-filter-list").addEventListener("change", (event) => {
        if (!event.target.matches("input[type=checkbox]")) return;
        if (event.target.checked) state.edgeTypes.add(event.target.value);
        else state.edgeTypes.delete(event.target.value);
        writeHash(); renderGraph(); renderTables();
    });
    $("#graph-stage").addEventListener("pointerdown", (event) => {
        if (event.target.closest(".graph-node")) return;
        state.drag = { x: event.clientX, y: event.clientY, panX: state.pan.x, panY: state.pan.y };
        event.currentTarget.setPointerCapture(event.pointerId);
    });
    $("#graph-stage").addEventListener("pointermove", (event) => {
        if (!state.drag) return;
        state.pan = { x: state.drag.panX + event.clientX - state.drag.x, y: state.drag.panY + event.clientY - state.drag.y };
        renderGraph();
    });
    ["pointerup", "pointercancel"].forEach((name) => $("#graph-stage").addEventListener(name, () => { state.drag = null; }));
    $("#graph-stage").addEventListener("wheel", (event) => {
        event.preventDefault();
        state.zoom = Math.max(0.45, Math.min(2.4, state.zoom + (event.deltaY < 0 ? 0.1 : -0.1)));
        renderGraph();
    }, { passive: false });
    window.addEventListener("hashchange", async () => {
        const previous = `${state.repositoryId}/${state.queryId}`;
        readHash();
        if (previous !== `${state.repositoryId}/${state.queryId}`) {
            await loadSelectedData({ preserveHashFilters: true, preserveSelection: true });
        }
        render();
    });
}

async function refreshSelection() {
    try {
        await loadSelectedData();
        writeHash(); render();
    } catch (error) { showError(error); }
}

function selectNode(id) {
    state.selectedNode = id;
    state.selectedEdge = "";
    state.selectedPath = "";
    writeHash(); renderGraph(); renderInspector();
}

function render() {
    document.querySelectorAll("[data-view-panel]").forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== state.view; });
    document.querySelectorAll("[data-view]").forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.view === state.view)));
    renderExplore(); renderEvaluation(); renderContext();
}

function renderExplore() {
    fillSelect($("#repository-select"), state.repositories, state.repositoryId, (entry) => entry.label || entry.name || entry.id);
    fillSelect($("#query-select"), state.queries, state.queryId, (entry) => entry.label);
    $("#query-question").textContent = state.query?.query || state.query?.prompt || "Recorded graph result";
    $("#query-description").textContent = state.query?.description || "Precomputed nodes and strongest paths for the selected question.";
    $("#score-filter").value = String(state.minimumScore);
    $("#score-output").textContent = state.minimumScore.toFixed(2);
    renderFilters(); renderGraph(); renderPaths(); renderInspector(); renderTables(); renderRankedRows(); renderProvenance($("#explore-provenance"), state.query?.provenance);
}

function fillSelect(select, items, selected, label) {
    clear(select);
    items.forEach((item) => {
        const option = element("option", null, label(item));
        option.value = item.id;
        option.selected = item.id === selected;
        select.append(option);
    });
}

function renderFilters() {
    const holder = clear($("#edge-filter-list"));
    edgeTypes().forEach((type) => {
        const label = element("label");
        const input = element("input");
        input.type = "checkbox"; input.value = type; input.checked = state.edgeTypes.has(type);
        label.append(input, document.createTextNode(type));
        holder.append(label);
    });
}

function graphPoint(node, index) {
    const rawX = node.x ?? node.position?.x;
    const rawY = node.y ?? node.position?.y;
    const columns = Math.max(1, Math.ceil(Math.sqrt(nodes().length)));
    return {
        x: Number.isFinite(Number(rawX)) ? Number(rawX) : 110 + (index % columns) * 220,
        y: Number.isFinite(Number(rawY)) ? Number(rawY) : 110 + Math.floor(index / columns) * 155,
    };
}

function renderGraph() {
    const viewport = clear($("#graph-viewport"));
    viewport.setAttribute("transform", `translate(${state.pan.x} ${state.pan.y}) scale(${state.zoom})`);
    const positions = new Map(nodes().map((node, index) => [nodeId(node, index), graphPoint(node, index)]));
    const pathNodeIds = selectedPathNodeIds();
    const pathEdgeIds = selectedPathEdgeIds();
    visibleEdges().forEach((edge, index) => {
        const source = positions.get(edgeSource(edge)); const target = positions.get(edgeTarget(edge));
        if (!source || !target) return;
        const id = edgeId(edge, index);
        const selected = id === state.selectedEdge || pathEdgeIds.has(id);
        const line = svgElement("line", { x1: source.x, y1: source.y, x2: target.x, y2: target.y, class: `graph-edge${selected ? " is-selected" : ""}` });
        line.dataset.edgeId = id;
        line.addEventListener("click", () => { state.selectedEdge = id; state.selectedNode = ""; state.selectedPath = ""; writeHash(); renderGraph(); renderInspector(); });
        viewport.append(line);
        const label = svgElement("text", { x: (source.x + target.x) / 2, y: (source.y + target.y) / 2 - 7, class: "edge-label", "text-anchor": "middle" }, edgeType(edge));
        viewport.append(label);
    });
    nodes().forEach((node, index) => {
        const id = nodeId(node, index); const point = positions.get(id);
        const group = svgElement("g", { transform: `translate(${point.x} ${point.y})`, class: `graph-node${state.selectedNode === id || pathNodeIds.has(id) ? " is-selected" : ""}`, tabindex: "0", role: "button", "aria-label": `Inspect ${nodeLabel(node, index)}`, "data-node-type": text(node.type || node.kind, "node").toLowerCase() });
        group.addEventListener("click", () => selectNode(id));
        group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectNode(id); } });
        group.append(svgElement("circle", { r: 25 }));
        group.append(svgElement("text", { x: 0, y: 5, "text-anchor": "middle" }, nodeLabel(node, index).slice(0, 14)));
        group.append(svgElement("text", { x: 0, y: 42, "text-anchor": "middle", class: "node-type" }, text(node.type || node.kind, "node")));
        viewport.append(group);
    });
}

function selectedPathNodeIds() {
    const path = findPath(state.selectedPath);
    const values = safeArray(path?.nodes || path?.node_ids || path?.steps || path);
    return new Set(values.map((item) => typeof item === "object" ? item.id || item.node_id || item.node : item).filter(Boolean).map(String));
}

function selectedPathEdgeIds() {
    const path = findPath(state.selectedPath);
    const ids = [];
    safeArray(path?.steps).forEach((step) => {
        if (step?.edge_id) ids.push(step.edge_id);
        safeArray(step?.contributions).forEach((contribution) => {
            if (contribution?.edge_id) ids.push(contribution.edge_id);
        });
    });
    return new Set(ids.map(String));
}

function pathId(path, index) { return text(path?.id || path?.path_id, `path-${index + 1}`); }
function findPath(id) { return paths().find((path, index) => pathId(path, index) === id); }

function renderPaths() {
    const holder = clear($("#path-list"));
    if (!paths().length) { holder.append(element("span", "inspector-content", "No recorded paths are available.")); return; }
    paths().forEach((path, index) => {
        const label = path.label || path.summary || path.name || safeArray(path.nodes || path.node_ids || path).map(String).join(" → ");
        const button = element("button", null, label || `Path ${index + 1}`);
        const id = pathId(path, index);
        button.type = "button"; button.dataset.pathId = id; button.setAttribute("aria-pressed", String(id === state.selectedPath));
        holder.append(button);
    });
}

function renderInspector() {
    const heading = $("#inspector-heading"); const content = clear($("#inspector-content"));
    let selected = null;
    if (state.selectedNode) selected = findNode(state.selectedNode);
    if (selected) { heading.textContent = nodeLabel(selected, nodes().indexOf(selected)); appendAttributes(content, selected); return; }
    if (state.selectedEdge) {
        const edge = edges().find((item, index) => edgeId(item, index) === state.selectedEdge);
        if (edge) { heading.textContent = `${edgeSource(edge)} ${edgeType(edge)} ${edgeTarget(edge)}`; appendAttributes(content, edge); return; }
    }
    const path = findPath(state.selectedPath);
    if (path) { heading.textContent = path.label || path.name || "Recorded path"; appendAttributes(content, path); return; }
    heading.textContent = "Nothing selected";
    content.textContent = "Select a graph node, edge, or recorded path to inspect its attributes.";
}

function appendAttributes(container, value) {
    const list = element("dl", "inspector-list");
    Object.entries(value).forEach(([key, item]) => {
        const row = element("div"); const name = element("dt", null, key); const detail = element("dd");
        detail.textContent = typeof item === "object" ? JSON.stringify(item, null, 2) : text(item);
        row.append(name, detail); list.append(row);
    });
    container.append(list);
}

function renderTables() {
    const nodeBody = clear($("#nodes-table")); const edgeBody = clear($("#edges-table"));
    nodes().forEach((node, index) => {
        const row = element("tr"); const nodeCell = element("td"); const button = element("button", "table-button", nodeLabel(node, index));
        button.type = "button"; button.dataset.nodeId = nodeId(node, index); nodeCell.append(button);
        row.append(nodeCell, cell(text(node.type || node.kind, "node"), "type-pill"), cell(formatScore(node.score ?? node.relevance)), cell(text(node.path || node.description || node.file || node.module)));
        nodeBody.append(row);
    });
    visibleEdges().forEach((edge, index) => {
        const row = element("tr"); const sourceCell = element("td"); const button = element("button", "table-button", edgeSource(edge));
        button.type = "button"; button.setAttribute("aria-label", `Inspect edge ${edgeSource(edge)} ${edgeType(edge)} ${edgeTarget(edge)}`);
        button.addEventListener("click", () => { state.selectedEdge = edgeId(edge, index); state.selectedNode = ""; state.selectedPath = ""; writeHash(); renderGraph(); renderInspector(); });
        sourceCell.append(button); row.append(sourceCell, cell(edgeType(edge), "type-pill"), cell(edgeTarget(edge)), cell(formatScore(edgeScore(edge))));
        edgeBody.append(row);
    });
    $("#node-count").textContent = String(nodes().length); $("#edge-count").textContent = String(visibleEdges().length);
}

function rankedGroups() {
    const ranked = state.query?.ranked || state.query?.results || {};
    return {
        relevant: safeArray(ranked.relevant || state.query?.relevant || state.query?.ranked_relevant),
        related: safeArray(ranked.related || state.query?.related || state.query?.ranked_related),
    };
}

function rankedLabel(row, index) {
    if (typeof row === "string") return row;
    return text(row.label || row.name || row.path || row.node_id || row.id || row.result, `Result ${index + 1}`);
}

function renderRankedRows() {
    const body = clear($("#ranked-table"));
    const groups = rankedGroups(); let count = 0;
    Object.entries(groups).forEach(([group, rows]) => rows.forEach((row, index) => {
        const result = element("tr");
        result.append(cell(group, "type-pill"), cell(String(row.rank || index + 1)), cell(rankedLabel(row, index)), cell(formatScore(typeof row === "object" ? row.score ?? row.relevance : undefined)));
        body.append(result); count += 1;
    }));
    if (!count) { const row = element("tr"); const message = element("td", null, "No ranked relevant or related rows were recorded."); message.colSpan = 4; row.append(message); body.append(row); }
}

function cell(value, className) { const item = element("td"); const child = element("span", className, value); item.append(child); return item; }

function evaluationRows() {
    return safeArray(state.evaluation?.queries || state.evaluation?.results || state.evaluation?.per_query || state.evaluation?.query_results);
}

function metricValue(source, system) {
    if (!source || typeof source !== "object") return null;
    const base = source[system] || source[`${system}_metrics`] || source.metrics?.[system];
    const keys = ["recall_at_k", "recall", "mrr", "precision_at_k", "precision", "score"];
    if (typeof base === "number") return base;
    for (const key of keys) if (Number.isFinite(Number(base?.[key]))) return Number(base[key]);
    for (const key of keys) if (Number.isFinite(Number(source[`${system}_${key}`]))) return Number(source[`${system}_${key}`]);
    return null;
}

function renderEvaluation() {
    const rows = evaluationRows();
    const summary = state.evaluation?.summary || {};
    const lexicalMetrics = summary.lexical_metrics || {};
    const graphMetrics = summary.graph_metrics || {};
    const fallbackLexical = average(rows.map((row) => metricValue(row, "lexical")));
    const fallbackGraph = average(rows.map((row) => metricValue(row, "graph")));
    const definitions = [
        ["Answer MRR at 10", "answer_mrr_at_10", summary.lexical?.score ?? fallbackLexical, summary.graph?.score ?? fallbackGraph],
        ["Overall recall at 10", "recall_at_10", null, null],
        ["Overall recall at 20", "recall_at_20", null, null],
        ["Supporting recall at 10", "supporting_recall_at_10", null, null],
    ];
    const grid = clear($("#metric-grid"));
    definitions.forEach(([title, key, lexicalFallback, graphFallback]) => {
        comparisonMetricCard(
            grid,
            title,
            numberOrNull(lexicalMetrics[key]) ?? lexicalFallback,
            numberOrNull(graphMetrics[key]) ?? graphFallback,
        );
    });
    $("#evaluation-conclusion").textContent = text(
        summary.conclusion,
        "The artifact doesn't include an evaluation conclusion.",
    );
    const body = clear($("#evaluation-table"));
    rows.forEach((row, index) => {
        const l = metricValue(row, "lexical"); const g = metricValue(row, "graph"); const change = l !== null && g !== null ? g - l : null;
        const retrieved = safeArray(row.newly_retrieved_at_10).length;
        const missed = safeArray(row.newly_missed_at_10).length;
        let changeLabel = formatDelta(change);
        if (row.regression) changeLabel = `Regression: lost ${missed} judgment${missed === 1 ? "" : "s"}`;
        else if (retrieved) changeLabel = `Found ${retrieved} additional judgment${retrieved === 1 ? "" : "s"}`;
        const tr = element("tr"); tr.append(cell(text(row.label || row.query || row.query_id || row.id, `Recorded query ${index + 1}`)), cell(formatMetric(l)), cell(formatMetric(g)), cell(changeLabel, row.regression ? "negative" : retrieved ? "positive" : ""));
        body.append(tr);
    });
    if (!rows.length) { const row = element("tr"); const message = element("td", null, "No per-query evaluation rows were recorded."); message.colSpan = 4; row.append(message); body.append(row); }
    renderMisses(); renderProvenance($("#evaluation-provenance"), state.evaluation?.provenance || state.manifest?.provenance);
}

function average(values) { const numbers = values.filter((item) => item !== null && Number.isFinite(item)); return numbers.length ? numbers.reduce((total, item) => total + item, 0) / numbers.length : null; }
function numberOrNull(value) { if (value === null || value === undefined || value === "") return null; const parsed = Number(value); return Number.isFinite(parsed) ? parsed : null; }
function formatMetric(value) { return value === null || value === undefined ? "—" : Number(value).toFixed(3); }
function formatDelta(value) { return value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${Number(value).toFixed(3)}`; }
function comparisonMetricCard(container, title, lexical, graph) {
    const card = element("article", "metric-card");
    card.append(element("small", null, title));
    const comparison = element("div", "metric-comparison");
    const lexicalGroup = element("div");
    lexicalGroup.append(element("span", null, "Lexical"), element("strong", null, formatMetric(lexical)));
    const graphGroup = element("div");
    graphGroup.append(element("span", null, "Graph"), element("strong", null, formatMetric(graph)));
    comparison.append(lexicalGroup, element("span", "metric-arrow", "→"), graphGroup);
    const delta = lexical !== null && graph !== null ? graph - lexical : null;
    const change = element("p", delta !== null ? delta > 0 ? "positive" : delta < 0 ? "negative" : "" : "", `Change ${formatDelta(delta)}`);
    card.append(comparison, change);
    container.append(card);
}

function renderMisses() {
    const holder = clear($("#misses-list"));
    const misses = safeArray(state.evaluation?.misses || state.evaluation?.disagreements || state.evaluation?.failures || state.evaluation?.error_cases);
    if (!misses.length) { holder.append(element("p", "inspector-content", "No misses or disagreements were recorded in this evaluation artifact.")); return; }
    misses.forEach((miss, index) => {
        const card = element("article", `miss-card${miss.regression ? " is-regression" : ""}`);
        card.append(
            element("strong", null, text(miss.query || miss.label || miss.query_id || miss.id, `Case ${index + 1}`)),
            element("p", null, text(miss.reason || miss.description || miss.message || miss.miss)),
        );
        const groups = [
            ["Newly retrieved by graph", miss.newly_retrieved_at_10],
            ["Newly missed by graph", miss.newly_missed_at_10],
            ["Lexical misses at 10", miss.lexical_missed_at_10],
            ["Graph misses at 10", miss.graph_missed_at_10],
        ];
        groups.forEach(([label, values]) => {
            const nodeIds = safeArray(values);
            if (!nodeIds.length) return;
            const details = element("details", "miss-detail");
            details.open = Boolean(miss.regression && label === "Newly missed by graph");
            details.append(element("summary", null, `${label} · ${nodeIds.length}`));
            const list = element("ul");
            nodeIds.forEach((nodeId) => {
                const item = element("li");
                item.append(element("code", null, nodeId));
                list.append(item);
            });
            details.append(list);
            card.append(details);
        });
        holder.append(card);
    });
}

function contextPayload() {
    const context = state.query?.context || state.query?.context_bundle || state.query?.bundle || {};
    if (state.contextFormat === "markdown") return typeof context.markdown === "string" ? context.markdown : typeof state.query?.markdown === "string" ? state.query.markdown : "No recorded Markdown context was provided.";
    if (context.json !== undefined) return typeof context.json === "string" ? context.json : JSON.stringify(context.json, null, 2);
    return JSON.stringify(context, null, 2);
}

function renderContext() {
    const description = state.query?.description || state.query?.query || state.query?.prompt || "the selected recorded query";
    $("#context-description").textContent = `Structured context supplied for ${description}.`;
    document.querySelectorAll("[data-context-format]").forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.contextFormat === state.contextFormat)));
    $("#context-code").textContent = contextPayload();
    $("#copy-context").textContent = `Copy ${state.contextFormat === "json" ? "JSON" : "Markdown"}`;
    renderProvenance($("#context-provenance"), state.query?.provenance);
}

async function copyContext() {
    const value = contextPayload(); const button = $("#copy-context");
    try { await navigator.clipboard.writeText(value); button.textContent = "Copied"; }
    catch { button.textContent = "Copy unavailable"; }
    window.setTimeout(() => { button.textContent = `Copy ${state.contextFormat === "json" ? "JSON" : "Markdown"}`; }, 1500);
}

function renderProvenance(holder, provenance) {
    clear(holder);
    const values = provenance && typeof provenance === "object" ? provenance : {};
    const repositoryName = state.repository?.name || state.repository?.repository || selectedRepositoryEntry()?.label || selectedRepositoryEntry()?.id;
    const entries = [
        ["Repository", repositoryName],
        ["Commit", shortHash(values.snapshot || values.commit || state.repository?.commit || state.repository?.revision)],
        ["Tree", shortHash(values.tree)],
        ["Worktree", values.clean === true ? "clean" : values.clean === false ? "dirty" : null],
        ["History", values.shallow === true ? "shallow" : values.shallow === false ? "complete" : null],
        ["Branch", values.detached ? "detached HEAD" : values.branch],
        ["Source digest", shortHash(values.indexed_source_sha256)],
        ["Graph schema", values.schema_version || state.manifest?.schema_version],
        ["Extractor", values.extractor_version],
    ];
    entries.filter(([, value]) => value).forEach(([label, value]) => { const item = element("span"); item.append(element("strong", null, `${label}: `), document.createTextNode(text(value))); holder.append(item); });
    if (!holder.childElementCount) holder.textContent = "Provenance wasn't recorded in this artifact.";
}

function shortHash(value) { const raw = text(value, ""); return raw.length > 12 ? raw.slice(0, 12) : raw; }

function showError(error) {
    $("#loading-state").hidden = true;
    const errorState = $("#error-state"); errorState.hidden = false;
    errorState.textContent = `The recorded graph data couldn't be loaded. ${error.message}`;
}

async function initialize() {
    try {
        state.manifest = await loadJson("/data/manifest.json");
        state.repositories = manifestRepositories(); state.queries = manifestQueries();
        readHash();
        state.evaluation = await loadJson(dataPath(state.manifest.evaluation || state.manifest.evaluation_file, "evaluation.json"));
        await loadSelectedData({ preserveHashFilters: true, preserveSelection: true }); bindEvents(); writeHash();
        $("#loading-state").hidden = true; render();
    } catch (error) { showError(error); }
}

initialize();
