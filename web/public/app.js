"use strict";

const WEB_DATA_SCHEMA_VERSION = 3;
const REPOSITORY_GRAPH_ID = "repository";
const VALID_VIEWS = new Set(["graph", "comparison"]);
const AGGREGATE_METRICS = [
    ["answer_mrr_at_10", "Answer MRR at 10"],
    ["recall_at_10", "Recall at 10"],
    ["recall_at_20", "Recall at 20"],
    ["supporting_recall_at_10", "Supporting recall at 10"],
];

const state = {
    manifest: null,
    repository: null,
    repositoryInspection: null,
    evaluation: null,
    queries: [],
    queryCache: new Map(),
    snapshot: {},
    view: "graph",
    graphId: REPOSITORY_GRAPH_ID,
    comparisonQueryId: "",
    activeInspection: null,
    selectedNodeId: "",
    graphRendered: false,
    renderedGraphId: "",
    graphPlotQueue: Promise.resolve(),
    graphRenderToken: 0,
    neighborhoodRendered: false,
    neighborhoodPlotQueue: Promise.resolve(),
    inspectorRenderToken: 0,
    resizeFrame: 0,
};

function requiredElement(selector) {
    const item = document.querySelector(selector);
    if (!item) throw new Error(`The page is missing required element ${selector}.`);
    return item;
}

function createElement(name, className, content) {
    const item = document.createElement(name);
    if (className) item.className = className;
    if (content !== undefined && content !== null) item.textContent = String(content);
    return item;
}

function clear(item) {
    item.replaceChildren();
    return item;
}

function safeArray(value) {
    return Array.isArray(value) ? value : [];
}

function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

function finiteNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") {
        return null;
    }
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function assetValue(value) {
    if (typeof value === "string") return value;
    if (!isObject(value)) return "";
    return value.file || value.path || value.url || value.asset || "";
}

function dataPath(value, fallback = "") {
    const raw = assetValue(value) || fallback;
    if (!raw) return "";
    if (raw.startsWith("/")) return raw;
    return `/data/${raw.replace(/^data\//, "")}`;
}

async function loadJson(url) {
    let response;
    try {
        response = await fetch(url, { cache: "no-cache", credentials: "same-origin" });
    } catch (error) {
        throw new Error(`Could not request ${url}: ${error.message}`);
    }
    if (!response.ok) throw new Error(`Could not load ${url} (${response.status}).`);
    try {
        return await response.json();
    } catch (error) {
        throw new Error(`${url} is not valid JSON: ${error.message}`);
    }
}

function normalizeQueries(manifest) {
    const entries = safeArray(manifest.queries);
    return entries.map((entry, index) => {
        const value = typeof entry === "string" ? { file: entry } : isObject(entry) ? entry : {};
        const id = String(value.id || `query-${index + 1}`);
        return {
            id,
            label: String(value.label || value.query || id),
            file: assetValue(value),
        };
    });
}

function repositoryAsset(manifest) {
    const candidates = safeArray(manifest.repositories);
    return manifest.repository || candidates[0] || "repository.json";
}

function validateSchema(label, payload) {
    if (!isObject(payload)) throw new Error(`${label} is not an object.`);
    if (payload.schema_version !== WEB_DATA_SCHEMA_VERSION) {
        throw new Error(
            `${label} uses schema ${payload.schema_version}; schema ${WEB_DATA_SCHEMA_VERSION} is required.`,
        );
    }
}

function snapshotFields(payload) {
    const provenance = isObject(payload?.provenance) ? payload.provenance : {};
    const repository = isObject(payload?.repository) ? payload.repository : {};
    return {
        repository: provenance.repository || repository.name || payload?.name || "",
        commit: provenance.commit || repository.commit || payload?.commit || "",
        tree: provenance.tree || payload?.tree || "",
        indexed_source_sha256: provenance.indexed_source_sha256 || "",
    };
}

function validateSnapshot(label, payload) {
    const candidate = snapshotFields(payload);
    if (!candidate.commit) throw new Error(`${label} does not identify its source commit.`);
    for (const key of Object.keys(candidate)) {
        if (!candidate[key]) continue;
        if (state.snapshot[key] && state.snapshot[key] !== candidate[key]) {
            throw new Error(`${label} does not match the recorded ${key.replaceAll("_", " ")}.`);
        }
        if (!state.snapshot[key]) state.snapshot[key] = candidate[key];
    }
}

function plotly() {
    const library = window.Plotly;
    if (!library || typeof library.newPlot !== "function" || typeof library.react !== "function") {
        throw new Error("The local Plotly library could not be loaded.");
    }
    return library;
}

