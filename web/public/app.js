"use strict";

const $ = (selector) => document.querySelector(selector);
const svgNs = "http://www.w3.org/2000/svg";
const graphWidth = 1000;
const graphHeight = 620;
const maximumGraphNodes = 12;
const maximumAnchorNodes = 6;
const minimumZoom = 0.6;
const maximumZoom = 2.4;
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
    pan: { x: 0, y: 0 },
    zoom: 1,
    drag: null,
    loadRequestId: 0,
    resizeFrame: null,
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

function text(value, fallback = "-") {
    if (value === undefined || value === null || value === "") return fallback;
    return String(value);
}

function number(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
}

function formatScore(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(2) : "-";
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
    state.view = ["explore", "evaluation"].includes(params.get("view")) ? params.get("view") : "explore";
    state.repositoryId = params.get("repo") || state.repositories[0]?.id || "";
    state.queryId = params.get("query") || state.queries[0]?.id || "";
    state.selectedNode = params.get("node") || "";
    state.selectedEdge = params.get("edge") || "";
    state.selectedPath = params.get("path") || "";
    if (state.selectedNode) {
        state.selectedEdge = "";
        state.selectedPath = "";
    } else if (state.selectedEdge) {
        state.selectedPath = "";
    }
    state.minimumScore = Math.max(0, Math.min(1, number(params.get("score"), 0)));
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
    if (state.selectedEdge) params.set("edge", state.selectedEdge);
    if (state.selectedPath) params.set("path", state.selectedPath);
    if (state.minimumScore > 0) params.set("score", String(state.minimumScore));
    const availableTypes = edgeTypes();
    const selectedTypes = [...state.edgeTypes].sort();
    if (selectedTypes.join(",") !== availableTypes.join(",")) {
        params.set("types", selectedTypes.join(",") || "_none");
    }
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
    state.repositoryId = repositoryEntry.id;
    state.queryId = queryEntry.id;
    const requestedRepositoryId = repositoryEntry.id;
    const requestedQueryId = queryEntry.id;
    const requestId = ++state.loadRequestId;
    const repositoryFile = dataPath(repositoryEntry.file || repositoryEntry, "repository.json");
    const queryFile = dataPath(queryEntry.file, null);
    if (!queryFile) throw new Error(`Recorded query '${queryEntry.id}' doesn't declare a JSON file.`);
    let repository;
    let query;
    try {
        [repository, query] = await Promise.all([loadJson(repositoryFile), loadJson(queryFile)]);
    } catch (error) {
        if (requestId !== state.loadRequestId) return false;
        throw error;
    }
    if (
        requestId !== state.loadRequestId
        || state.repositoryId !== requestedRepositoryId
        || state.queryId !== requestedQueryId
    ) return false;
    state.repository = repository;
    state.query = query;
    state.minimumScore = normaliseMinimumScore(state.minimumScore);
    if (!preserveSelection) {
        state.selectedNode = "";
        state.selectedEdge = "";
        state.selectedPath = "";
    } else {
        if (state.selectedNode && !findNode(state.selectedNode)) state.selectedNode = "";
        if (state.selectedEdge && !findEdge(state.selectedEdge)) state.selectedEdge = "";
        if (state.selectedPath && !findPath(state.selectedPath)) state.selectedPath = "";
    }
    const types = edgeTypes();
    if (preserveHashFilters && state.edgeTypesFromHash) {
        state.edgeTypes = new Set([...state.edgeTypes].filter((type) => types.includes(type)));
    } else {
        state.edgeTypes = new Set(types);
        state.edgeTypesFromHash = false;
    }
    reconcileSelectionWithFilters();
    return true;
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
function findEdge(id) { return edges().find((item, index) => edgeId(item, index) === id); }
function visibleEdges() { return edges().filter((edge) => state.edgeTypes.has(edgeType(edge)) && edgeScore(edge) >= state.minimumScore); }

function scoreThresholds() {
    const scores = [...new Set(edges().map(edgeScore))].sort((left, right) => left - right);
    const thresholds = [0, ...scores.slice(1)];
    if (scores.length && scores.at(-1) < 1) thresholds.push(1);
    return thresholds;
}

function normaliseMinimumScore(value) {
    if (value <= 0) return 0;
    const scores = [...new Set(edges().map(edgeScore))].sort((left, right) => left - right);
    if (scores.length && value <= scores[0]) return 0;
    const thresholds = scoreThresholds();
    return thresholds.find((threshold) => threshold >= value) ?? thresholds.at(-1) ?? 0;
}

function scoreThresholdIndex(value) {
    const thresholds = scoreThresholds();
    const exact = thresholds.indexOf(value);
    if (exact >= 0) return exact;
    const next = thresholds.findIndex((threshold) => threshold >= value);
    return next >= 0 ? next : Math.max(0, thresholds.length - 1);
}

function reconcileSelectionWithFilters() {
    if (state.selectedNode && !findNode(state.selectedNode)) state.selectedNode = "";
    if (state.selectedPath && !findPath(state.selectedPath)) state.selectedPath = "";
    if (state.selectedEdge && !visibleEdges().some((edge) => edgeId(edge, edges().indexOf(edge)) === state.selectedEdge)) {
        state.selectedEdge = "";
    }
}

function updateScoreOutput() {
    const output = $("#score-output");
    if (output) output.textContent = state.minimumScore === 0 ? "All" : `${(state.minimumScore * 100).toFixed(1)}%`;
}

function updateZoomOutput() {
    const percent = `${Math.round(state.zoom * 100)}%`;
    const output = $("#zoom-output");
    if (output) {
        output.textContent = percent;
        return;
    }
    const reset = $("#zoom-reset");
    if (reset) reset.textContent = percent;
}

function applyViewportTransform() {
    const viewport = $("#graph-viewport");
    if (viewport) viewport.setAttribute("transform", `translate(${state.pan.x} ${state.pan.y}) scale(${state.zoom})`);
    $("#graph-stage")?.classList.toggle("is-zoomed", state.zoom > 1.001);
    updateZoomOutput();
}

function zoomAt(nextZoom, focus = { x: graphWidth / 2, y: graphHeight / 2 }) {
    const zoom = Math.max(minimumZoom, Math.min(maximumZoom, nextZoom));
    if (zoom === state.zoom) return;
    const worldX = (focus.x - state.pan.x) / state.zoom;
    const worldY = (focus.y - state.pan.y) / state.zoom;
    state.pan = { x: focus.x - worldX * zoom, y: focus.y - worldY * zoom };
    state.zoom = zoom;
    applyViewportTransform();
}

function graphClientPoint(clientX, clientY) {
    const svg = $("#graph-svg");
    const transform = svg?.getScreenCTM();
    if (transform && typeof DOMPoint === "function") {
        const point = new DOMPoint(clientX, clientY).matrixTransform(transform.inverse());
        return { x: point.x, y: point.y };
    }
    const bounds = svg?.getBoundingClientRect();
    if (!bounds?.width || !bounds?.height) return { x: graphWidth / 2, y: graphHeight / 2 };
    const scale = Math.min(bounds.width / graphWidth, bounds.height / graphHeight);
    const offsetX = bounds.left + (bounds.width - graphWidth * scale) / 2;
    const offsetY = bounds.top + (bounds.height - graphHeight * scale) / 2;
    return {
        x: (clientX - offsetX) / scale,
        y: (clientY - offsetY) / scale,
    };
}

function graphEventPoint(event) { return graphClientPoint(event.clientX, event.clientY); }

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
            writeHash(); renderGraph(); renderInspector(); renderPaths(); renderRankedRows();
        } else if (button.dataset.nodeId) {
            selectNode(button.dataset.nodeId);
        } else if (button.id === "zoom-in") {
            zoomAt(state.zoom * 1.2);
        } else if (button.id === "zoom-out") {
            zoomAt(state.zoom / 1.2);
        } else if (button.id === "zoom-reset") {
            state.zoom = 1; state.pan = { x: 0, y: 0 }; applyViewportTransform();
        }
    });
    $("#query-select")?.addEventListener("change", async (event) => {
        state.queryId = event.target.value;
        state.pan = { x: 0, y: 0 }; state.zoom = 1;
        await refreshSelection();
    });
    $("#score-filter")?.addEventListener("input", (event) => {
        state.minimumScore = scoreThresholds()[number(event.target.value)] ?? 0;
        reconcileSelectionWithFilters();
        updateScoreOutput(); writeHash(); renderGraph(); renderInspector();
    });
    $("#edge-filter-list")?.addEventListener("change", (event) => {
        if (!event.target.matches("input[type=checkbox]")) return;
        if (event.target.checked) state.edgeTypes.add(event.target.value);
        else state.edgeTypes.delete(event.target.value);
        reconcileSelectionWithFilters();
        writeHash(); renderGraph(); renderInspector();
    });
    const stage = $("#graph-stage");
    stage?.addEventListener("pointerdown", (event) => {
        if (event.target.closest(".graph-node, .graph-edge, .graph-edge-hit")) return;
        state.drag = { point: graphEventPoint(event), panX: state.pan.x, panY: state.pan.y };
        event.currentTarget.setPointerCapture(event.pointerId);
    });
    stage?.addEventListener("pointermove", (event) => {
        if (!state.drag) return;
        const point = graphEventPoint(event);
        state.pan = {
            x: state.drag.panX + point.x - state.drag.point.x,
            y: state.drag.panY + point.y - state.drag.point.y,
        };
        applyViewportTransform();
    });
    ["pointerup", "pointercancel"].forEach((name) => stage?.addEventListener(name, () => { state.drag = null; }));
    stage?.addEventListener("wheel", (event) => {
        event.preventDefault();
        zoomAt(state.zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12), graphEventPoint(event));
    }, { passive: false });
    window.addEventListener("resize", () => {
        if (state.resizeFrame !== null) cancelAnimationFrame(state.resizeFrame);
        state.resizeFrame = requestAnimationFrame(() => {
            state.resizeFrame = null;
            renderGraph();
        });
    });
    window.addEventListener("hashchange", async () => {
        const previous = `${state.repositoryId}/${state.queryId}`;
        readHash();
        if (previous !== `${state.repositoryId}/${state.queryId}`) {
            const loaded = await loadSelectedData({ preserveHashFilters: true, preserveSelection: true });
            if (!loaded) return;
        } else {
            reconcileSelectionWithFilters();
        }
        writeHash();
        render();
    });
}

async function refreshSelection() {
    try {
        const loaded = await loadSelectedData();
        if (!loaded) return;
        writeHash(); render();
    } catch (error) { showError(error); }
}

function selectNode(id) {
    state.selectedNode = id;
    state.selectedEdge = "";
    state.selectedPath = "";
    writeHash(); renderGraph(); renderInspector(); renderPaths(); renderRankedRows();
}

function selectEdge(id) {
    state.selectedEdge = id;
    state.selectedNode = "";
    state.selectedPath = "";
    writeHash(); renderGraph(); renderInspector(); renderPaths(); renderRankedRows();
}

function render() {
    document.querySelectorAll("[data-view-panel]").forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== state.view; });
    document.querySelectorAll("[data-view]").forEach((tab) => tab.setAttribute("aria-pressed", String(tab.dataset.view === state.view)));
    renderExplore(); renderEvaluation();
}

function renderExplore() {
    const querySelect = $("#query-select");
    if (querySelect) fillSelect(querySelect, state.queries, state.queryId, (entry) => entry.label);
    const question = $("#query-question");
    if (question) question.textContent = state.query?.query || state.query?.prompt || "Recorded graph result";
    const scoreFilter = $("#score-filter");
    if (scoreFilter) {
        scoreFilter.max = String(Math.max(0, scoreThresholds().length - 1));
        scoreFilter.value = String(scoreThresholdIndex(state.minimumScore));
    }
    updateScoreOutput();
    renderFilters(); renderGraph(); renderPaths(); renderInspector(); renderRankedRows();
    const provenance = $("#explore-provenance");
    if (provenance) renderProvenance(provenance, state.query?.provenance);
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
    const filterList = $("#edge-filter-list");
    if (!filterList) return;
    const holder = clear(filterList);
    edgeTypes().forEach((type) => {
        const label = element("label");
        const input = element("input");
        input.type = "checkbox"; input.value = type; input.checked = state.edgeTypes.has(type);
        label.append(input, document.createTextNode(type));
        holder.append(label);
    });
}