function validateFigure(label, figure) {
    if (
        !isObject(figure)
        || !Array.isArray(figure.data)
        || !isObject(figure.layout)
        || !isObject(figure.config)
    ) {
        throw new Error(`${label} does not contain a complete Plotly figure.`);
    }
    if (typeof figure.plotly_js_version !== "string" || !figure.plotly_js_version) {
        throw new Error(`${label} does not record its Plotly.js version.`);
    }
    const loadedVersion = String(plotly().version || "");
    if (loadedVersion && loadedVersion !== figure.plotly_js_version) {
        throw new Error(
            `${label} requires Plotly.js ${figure.plotly_js_version}, but ${loadedVersion} is loaded.`,
        );
    }
}

function validateInspection(label, payload) {
    const inspection = payload?.inspection;
    if (!isObject(inspection) || !Array.isArray(inspection.nodes) || !Array.isArray(inspection.edges)) {
        throw new Error(`${label} does not contain graph inspection data.`);
    }
    const nodeById = new Map();
    for (const node of inspection.nodes) {
        if (!isObject(node) || typeof node.id !== "string" || !node.id) {
            throw new Error(`${label} contains an invalid inspection node.`);
        }
        if (nodeById.has(node.id)) {
            throw new Error(`${label} contains duplicate inspection node '${node.id}'.`);
        }
        nodeById.set(node.id, node);
    }
    if (!nodeById.size) throw new Error(`${label} does not contain any inspectable nodes.`);

    for (const edge of inspection.edges) {
        if (
            !isObject(edge)
            || typeof edge.source_id !== "string"
            || typeof edge.target_id !== "string"
            || typeof edge.kind !== "string"
            || finiteNumber(edge.strength) === null
        ) {
            throw new Error(`${label} contains an invalid inspection edge.`);
        }
        if (!nodeById.has(edge.source_id) || !nodeById.has(edge.target_id)) {
            throw new Error(`${label} contains an inspection edge with a missing endpoint.`);
        }
    }
    return { nodes: inspection.nodes, edges: inspection.edges, nodeById };
}

async function renderFigure(host, figure, label, useReact) {
    validateFigure(label, figure);
    const library = plotly();
    if (useReact) {
        await library.react(host, figure.data, figure.layout, figure.config);
    } else {
        await library.newPlot(host, figure.data, figure.layout, figure.config);
    }
}

function selectedGraphEntry() {
    return state.queries.find((entry) => entry.id === state.graphId) || null;
}

function evaluationQueries() {
    return safeArray(state.evaluation?.queries);
}

function selectedEvaluationQuery() {
    return evaluationQueries().find((query) => query.id === state.comparisonQueryId) || null;
}

function readHash() {
    const parameters = new URLSearchParams(location.hash.slice(1));
    const requestedView = parameters.get("view");
    const requestedGraph = parameters.get("graph");
    const requestedComparison = parameters.get("comparison");
    state.view = VALID_VIEWS.has(requestedView) ? requestedView : "graph";
    state.graphId = requestedGraph === REPOSITORY_GRAPH_ID
        || state.queries.some((entry) => entry.id === requestedGraph)
        ? requestedGraph
        : REPOSITORY_GRAPH_ID;
    state.comparisonQueryId = evaluationQueries().some(
        (query) => query.id === requestedComparison,
    )
        ? requestedComparison
        : evaluationQueries()[0]?.id || "";
}

function writeHash() {
    const parameters = new URLSearchParams({
        view: state.view,
        graph: state.graphId,
    });
    if (state.comparisonQueryId) parameters.set("comparison", state.comparisonQueryId);
    const nextHash = `#${parameters.toString()}`;
    if (location.hash !== nextHash) history.replaceState(null, "", nextHash);
}

function populateGraphSelect() {
    const select = requiredElement("#graph-select");
    const fragment = document.createDocumentFragment();
    const repositoryOption = createElement("option", null, "Repository overview");
    repositoryOption.value = REPOSITORY_GRAPH_ID;
    fragment.append(repositoryOption);
    for (const query of state.queries) {
        const option = createElement("option", null, query.label);
        option.value = query.id;
        fragment.append(option);
    }
    clear(select).append(fragment);
}

function populateComparisonSelect() {
    const select = requiredElement("#comparison-query-select");
    const fragment = document.createDocumentFragment();
    for (const query of evaluationQueries()) {
        const option = createElement("option", null, query.query || query.id);
        option.value = query.id;
        fragment.append(option);
    }
    clear(select).append(fragment);
    select.disabled = !evaluationQueries().length;
}

function syncControls() {
    const graphSelect = requiredElement("#graph-select");
    if (graphSelect.value !== state.graphId) graphSelect.value = state.graphId;
    const comparisonSelect = requiredElement("#comparison-query-select");
    if (comparisonSelect.value !== state.comparisonQueryId) {
        comparisonSelect.value = state.comparisonQueryId;
    }
}

function setViewControls() {
    document.querySelectorAll("[data-view-panel]").forEach((panel) => {
        panel.hidden = panel.dataset.viewPanel !== state.view;
    });
    document.querySelectorAll("button[data-view]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
    });
}

function showError(error) {
    const holder = requiredElement("#error-state");
    holder.textContent = error instanceof Error ? error.message : String(error);
    holder.hidden = false;
    requiredElement("#loading-state").hidden = true;
}

function clearError() {
    const holder = requiredElement("#error-state");
    holder.textContent = "";
    holder.hidden = true;
}

function schedulePlotResize() {
    window.cancelAnimationFrame(state.resizeFrame);
    state.resizeFrame = window.requestAnimationFrame(() => {
        state.resizeFrame = window.requestAnimationFrame(() => {
            const library = window.Plotly;
            if (!library?.Plots || typeof library.Plots.resize !== "function") return;
            if (state.view === "graph" && state.graphRendered) {
                library.Plots.resize(requiredElement("#graph-figure"));
            }
            const inspector = requiredElement("#node-inspector");
            if (state.view === "graph" && !inspector.hidden && state.neighborhoodRendered) {
                library.Plots.resize(requiredElement("#neighborhood-figure"));
            }
        });
    });
}

function loadQuery(entry) {
    if (state.queryCache.has(entry.id)) return state.queryCache.get(entry.id);
    if (!entry.file) {
        return Promise.reject(new Error(`Recorded query '${entry.label}' does not declare a data file.`));
    }
    const promise = loadJson(dataPath(entry.file)).then((payload) => {
        validateSchema(`Recorded query '${entry.label}'`, payload);
        if (payload.id !== entry.id) {
            throw new Error(`Recorded query '${entry.label}' loaded the wrong artifact.`);
        }
        validateSnapshot(`Recorded query '${entry.label}'`, payload);
        validateFigure(`Recorded query '${entry.label}'`, payload.figure);
        return {
            payload,
            inspection: validateInspection(`Recorded query '${entry.label}'`, payload),
        };
    }).catch((error) => {
        state.queryCache.delete(entry.id);
        throw error;
    });
    state.queryCache.set(entry.id, promise);
    return promise;
}

function clearInspector() {
    state.selectedNodeId = "";
    state.activeInspection = null;
    state.inspectorRenderToken += 1;
    for (const selector of [
        "#inspector-title",
        "#inspector-location",
        "#inspector-kind",
        "#inspector-signature",
        "#inspector-docstring",
        "#inspector-direct-score",
        "#inspector-relationship-score",
    ]) {
        requiredElement(selector).textContent = "";
    }
    clear(requiredElement("#relationship-list"));
    requiredElement("#inspector-scores").hidden = true;
    requiredElement("#inspector-signature-section").hidden = true;
    requiredElement("#inspector-docstring-section").hidden = true;
    requiredElement("#node-inspector").hidden = true;
}

function showGraphQuery(payload, isRepository) {
    const holder = requiredElement("#graph-query");
    holder.textContent = isRepository ? "" : String(payload.query || "");
    holder.hidden = isRepository;
}

function sizeGraphHost(host, figure) {
    const recordedHeight = finiteNumber(figure?.layout?.height);
    const height = recordedHeight === null ? 720 : Math.max(320, Math.min(1200, recordedHeight));
    host.style.height = `${height}px`;
    host.style.minHeight = `${height}px`;
}

function clearGraphQuery() {
    const holder = requiredElement("#graph-query");
    holder.textContent = "";
    holder.hidden = true;
}

function bindMainNodeClicks() {
    const host = requiredElement("#graph-figure");
    if (host.dataset.nodeClicksBound === "true") return;
    if (typeof host.on !== "function") {
        throw new Error("The main Plotly figure does not support click events.");
    }
    host.on("plotly_click", (event) => {
        if (host.getAttribute("aria-busy") === "true") return;
        const point = safeArray(event?.points)[0];
        const nodeId = typeof point?.customdata === "string" ? point.customdata : "";
        if (!nodeId || !state.activeInspection?.nodeById.has(nodeId)) return;
        selectInspectionNode(nodeId, { scroll: true }).catch(showError);
    });
    host.dataset.nodeClicksBound = "true";
}