function rankedNodeId(row) {
    if (typeof row === "string") return row;
    return text(row?.node_id || row?.id || row?.key, "");
}

function graphRankings() {
    const groups = rankedGroups();
    return {
        relevant: new Map(groups.relevant.map((row, index) => [rankedNodeId(row), number(row?.rank, index + 1)])),
        related: new Map(groups.related.map((row, index) => [rankedNodeId(row), number(row?.rank, index + 1)])),
    };
}

function graphSlice() {
    const allNodes = nodes();
    const nodesById = new Map(allNodes.map((node, index) => [nodeId(node, index), node]));
    const rankings = graphRankings();
    const recordedAnchors = [...rankings.relevant.keys()].filter((id) => nodesById.has(id));
    const fallbackAnchors = allNodes
        .filter((node) => text(node.rank_group, "").toLowerCase() === "anchor")
        .map((node) => nodeId(node, allNodes.indexOf(node)));
    const anchorIds = new Set((recordedAnchors.length ? recordedAnchors : fallbackAnchors.length ? fallbackAnchors : [...nodesById.keys()]).slice(0, maximumAnchorNodes));
    const displayedIds = new Set(anchorIds);
    const pathNodeIds = selectedPathNodeIds();
    const selectedEdge = findEdge(state.selectedEdge);
    [...pathNodeIds, state.selectedNode].filter(Boolean).forEach((id) => {
        if (displayedIds.size < maximumGraphNodes && nodesById.has(id)) displayedIds.add(id);
    });
    if (selectedEdge && visibleEdges().includes(selectedEdge)) {
        [edgeSource(selectedEdge), edgeTarget(selectedEdge)].forEach((id) => {
            if (displayedIds.size < maximumGraphNodes && nodesById.has(id)) displayedIds.add(id);
        });
    }

    const eligibleEdges = visibleEdges();
    const expansionEdges = [];
    while (displayedIds.size < maximumGraphNodes) {
        const candidates = eligibleEdges.flatMap((edge) => {
            const sourceSelected = displayedIds.has(edgeSource(edge));
            const targetSelected = displayedIds.has(edgeTarget(edge));
            if (sourceSelected === targetSelected) return [];
            const candidateId = sourceSelected ? edgeTarget(edge) : edgeSource(edge);
            if (!nodesById.has(candidateId)) return [];
            return [{ edge, candidateId }];
        });
        candidates.sort((left, right) => (
            (rankings.related.get(left.candidateId) ?? Number.MAX_SAFE_INTEGER)
            - (rankings.related.get(right.candidateId) ?? Number.MAX_SAFE_INTEGER)
            || edgeScore(right.edge) - edgeScore(left.edge)
            || left.candidateId.localeCompare(right.candidateId)
        ));
        if (!candidates.length) break;
        displayedIds.add(candidates[0].candidateId);
        expansionEdges.push(candidates[0].edge);
    }

    const graphNodes = allNodes.filter((node, index) => displayedIds.has(nodeId(node, index)));
    graphNodes.sort((left, right) => {
        const leftId = nodeId(left, allNodes.indexOf(left));
        const rightId = nodeId(right, allNodes.indexOf(right));
        const leftGroup = anchorIds.has(leftId) ? 0 : 1;
        const rightGroup = anchorIds.has(rightId) ? 0 : 1;
        return leftGroup - rightGroup
            || (leftGroup === 0
                ? (rankings.relevant.get(leftId) ?? Number.MAX_SAFE_INTEGER) - (rankings.relevant.get(rightId) ?? Number.MAX_SAFE_INTEGER)
                : (rankings.related.get(leftId) ?? Number.MAX_SAFE_INTEGER) - (rankings.related.get(rightId) ?? Number.MAX_SAFE_INTEGER));
    });

    const expansionIds = new Set(expansionEdges.map((edge) => edgeId(edge, edges().indexOf(edge))));
    const pathEdgeIds = selectedPathEdgeIds();
    const graphEdges = eligibleEdges
        .filter((edge) => displayedIds.has(edgeSource(edge)) && displayedIds.has(edgeTarget(edge)))
        .sort((left, right) => {
            const leftSelected = edgeId(left, edges().indexOf(left)) === state.selectedEdge ? 1 : 0;
            const rightSelected = edgeId(right, edges().indexOf(right)) === state.selectedEdge ? 1 : 0;
            const leftPath = pathEdgeIds.has(edgeId(left, edges().indexOf(left))) ? 1 : 0;
            const rightPath = pathEdgeIds.has(edgeId(right, edges().indexOf(right))) ? 1 : 0;
            const leftExpansion = expansionIds.has(edgeId(left, edges().indexOf(left))) ? 1 : 0;
            const rightExpansion = expansionIds.has(edgeId(right, edges().indexOf(right))) ? 1 : 0;
            return rightSelected - leftSelected || rightPath - leftPath || rightExpansion - leftExpansion || edgeScore(right) - edgeScore(left);
        })
        .slice(0, 16);
    return { anchorIds, graphEdges, graphNodes, rankings };
}