async function renderActiveGraph() {
    if (
        state.graphRendered
        && state.renderedGraphId === state.graphId
        && state.activeInspection
    ) {
        schedulePlotResize();
        return;
    }
    const token = ++state.graphRenderToken;
    const graphId = state.graphId;
    const host = requiredElement("#graph-figure");
    clearInspector();
    clearGraphQuery();
    host.setAttribute("aria-busy", "true");

    try {
        let payload;
        let inspection;
        let label;
        const isRepository = graphId === REPOSITORY_GRAPH_ID;
        if (isRepository) {
            payload = state.repository;
            inspection = state.repositoryInspection;
            label = "The repository artifact";
        } else {
            const entry = selectedGraphEntry();
            if (!entry) throw new Error(`Unknown recorded graph '${graphId}'.`);
            const loaded = await loadQuery(entry);
            payload = loaded.payload;
            inspection = loaded.inspection;
            label = `Recorded query '${entry.label}'`;
        }

        if (token !== state.graphRenderToken || state.view !== "graph" || state.graphId !== graphId) {
            return;
        }
        showGraphQuery(payload, isRepository);
        sizeGraphHost(host, payload.figure);
        state.graphPlotQueue = state.graphPlotQueue.catch(() => {}).then(async () => {
            if (
                token !== state.graphRenderToken
                || state.view !== "graph"
                || state.graphId !== graphId
            ) {
                return false;
            }
            await renderFigure(host, payload.figure, label, state.graphRendered);
            state.graphRendered = true;
            state.renderedGraphId = graphId;
            bindMainNodeClicks();
            if (
                token !== state.graphRenderToken
                || state.view !== "graph"
                || state.graphId !== graphId
            ) {
                return false;
            }
            state.activeInspection = inspection;
            return true;
        });
        const rendered = await state.graphPlotQueue;
        if (!rendered || token !== state.graphRenderToken || state.view !== "graph") return;
        schedulePlotResize();
    } catch (error) {
        if (token !== state.graphRenderToken || state.view !== "graph" || state.graphId !== graphId) {
            return;
        }
        throw error;
    } finally {
        if (token === state.graphRenderToken) host.removeAttribute("aria-busy");
    }
}

function compareText(left, right) {
    if (left < right) return -1;
    if (left > right) return 1;
    return 0;
}

function nodeQueryRelevance(node) {
    return Math.max(
        finiteNumber(node.direct_relevance) || 0,
        finiteNumber(node.relationship_strength) || 0,
    );
}

function edgePreference(left, right) {
    const strengthDifference = finiteNumber(right.strength) - finiteNumber(left.strength);
    if (strengthDifference) return strengthDifference;
    return compareText(
        `${left.kind}\u0000${left.source_id}\u0000${left.target_id}`,
        `${right.kind}\u0000${right.source_id}\u0000${right.target_id}`,
    );
}

function neighborhoodFor(nodeId) {
    const inspection = state.activeInspection;
    if (!inspection) return [];
    const strongestByNeighbor = new Map();
    for (const edge of inspection.edges) {
        let neighborId = "";
        let direction = "";
        if (edge.source_id === nodeId && edge.target_id !== nodeId) {
            neighborId = edge.target_id;
            direction = "outgoing";
        } else if (edge.target_id === nodeId && edge.source_id !== nodeId) {
            neighborId = edge.source_id;
            direction = "incoming";
        }
        if (!neighborId) continue;
        const node = inspection.nodeById.get(neighborId);
        if (!node) continue;
        const candidate = { node, edge, direction };
        const current = strongestByNeighbor.get(neighborId);
        if (!current || edgePreference(edge, current.edge) < 0) {
            strongestByNeighbor.set(neighborId, candidate);
        }
    }
    return [...strongestByNeighbor.values()].sort((left, right) => {
        const strengthDifference = finiteNumber(right.edge.strength)
            - finiteNumber(left.edge.strength);
        if (strengthDifference) return strengthDifference;
        const relevanceDifference = nodeQueryRelevance(right.node) - nodeQueryRelevance(left.node);
        if (relevanceDifference) return relevanceDifference;
        return compareText(left.node.id, right.node.id);
    }).slice(0, 8);
}

function inspectorLocation(node) {
    if (!node.path) return "Not recorded";
    const start = finiteNumber(node.start_line);
    const end = finiteNumber(node.end_line);
    if (start === null) return String(node.path);
    if (end !== null && end !== start) return `${node.path}:${start}-${end}`;
    return `${node.path}:${start}`;
}

function formatInspectorScore(value) {
    const score = finiteNumber(value);
    return score === null ? "Not available" : score.toFixed(4);
}

function shortNodeLabel(node) {
    const raw = String(node.label || node.qualified_name || node.id);
    return raw.length > 28 ? `${raw.slice(0, 25)}...` : raw;
}

function relationshipDescription(candidate) {
    const direction = candidate.direction === "outgoing" ? "outgoing" : "incoming";
    return `${candidate.edge.kind} | strength ${finiteNumber(candidate.edge.strength).toFixed(3)} | ${direction}`;
}

function renderRelationshipList(candidates) {
    const holder = clear(requiredElement("#relationship-list"));
    if (!candidates.length) {
        holder.append(createElement("p", "relationship-empty", "No displayed relationships."));
        return;
    }
    for (const candidate of candidates) {
        const button = createElement("button", "relationship-entry");
        button.type = "button";
        button.dataset.nodeId = candidate.node.id;
        button.append(
            createElement(
                "strong",
                "relationship-node",
                candidate.node.qualified_name || candidate.node.label || candidate.node.id,
            ),
            createElement(
                "span",
                "relationship-detail",
                relationshipDescription(candidate),
            ),
        );
        holder.append(button);
    }
}