function verticalPositions(count) {
    if (count === 0) return [];
    if (count === 1) return [graphHeight / 2];
    const top = 68;
    const bottom = graphHeight - 68;
    const step = (bottom - top) / (count - 1);
    return Array.from({ length: count }, (_, index) => top + index * step);
}

function layoutGraph(graph) {
    const anchorNodes = graph.graphNodes.filter((node) => graph.anchorIds.has(nodeId(node, nodes().indexOf(node))));
    const relatedNodes = graph.graphNodes.filter((node) => !graph.anchorIds.has(nodeId(node, nodes().indexOf(node))));
    const anchorOrder = new Map(anchorNodes.map((node, index) => [nodeId(node, nodes().indexOf(node)), index]));
    relatedNodes.sort((left, right) => {
        const connectedOrder = (node) => {
            const id = nodeId(node, nodes().indexOf(node));
            const neighbourOrders = graph.graphEdges.flatMap((edge) => {
                if (edgeSource(edge) === id && anchorOrder.has(edgeTarget(edge))) return [anchorOrder.get(edgeTarget(edge))];
                if (edgeTarget(edge) === id && anchorOrder.has(edgeSource(edge))) return [anchorOrder.get(edgeSource(edge))];
                return [];
            });
            return neighbourOrders.length ? average(neighbourOrders) : Number.MAX_SAFE_INTEGER;
        };
        const leftId = nodeId(left, nodes().indexOf(left));
        const rightId = nodeId(right, nodes().indexOf(right));
        return connectedOrder(left) - connectedOrder(right)
            || (graph.rankings.related.get(leftId) ?? Number.MAX_SAFE_INTEGER) - (graph.rankings.related.get(rightId) ?? Number.MAX_SAFE_INTEGER);
    });

    const positions = new Map();
    const hasRelatedNodes = relatedNodes.length > 0;
    const anchorX = hasRelatedNodes ? 285 : 420;
    const relatedX = 715;
    verticalPositions(anchorNodes.length).forEach((y, index) => {
        positions.set(nodeId(anchorNodes[index], nodes().indexOf(anchorNodes[index])), { x: anchorX, y, labelSide: hasRelatedNodes ? "left" : "right" });
    });
    verticalPositions(relatedNodes.length).forEach((y, index) => {
        positions.set(nodeId(relatedNodes[index], nodes().indexOf(relatedNodes[index])), { x: relatedX, y, labelSide: "right" });
    });
    return positions;
}

function shortenedEdge(source, target, radius = 21) {
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const length = Math.hypot(dx, dy) || 1;
    return {
        x1: source.x + dx / length * radius,
        y1: source.y + dy / length * radius,
        x2: target.x - dx / length * radius,
        y2: target.y - dy / length * radius,
    };
}

function edgeColour(type) {
    return {
        CALLS: "#145fe4",
        CONTAINS: "#6646b8",
        CO_CHANGES: "#a15c00",
        IMPORTS: "#08735b",
        INSTANTIATES: "#b42318",
        TESTS: "#0b7a70",
    }[type] || "#5b6070";
}

function edgeLabelPoint(source, target, occupied, index) {
    const sameColumn = Math.abs(source.x - target.x) < 80;
    const base = {
        x: sameColumn ? source.x + (source.labelSide === "left" ? 82 : -82) : (source.x + target.x) / 2,
        y: (source.y + target.y) / 2,
    };
    const offsets = [0, -16, 16, -32, 32, -48, 48];
    for (const offset of offsets) {
        const candidate = { x: base.x + ((index % 3) - 1) * 26, y: Math.max(20, Math.min(graphHeight - 20, base.y + offset)) };
        if (occupied.every((point) => Math.hypot(point.x - candidate.x, point.y - candidate.y) >= 52)) {
            occupied.push(candidate);
            return candidate;
        }
    }
    occupied.push(base);
    return base;
}

function compactLabel(value, maximumLength = 30) {
    const label = text(value);
    return label.length > maximumLength ? `${label.slice(0, maximumLength - 3)}...` : label;
}