function neighborhoodFigure(center, candidates) {
    const positions = new Map([[center.id, [0, 0]]]);
    const count = candidates.length;
    candidates.forEach((candidate, index) => {
        const angle = -Math.PI / 2 + (2 * Math.PI * index) / Math.max(1, count);
        positions.set(candidate.node.id, [Math.cos(angle), Math.sin(angle)]);
    });

    const traces = candidates.map((candidate) => {
        const [x, y] = positions.get(candidate.node.id);
        const strength = finiteNumber(candidate.edge.strength);
        return {
            type: "scatter",
            mode: "lines",
            x: [0, x],
            y: [0, y],
            line: { color: "#94a3b8", width: 1 + 3 * strength },
            hoverinfo: "skip",
            showlegend: false,
        };
    });

    if (candidates.length) {
        traces.push({
            type: "scatter",
            mode: "markers+text",
            x: candidates.map((candidate) => positions.get(candidate.node.id)[0]),
            y: candidates.map((candidate) => positions.get(candidate.node.id)[1]),
            text: candidates.map((candidate) => shortNodeLabel(candidate.node)),
            textposition: "top center",
            textfont: { color: "#334155", size: 10 },
            customdata: candidates.map((candidate) => candidate.node.id),
            marker: {
                color: candidates.map((candidate) => candidate.node.color || "#38bdf8"),
                line: { color: "#ffffff", width: 1.5 },
                size: candidates.map((candidate) => Math.max(
                    13,
                    Math.min(24, finiteNumber(candidate.node.size) || 16),
                )),
            },
            hovertext: candidates.map(
                (candidate) => candidate.node.qualified_name || candidate.node.id,
            ),
            hovertemplate: "%{hovertext}<extra></extra>",
            showlegend: false,
        });
    }
    traces.push({
        type: "scatter",
        mode: "markers+text",
        x: [0],
        y: [0],
        text: [shortNodeLabel(center)],
        textposition: "bottom center",
        textfont: { color: "#0f172a", size: 11 },
        customdata: [center.id],
        marker: {
            color: center.color || "#fb7185",
            line: { color: "#0f172a", width: 2 },
            size: 24,
        },
        hovertext: [center.qualified_name || center.id],
        hovertemplate: "%{hovertext}<extra></extra>",
        showlegend: false,
    });

    return {
        data: traces,
        layout: {
            height: 340,
            margin: { b: 28, l: 28, r: 28, t: 28 },
            paper_bgcolor: "#ffffff",
            plot_bgcolor: "#ffffff",
            hovermode: "closest",
            dragmode: false,
            showlegend: false,
            xaxis: {
                fixedrange: true,
                range: [-1.35, 1.35],
                showgrid: false,
                showticklabels: false,
                zeroline: false,
            },
            yaxis: {
                fixedrange: true,
                range: [-1.35, 1.35],
                scaleanchor: "x",
                scaleratio: 1,
                showgrid: false,
                showticklabels: false,
                zeroline: false,
            },
            uirevision: `neighborhood:${center.id}`,
        },
        config: { displaylogo: false, displayModeBar: false, responsive: true },
    };
}

function bindNeighborhoodNodeClicks() {
    const host = requiredElement("#neighborhood-figure");
    if (host.dataset.nodeClicksBound === "true") return;
    if (typeof host.on !== "function") {
        throw new Error("The neighborhood Plotly figure does not support click events.");
    }
    host.on("plotly_click", (event) => {
        const point = safeArray(event?.points)[0];
        const nodeId = typeof point?.customdata === "string" ? point.customdata : "";
        if (!nodeId || !state.activeInspection?.nodeById.has(nodeId)) return;
        selectInspectionNode(nodeId, { scroll: false }).catch(showError);
    });
    host.dataset.nodeClicksBound = "true";
}

async function renderNeighborhood(center, candidates, token) {
    const figure = neighborhoodFigure(center, candidates);
    const host = requiredElement("#neighborhood-figure");
    state.neighborhoodPlotQueue = state.neighborhoodPlotQueue.catch(() => {}).then(async () => {
        if (token !== state.inspectorRenderToken || state.selectedNodeId !== center.id) return false;
        if (state.neighborhoodRendered) {
            await plotly().react(host, figure.data, figure.layout, figure.config);
        } else {
            await plotly().newPlot(host, figure.data, figure.layout, figure.config);
            state.neighborhoodRendered = true;
            bindNeighborhoodNodeClicks();
        }
        return token === state.inspectorRenderToken && state.selectedNodeId === center.id;
    });
    try {
        const rendered = await state.neighborhoodPlotQueue;
        if (rendered) schedulePlotResize();
    } catch (error) {
        if (token !== state.inspectorRenderToken || state.selectedNodeId !== center.id) return;
        throw error;
    }
}