function renderGraph() {
    const viewportElement = $("#graph-viewport");
    if (!viewportElement) return;
    const viewport = clear(viewportElement);
    const graph = graphSlice();
    const positions = layoutGraph(graph);
    const pathNodeIds = selectedPathNodeIds();
    const pathEdgeIds = selectedPathEdgeIds();
    const edgeLabelPositions = [];

    const definitions = svgElement("defs");
    const arrow = svgElement("marker", { id: "graph-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse" });
    arrow.append(svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#74839a" }));
    definitions.append(arrow);
    viewport.append(definitions);

    const hasRelevant = graph.graphNodes.some((node) => graph.anchorIds.has(nodeId(node, nodes().indexOf(node))));
    const hasRelated = graph.graphNodes.some((node) => !graph.anchorIds.has(nodeId(node, nodes().indexOf(node))));
    if (hasRelevant) viewport.append(svgElement("text", { x: hasRelated ? 285 : 420, y: 26, class: "graph-column-label", "text-anchor": "middle" }, "Relevant matches"));
    if (hasRelated) viewport.append(svgElement("text", { x: 715, y: 26, class: "graph-column-label", "text-anchor": "middle" }, "Related by graph"));

    graph.graphEdges.forEach((edge, index) => {
        const source = positions.get(edgeSource(edge));
        const target = positions.get(edgeTarget(edge));
        if (!source || !target) return;
        const id = edgeId(edge, edges().indexOf(edge));
        const selected = id === state.selectedEdge || pathEdgeIds.has(id);
        const coordinates = shortenedEdge(source, target);
        const relationshipLabel = `${edgeType(edge)} from ${edgeSource(edge)} to ${edgeTarget(edge)}, ${Math.round(edgeScore(edge) * 100)}% strength`;
        const hitLine = svgElement("line", { ...coordinates, class: "graph-edge-hit", "aria-hidden": "true" });
        hitLine.addEventListener("click", () => selectEdge(id));
        viewport.append(hitLine);
        const line = svgElement("line", {
            ...coordinates,
            class: `graph-edge${selected ? " is-selected" : ""}`,
            "marker-end": "url(#graph-arrow)",
            tabindex: "0",
            role: "button",
            "aria-label": `Inspect ${relationshipLabel}`,
        });
        line.dataset.edgeId = id;
        line.style.stroke = selected ? "#0c47b7" : edgeColour(edgeType(edge));
        line.style.strokeWidth = String(selected ? 4 : 1.5 + edgeScore(edge) * 2);
        line.style.opacity = String(selected ? 1 : 0.55 + edgeScore(edge) * 0.4);
        line.append(svgElement("title", {}, relationshipLabel));
        line.addEventListener("click", () => selectEdge(id));
        line.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectEdge(id); } });
        viewport.append(line);

        if (selected) {
            const labelText = `${edgeType(edge)} ${Math.round(edgeScore(edge) * 100)}%`;
            const labelPoint = edgeLabelPoint(source, target, edgeLabelPositions, index);
            const labelWidth = labelText.length * 6.1 + 12;
            viewport.append(svgElement("rect", { x: labelPoint.x - labelWidth / 2, y: labelPoint.y - 11, width: labelWidth, height: 18, rx: 5, fill: "#ffffff", stroke: "#d7d7db", opacity: 0.94, "pointer-events": "none" }));
            const label = svgElement("text", { x: labelPoint.x, y: labelPoint.y + 2, class: "edge-label", "text-anchor": "middle" }, labelText);
            label.style.display = "block";
            viewport.append(label);
        }
    });

    graph.graphNodes.forEach((node) => {
        const index = nodes().indexOf(node);
        const id = nodeId(node, index);
        const point = positions.get(id);
        if (!point) return;
        const isAnchor = graph.anchorIds.has(id);
        const rank = isAnchor ? graph.rankings.relevant.get(id) : graph.rankings.related.get(id);
        const selected = state.selectedNode === id || pathNodeIds.has(id);
        const maximumLabelLength = window.matchMedia("(max-width: 620px)").matches ? 12 : 30;
        const labelX = point.labelSide === "left" ? -29 : 29;
        const textAnchor = point.labelSide === "left" ? "end" : "start";
        const group = svgElement("g", {
            transform: `translate(${point.x} ${point.y})`,
            class: `graph-node${selected ? " is-selected" : ""}`,
            tabindex: "0",
            role: "button",
            "aria-label": `Inspect ${nodeLabel(node, index)}`,
            "data-node-type": text(node.type || node.kind, "node").toLowerCase(),
            "data-rank-group": isAnchor ? "relevant" : "related",
        });
        group.addEventListener("click", () => selectNode(id));
        group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectNode(id); } });
        group.append(svgElement("title", {}, `${nodeLabel(node, index)}\n${text(node.path || node.file || node.module, "")}`));
        group.append(svgElement("circle", { r: 34, class: "node-hit", "aria-hidden": "true" }));
        group.append(svgElement("circle", { r: 19, class: "node-glyph", "aria-hidden": "true" }));
        group.append(svgElement("text", { x: 0, y: 5, "text-anchor": "middle" }, rank ?? ""));
        group.append(svgElement("text", { x: labelX, y: 1, "text-anchor": textAnchor }, compactLabel(nodeLabel(node, index), maximumLabelLength)));
        group.append(svgElement("text", { x: labelX, y: 18, "text-anchor": textAnchor, class: "node-type" }, `${isAnchor ? "Relevant" : "Related"} ${rank ?? ""} · ${text(node.type || node.kind, "node")}`));
        viewport.append(group);
    });

    const count = $("#graph-count");
    if (count) {
        const matchingRelationships = visibleEdges().length;
        count.textContent = matchingRelationships
            ? `${graph.graphNodes.length} nodes · ${graph.graphEdges.length} of ${matchingRelationships} relationships shown`
            : `${graph.graphNodes.length} nodes · no matching relationships`;
    }
    const description = $("#graph-description");
    if (description) description.textContent = `The graph shows ${graph.graphNodes.length} nodes and ${graph.graphEdges.length} relationships after applying the current filters.`;
    applyViewportTransform();
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
    const pathList = $("#path-list");
    if (!pathList) return;
    const holder = clear(pathList);
    if (!paths().length) { holder.append(element("span", "inspector-content", "No recorded paths are available.")); return; }
    paths().slice(0, 6).forEach((path, index) => {
        const label = path.label || path.summary || path.name || safeArray(path.nodes || path.node_ids || path).map(String).join(" → ");
        const button = element("button", null, label || `Path ${index + 1}`);
        const id = pathId(path, index);
        button.type = "button"; button.dataset.pathId = id; button.setAttribute("aria-pressed", String(id === state.selectedPath));
        holder.append(button);
    });
}