async function selectInspectionNode(nodeId, options) {
    const node = state.activeInspection?.nodeById.get(nodeId);
    if (!node) return;
    const signature = typeof node.signature === "string" ? node.signature.trim() : "";
    const docstring = typeof node.docstring === "string" ? node.docstring.trim() : "";
    state.selectedNodeId = nodeId;
    const token = ++state.inspectorRenderToken;
    requiredElement("#inspector-title").textContent = node.qualified_name || node.label || node.id;
    requiredElement("#inspector-location").textContent = inspectorLocation(node);
    requiredElement("#inspector-kind").textContent = node.kind || "Not recorded";
    requiredElement("#inspector-signature").textContent = signature;
    requiredElement("#inspector-docstring").textContent = docstring;
    requiredElement("#inspector-scores").hidden = state.graphId === REPOSITORY_GRAPH_ID;
    requiredElement("#inspector-signature-section").hidden = !signature;
    requiredElement("#inspector-docstring-section").hidden = !docstring;
    requiredElement("#inspector-direct-score").textContent = formatInspectorScore(
        node.direct_relevance,
    );
    requiredElement("#inspector-relationship-score").textContent = formatInspectorScore(
        node.relationship_strength,
    );
    const candidates = neighborhoodFor(nodeId);
    renderRelationshipList(candidates);
    const inspector = requiredElement("#node-inspector");
    inspector.hidden = false;
    if (options.scroll) {
        window.requestAnimationFrame(() => {
            inspector.scrollIntoView({ behavior: "smooth", block: "start" });
        });
    }
    await renderNeighborhood(node, candidates, token);
}

function formatMetric(value) {
    const parsed = finiteNumber(value);
    return parsed === null ? "Not available" : parsed.toFixed(3);
}

function formatDelta(value) {
    const parsed = finiteNumber(value);
    if (parsed === null) return "Not available";
    if (Math.abs(parsed) < 0.0005) return "0.000";
    return `${parsed > 0 ? "+" : ""}${parsed.toFixed(3)}`;
}

function appendCell(row, value, className = "") {
    const cell = createElement("td", className, value);
    row.append(cell);
    return cell;
}

function renderAggregate() {
    const holder = clear(requiredElement("#aggregate-table-body"));
    const aggregate = isObject(state.evaluation?.aggregate) ? state.evaluation.aggregate : {};
    const lexical = isObject(aggregate.lexical) ? aggregate.lexical : {};
    const graph = isObject(aggregate.graph_expanded) ? aggregate.graph_expanded : {};
    const deltas = isObject(aggregate.delta) ? aggregate.delta : {};
    let rowCount = 0;

    for (const [key, label] of AGGREGATE_METRICS) {
        const lexicalValue = finiteNumber(lexical[key]);
        const graphValue = finiteNumber(graph[key]);
        if (lexicalValue === null || graphValue === null) continue;
        const delta = finiteNumber(deltas[key]) ?? graphValue - lexicalValue;
        const row = createElement("tr");
        const heading = createElement("th", null, label);
        heading.scope = "row";
        row.append(heading);
        appendCell(row, formatMetric(lexicalValue));
        appendCell(row, formatMetric(graphValue));
        const change = appendCell(row, formatDelta(delta));
        if (delta > 0) change.classList.add("positive");
        if (delta < 0) change.classList.add("negative");
        holder.append(row);
        rowCount += 1;
    }

    if (!rowCount) {
        const row = createElement("tr");
        const cell = appendCell(row, "Aggregate metrics are unavailable.");
        cell.colSpan = 4;
        holder.append(row);
    }
}

function formatRank(value) {
    const rank = finiteNumber(value);
    return rank === null ? "Not retrieved" : String(rank);
}

function formatPair(lexical, graph, formatter = formatMetric) {
    return `${formatter(lexical)} / ${formatter(graph)}`;
}

function reviewedChangesCell(row, comparison) {
    const added = safeArray(comparison?.newly_retrieved_judgments_at_10);
    const lost = safeArray(comparison?.newly_missed_judgments_at_10);
    const cell = appendCell(row, `${added.length} added / ${lost.length} lost`);
    const details = [];
    if (added.length) details.push(`Added: ${added.join(", ")}`);
    if (lost.length) details.push(`Lost: ${lost.join(", ")}`);
    if (details.length) {
        cell.title = details.join("\n");
        cell.setAttribute("aria-label", `${cell.textContent}. ${details.join(". ")}`);
    }
}