function renderInspector() {
    const heading = $("#inspector-heading");
    const inspectorContent = $("#inspector-content");
    if (!heading || !inspectorContent) return;
    const content = clear(inspectorContent);
    let selected = null;
    if (state.selectedNode) selected = findNode(state.selectedNode);
    if (selected) {
        const index = nodes().indexOf(selected);
        const id = nodeId(selected, index);
        const rankings = graphRankings();
        const relevantRank = rankings.relevant.get(id);
        const relatedRank = rankings.related.get(id);
        heading.textContent = nodeLabel(selected, index);
        appendAttributes(content, [
            ["Result", relevantRank ? `Relevant #${relevantRank}` : relatedRank ? `Related #${relatedRank}` : null],
            ["Type", text(selected.type || selected.kind, "node")],
            ["Score", formatScore(selected.score ?? selected.relevance)],
            ["Location", sourceLocation(selected)],
        ]);
        return;
    }
    if (state.selectedEdge) {
        const edge = edges().find((item, index) => edgeId(item, index) === state.selectedEdge);
        if (edge) {
            heading.textContent = edgeType(edge);
            appendAttributes(content, [
                ["From", nodeName(edgeSource(edge))],
                ["To", nodeName(edgeTarget(edge))],
                ["Strength", `${Math.round(edgeScore(edge) * 100)}%`],
                ["Evidence", safeArray(edge.evidence).join(", ")],
            ]);
            return;
        }
    }
    const path = findPath(state.selectedPath);
    if (path) {
        const relationships = [...new Set(safeArray(path.steps).flatMap((step) => safeArray(step?.contributions).map((contribution) => contribution.kind)).filter(Boolean))];
        heading.textContent = path.label || path.name || "Relationship path";
        appendAttributes(content, [
            ["Score", formatScore(path.score ?? path.strength)],
            ["Nodes", safeArray(path.nodes || path.node_ids).length],
            ["Relationships", relationships.join(", ")],
        ]);
        return;
    }
    heading.textContent = "Details";
    content.textContent = "Select a node or relationship.";
}

function sourceLocation(node) {
    const path = text(node.path || node.file || node.module, "");
    const start = numberOrNull(node.start_line);
    const end = numberOrNull(node.end_line);
    if (!path) return null;
    if (start === null) return path;
    return end !== null && end !== start ? `${path}:${start}-${end}` : `${path}:${start}`;
}

function nodeName(id) {
    const node = findNode(id);
    return node ? nodeLabel(node, nodes().indexOf(node)) : id;
}

function appendAttributes(container, entries) {
    const list = element("dl", "inspector-list");
    entries.filter(([, value]) => value !== null && value !== undefined && value !== "").forEach(([key, value]) => {
        const row = element("div"); const name = element("dt", null, key); const detail = element("dd");
        detail.textContent = text(value);
        row.append(name, detail); list.append(row);
    });
    container.append(list);
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
    const rankedResults = $("#ranked-results");
    if (!rankedResults) return;
    const holder = clear(rankedResults);
    const groups = rankedGroups();
    let count = 0;
    Object.entries(groups).forEach(([group, rows]) => {
        if (!rows.length) return;
        const section = element("section", "result-group");
        section.append(element("h3", null, group === "relevant" ? "Relevant" : "Related by graph"));
        const list = element("ol", "result-list");
        rows.slice(0, 6).forEach((row, index) => {
            const id = rankedNodeId(row);
            const item = element("li", `ranked-result${id === state.selectedNode ? " is-selected" : ""}`);
            item.append(element("span", "result-rank", `#${row.rank || index + 1}`));
            if (id && findNode(id)) {
                const button = element("button", "result-button", rankedLabel(row, index));
                button.type = "button";
                button.dataset.nodeId = id;
                if (typeof row === "object" && row.path) button.title = row.path;
                if (id === state.selectedNode) button.setAttribute("aria-current", "true");
                item.append(button);
            } else {
                item.append(element("span", "result-label", rankedLabel(row, index)));
            }
            item.append(element("span", "result-score", formatScore(typeof row === "object" ? row.score ?? row.relevance : undefined)));
            list.append(item);
            count += 1;
        });
        section.append(list);
        holder.append(section);
    });
    if (!count) holder.append(element("p", "inspector-content", "No ranked results were recorded."));
}

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
        ["Overall recall at 10", "recall_at_10", null, null],
        ["Overall recall at 20", "recall_at_20", null, null],
        ["Supporting recall at 10", "supporting_recall_at_10", null, null],
        ["Answer MRR at 10", "answer_mrr_at_10", summary.lexical?.score ?? fallbackLexical, summary.graph?.score ?? fallbackGraph],
    ];
    const comparisons = definitions.map(([title, key, lexicalFallback, graphFallback]) => ({
        title,
        key,
        lexical: numberOrNull(lexicalMetrics[key]) ?? lexicalFallback,
        graph: numberOrNull(graphMetrics[key]) ?? graphFallback,
    }));
    const metricGrid = $("#metric-grid");
    if (metricGrid) {
        clear(metricGrid);
        comparisons.forEach(({ title, lexical, graph }) => comparisonMetricCard(metricGrid, title, lexical, graph));
    }
    const answer = comparisons.find((comparison) => comparison.key === "answer_mrr_at_10");
    const recallGains = comparisons.filter((comparison) => comparison.key !== "answer_mrr_at_10" && comparison.graph > comparison.lexical);
    const answerUnchanged = answer?.lexical !== null && answer?.graph !== null && Math.abs(answer.graph - answer.lexical) < 0.0005;
    const conclusion = $("#evaluation-conclusion");
    if (conclusion) {
        conclusion.textContent = answerUnchanged && recallGains.length
            ? "Graph expansion improves overall and supporting recall. Answer MRR at 10 is unchanged, so the graph adds context without improving the first answer's rank in this benchmark."
            : text(summary.conclusion, "The artifact doesn't include an evaluation conclusion.");
    }
    renderMisses();
}