function renderPerQueryComparison() {
    const holder = clear(requiredElement("#comparison-table-body"));
    for (const query of evaluationQueries()) {
        const lexical = isObject(query.lexical) ? query.lexical : {};
        const graph = isObject(query.graph_expanded) ? query.graph_expanded : {};
        const comparison = isObject(query.comparison) ? query.comparison : {};
        const row = createElement("tr");
        const queryCell = createElement("th");
        queryCell.scope = "row";
        const button = createElement("button", null, query.query || query.id);
        button.type = "button";
        button.dataset.comparisonQuery = query.id;
        queryCell.append(button);
        row.append(queryCell);

        const rankCell = appendCell(row, formatPair(
            lexical.answer_rank,
            graph.answer_rank,
            formatRank,
        ));
        const rankChange = finiteNumber(comparison.answer_rank_change);
        if (rankChange !== null) {
            rankCell.title = `Answer rank change: ${rankChange > 0 ? "+" : ""}${rankChange}`;
        }
        appendCell(row, formatPair(
            lexical.reciprocal_answer_rank_at_10,
            graph.reciprocal_answer_rank_at_10,
        ));
        appendCell(row, formatPair(lexical.recall_at_10, graph.recall_at_10));
        appendCell(row, formatPair(lexical.recall_at_20, graph.recall_at_20));
        appendCell(row, formatPair(
            lexical.supporting_recall_at_10,
            graph.supporting_recall_at_10,
        ));
        reviewedChangesCell(row, comparison);
        appendCell(
            row,
            typeof comparison.regression === "boolean"
                ? comparison.regression ? "Yes" : "No"
                : "Not available",
            comparison.regression === true ? "regression" : "",
        );
        holder.append(row);
    }

    if (!holder.childElementCount) {
        const row = createElement("tr");
        const cell = appendCell(row, "Per-query metrics are unavailable.");
        cell.colSpan = 8;
        holder.append(row);
    }
}

function rankingLocation(result) {
    if (!result?.path) return "";
    const start = finiteNumber(result.start_line);
    const end = finiteNumber(result.end_line);
    if (start === null) return String(result.path);
    if (end !== null && end !== start) return `${result.path}:${start}-${end}`;
    return `${result.path}:${start}`;
}

function appendRankingResult(row, result) {
    if (!result) {
        appendCell(row, "Not ranked", "ranking-empty");
        return;
    }
    const cell = createElement("td", "ranking-result");
    cell.append(createElement("strong", "ranking-name", result.qualified_name || result.node_id));
    if (result.kind) cell.append(createElement("span", "ranking-meta", result.kind));
    const location = rankingLocation(result);
    if (location) cell.append(createElement("code", "ranking-location", location));
    if (result.judgment_role) {
        const relevance = finiteNumber(result.relevance);
        const suffix = relevance === null ? "" : `, relevance ${relevance}`;
        cell.append(createElement(
            "span",
            "ranking-judgment",
            `Reviewed ${result.judgment_role}${suffix}`,
        ));
    }
    row.append(cell);
}

function renderRankingComparison() {
    const holder = clear(requiredElement("#ranking-comparison"));
    const query = selectedEvaluationQuery();
    const lexical = safeArray(query?.lexical?.ranking);
    const graph = safeArray(query?.graph_expanded?.ranking);
    if (!query || !lexical.length || !graph.length) {
        holder.hidden = true;
        return;
    }

    const wrapper = createElement("div", "ranking-table-wrap");
    wrapper.tabIndex = 0;
    wrapper.setAttribute("role", "region");
    wrapper.setAttribute(
        "aria-label",
        `Full lexical and graph-expanded rankings for ${query.query || query.id}`,
    );
    const table = createElement("table", "ranking-table");
    table.append(createElement("caption", "ranking-caption", `Full rankings: ${query.query || query.id}`));
    const head = createElement("thead");
    const headRow = createElement("tr");
    for (const label of ["Rank", "Lexical", "Graph-expanded"]) {
        const heading = createElement("th", null, label);
        heading.scope = "col";
        headRow.append(heading);
    }
    head.append(headRow);
    table.append(head);

    const body = createElement("tbody");
    const length = Math.max(lexical.length, graph.length);
    for (let index = 0; index < length; index += 1) {
        const lexicalResult = lexical[index] || null;
        const graphResult = graph[index] || null;
        const row = createElement("tr");
        const lexicalRank = finiteNumber(lexicalResult?.rank);
        const graphRank = finiteNumber(graphResult?.rank);
        const displayedRank = lexicalRank === graphRank
            ? lexicalRank ?? index + 1
            : `${lexicalRank ?? "Not ranked"} / ${graphRank ?? "Not ranked"}`;
        const heading = createElement("th", null, displayedRank);
        heading.scope = "row";
        row.append(heading);
        appendRankingResult(row, lexicalResult);
        appendRankingResult(row, graphResult);
        body.append(row);
    }
    table.append(body);
    wrapper.append(table);
    holder.append(wrapper);
    holder.hidden = false;
}

function renderComparison() {
    renderAggregate();
    renderPerQueryComparison();
    renderRankingComparison();
}

async function activateView() {
    setViewControls();
    syncControls();
    if (state.view === "graph") await renderActiveGraph();
    if (state.view === "comparison") renderComparison();
    schedulePlotResize();
}

function setGraph(graphId) {
    if (
        graphId !== REPOSITORY_GRAPH_ID
        && !state.queries.some((entry) => entry.id === graphId)
    ) {
        return;
    }
    state.graphId = graphId;
    clearInspector();
    syncControls();
    writeHash();
}

function setComparisonQuery(queryId) {
    if (!evaluationQueries().some((query) => query.id === queryId)) return;
    state.comparisonQueryId = queryId;
    syncControls();
    writeHash();
}

function closestButton(event, selector) {
    return event.target instanceof Element ? event.target.closest(selector) : null;
}

function bindEvents() {
    document.querySelectorAll("button[data-view]").forEach((button) => {
        button.addEventListener("click", () => {
            const nextView = button.dataset.view;
            if (!VALID_VIEWS.has(nextView)) return;
            const viewChanged = state.view !== nextView;
            state.view = nextView;
            if (viewChanged) requiredElement("#app-main").scrollTop = 0;
            writeHash();
            clearError();
            activateView().catch(showError);
        });
    });

    requiredElement("#graph-select").addEventListener("change", (event) => {
        setGraph(event.currentTarget.value);
        clearError();
        renderActiveGraph().catch(showError);
    });
    requiredElement("#comparison-query-select").addEventListener("change", (event) => {
        setComparisonQuery(event.currentTarget.value);
        renderRankingComparison();
    });
    requiredElement("#comparison-table-body").addEventListener("click", (event) => {
        const button = closestButton(event, "button[data-comparison-query]");
        if (!button) return;
        setComparisonQuery(button.dataset.comparisonQuery);
        renderRankingComparison();
        requiredElement("#ranking-comparison").scrollIntoView({ block: "nearest" });
    });
    requiredElement("#relationship-list").addEventListener("click", (event) => {
        const button = closestButton(event, "button[data-node-id]");
        if (!button || !requiredElement("#relationship-list").contains(button)) return;
        selectInspectionNode(button.dataset.nodeId, { scroll: false }).catch(showError);
    });
    window.addEventListener("hashchange", () => {
        const previousView = state.view;
        const previousGraph = state.graphId;
        readHash();
        if (state.view !== previousView) requiredElement("#app-main").scrollTop = 0;
        if (state.graphId !== previousGraph) clearInspector();
        writeHash();
        clearError();
        activateView().catch(showError);
    });
    window.addEventListener("resize", schedulePlotResize);
}

function validateEvaluationQueries() {
    const expectedIds = new Set(state.queries.map((query) => query.id));
    const actual = evaluationQueries();
    if (actual.length !== state.queries.length) {
        throw new Error("The evaluation artifact does not cover every recorded query.");
    }
    for (const query of actual) {
        if (!isObject(query) || typeof query.id !== "string" || !expectedIds.delete(query.id)) {
            throw new Error("The evaluation artifact contains an unknown or duplicate query.");
        }
    }
    if (expectedIds.size) throw new Error("The evaluation artifact is missing recorded queries.");
}

async function initialize() {
    try {
        state.manifest = await loadJson("/data/manifest.json");
        validateSchema("The data manifest", state.manifest);
        validateSnapshot("The data manifest", state.manifest);
        state.queries = normalizeQueries(state.manifest);
        if (state.queries.length !== 14) {
            throw new Error(`The data manifest declares ${state.queries.length} queries; 14 are required.`);
        }

        const repositoryUrl = dataPath(repositoryAsset(state.manifest), "repository.json");
        const evaluationUrl = dataPath(state.manifest.evaluation, "evaluation.json");
        [state.repository, state.evaluation] = await Promise.all([
            loadJson(repositoryUrl),
            loadJson(evaluationUrl),
        ]);
        validateSchema("The repository artifact", state.repository);
        validateSchema("The evaluation artifact", state.evaluation);
        validateSnapshot("The repository artifact", state.repository);
        validateSnapshot("The evaluation artifact", state.evaluation);
        validateFigure("The repository artifact", state.repository.figure);
        state.repositoryInspection = validateInspection("The repository artifact", state.repository);
        validateEvaluationQueries();

        populateGraphSelect();
        populateComparisonSelect();
        readHash();
        writeHash();
        syncControls();
        clearInspector();
        renderComparison();
        bindEvents();
        await activateView();
        requiredElement("#loading-state").hidden = true;
    } catch (error) {
        showError(error);
    }
}

initialize();