function average(values) { const numbers = values.filter((item) => item !== null && Number.isFinite(item)); return numbers.length ? numbers.reduce((total, item) => total + item, 0) / numbers.length : null; }
function numberOrNull(value) { if (value === null || value === undefined || value === "") return null; const parsed = Number(value); return Number.isFinite(parsed) ? parsed : null; }
function formatMetric(value) { return value === null || value === undefined ? "-" : `${(Number(value) * 100).toFixed(1)}%`; }
function formatDelta(value) { return value === null || value === undefined ? "-" : `${value >= 0 ? "+" : ""}${(Number(value) * 100).toFixed(1)} pp`; }
function comparisonMetricCard(container, title, lexical, graph) {
    const card = element("article", "metric-card");
    card.append(element("small", null, title));
    const comparison = element("div", "metric-comparison");
    const lexicalGroup = element("div");
    lexicalGroup.append(element("span", null, "Lexical"), element("strong", null, formatMetric(lexical)));
    const graphGroup = element("div");
    graphGroup.append(element("span", null, "Graph"), element("strong", null, formatMetric(graph)));
    comparison.append(lexicalGroup, graphGroup);
    const delta = lexical !== null && graph !== null ? graph - lexical : null;
    const unchanged = delta !== null && Math.abs(delta) < 0.0005;
    const change = element(
        "p",
        delta !== null && !unchanged ? delta > 0 ? "positive" : "negative" : "",
        unchanged ? "Unchanged" : `Change ${formatDelta(delta)}`,
    );
    card.append(comparison, change);
    container.append(card);
}

function renderMisses() {
    const missesList = $("#misses-list");
    if (!missesList) return;
    const holder = clear(missesList);
    const misses = safeArray(state.evaluation?.misses || state.evaluation?.disagreements || state.evaluation?.failures || state.evaluation?.error_cases)
        .filter((miss) => miss.regression || safeArray(miss.newly_retrieved_at_10).length || safeArray(miss.newly_missed_at_10).length);
    if (!misses.length) { holder.append(element("p", "inspector-content", "No misses or disagreements were recorded in this evaluation artifact.")); return; }
    const summary = $(".misses-card > summary");
    if (summary) summary.textContent = `Where graph retrieval changed (${misses.length})`;
    misses.forEach((miss, index) => {
        const card = element("article", `miss-card${miss.regression ? " is-regression" : ""}`);
        card.append(
            element("strong", null, text(miss.query || miss.label || miss.query_id || miss.id, `Case ${index + 1}`)),
            element("p", null, text(miss.reason || miss.description || miss.message || miss.miss)),
        );
        const groups = [
            ["Newly retrieved by graph", miss.newly_retrieved_at_10],
            ["Newly missed by graph", miss.newly_missed_at_10],
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

function renderProvenance(holder, provenance) {
    clear(holder);
    const values = provenance && typeof provenance === "object" ? provenance : {};
    const repositoryName = state.repository?.name || state.repository?.repository || selectedRepositoryEntry()?.label || selectedRepositoryEntry()?.id;
    const commit = shortHash(values.snapshot || values.commit || state.repository?.commit || state.repository?.revision);
    if (repositoryName) {
        const source = element("span");
        source.append(element("strong", null, repositoryName));
        if (commit) source.append(document.createTextNode(` @ ${commit}`));
        holder.append(source);
    }
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
