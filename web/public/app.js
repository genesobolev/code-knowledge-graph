"use strict";

const VALID_VIEWS = new Set(["repository", "query", "comparison"]);
const AGGREGATE_METRICS = [
    ["answer_mrr_at_10", "Answer MRR at 10"],
    ["recall_at_10", "Recall at 10"],
    ["recall_at_20", "Recall at 20"],
    ["supporting_recall_at_10", "Supporting recall at 10"],
];

const state = {
    manifest: null,
    repository: null,
    evaluation: null,
    queries: [],
    queryCache: new Map(),
    snapshot: {},
    view: "repository",
    queryId: "",
    repositoryRendered: false,
    repositoryRenderPromise: null,
    queryRendered: false,
    queryPlotQueue: Promise.resolve(),
    queryRenderToken: 0,
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

function assetValue(value) {
    if (typeof value === "string") return value;
    if (!isObject(value)) return "";
    return value.file || value.path || value.url || value.asset || value.asset_path || value.data_file || "";
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
    const source = manifest.queries || manifest.recorded_queries;
    const entries = Array.isArray(source)
        ? source
        : isObject(source)
            ? Object.entries(source).map(([id, value]) => (
                typeof value === "string" ? { id, file: value } : { id, ...value }
            ))
            : [];
    return entries.map((entry, index) => {
        const value = typeof entry === "string" ? { file: entry } : isObject(entry) ? entry : {};
        const file = assetValue(value) || assetValue(value.query_asset);
        const id = String(value.id || value.query_id || value.slug || `query-${index + 1}`);
        return {
            ...value,
            id,
            label: String(value.label || value.name || value.query || value.text || id),
            file,
        };
    });
}

function repositoryAsset(manifest) {
    const candidates = Array.isArray(manifest.repositories) ? manifest.repositories : [];
    return manifest.repository || manifest.repository_asset || candidates[0] || "repository.json";
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

function selectedQueryEntry() {
    return state.queries.find((entry) => entry.id === state.queryId) || state.queries[0] || null;
}

function evaluationQueries() {
    return safeArray(state.evaluation?.queries);
}

function selectedEvaluationQuery() {
    return evaluationQueries().find((query) => query.id === state.queryId) || null;
}

function readHash() {
    const parameters = new URLSearchParams(location.hash.slice(1));
    const requestedView = parameters.get("view");
    const requestedQuery = parameters.get("query");
    state.view = VALID_VIEWS.has(requestedView) ? requestedView : "repository";
    state.queryId = state.queries.some((entry) => entry.id === requestedQuery)
        ? requestedQuery
        : state.queries[0]?.id || "";
}

function writeHash() {
    const parameters = new URLSearchParams({ view: state.view });
    if (state.queryId) parameters.set("query", state.queryId);
    const nextHash = `#${parameters.toString()}`;
    if (location.hash !== nextHash) history.replaceState(null, "", nextHash);
}

function populateQuerySelect(select) {
    const fragment = document.createDocumentFragment();
    for (const query of state.queries) {
        const option = createElement("option", null, query.label);
        option.value = query.id;
        fragment.append(option);
    }
    clear(select).append(fragment);
    select.disabled = state.queries.length === 0;
}

function syncQueryControls() {
    for (const selector of ["#query-select", "#comparison-query-select"]) {
        const select = requiredElement(selector);
        if (select.value !== state.queryId) select.value = state.queryId;
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

function plotly() {
    const library = window.Plotly;
    if (!library || typeof library.newPlot !== "function" || typeof library.react !== "function") {
        throw new Error("The local Plotly library could not be loaded.");
    }
    return library;
}

function validateFigure(label, figure) {
    if (!isObject(figure) || !Array.isArray(figure.data) || !isObject(figure.layout) || !isObject(figure.config)) {
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

async function renderFigure(host, figure, label, useReact) {
    validateFigure(label, figure);
    const library = plotly();
    if (useReact) {
        await library.react(host, figure.data, figure.layout, figure.config);
    } else {
        await library.newPlot(host, figure.data, figure.layout, figure.config);
    }
}

function schedulePlotResize() {
    window.cancelAnimationFrame(state.resizeFrame);
    state.resizeFrame = window.requestAnimationFrame(() => {
        state.resizeFrame = window.requestAnimationFrame(() => {
            const library = window.Plotly;
            if (!library?.Plots || typeof library.Plots.resize !== "function") return;
            if (state.view === "repository" && state.repositoryRendered) {
                library.Plots.resize(requiredElement("#repository-figure"));
            }
            if (state.view === "query" && state.queryRendered) {
                library.Plots.resize(requiredElement("#query-figure"));
            }
        });
    });
}

async function renderRepositoryFigure() {
    if (state.repositoryRendered) {
        schedulePlotResize();
        return;
    }
    if (!state.repositoryRenderPromise) {
        state.repositoryRenderPromise = renderFigure(
            requiredElement("#repository-figure"),
            state.repository?.figure,
            "The repository artifact",
            false,
        ).then(() => {
            state.repositoryRendered = true;
        }).finally(() => {
            state.repositoryRenderPromise = null;
        });
    }
    await state.repositoryRenderPromise;
    schedulePlotResize();
}

function loadQuery(entry) {
    if (state.queryCache.has(entry.id)) return state.queryCache.get(entry.id);
    if (!entry.file) {
        return Promise.reject(new Error(`Recorded query '${entry.label}' does not declare a data file.`));
    }
    const promise = loadJson(dataPath(entry.file)).then((payload) => {
        if (payload.id && payload.id !== entry.id) {
            throw new Error(`Recorded query '${entry.label}' loaded the wrong artifact.`);
        }
        validateSnapshot(`Recorded query '${entry.label}'`, payload);
        validateFigure(`Recorded query '${entry.label}'`, payload.figure);
        return payload;
    }).catch((error) => {
        state.queryCache.delete(entry.id);
        throw error;
    });
    state.queryCache.set(entry.id, promise);
    return promise;
}

async function renderQueryFigure() {
    const entry = selectedQueryEntry();
    const token = ++state.queryRenderToken;
    const host = requiredElement("#query-figure");
    if (!entry) {
        requiredElement("#query-question").textContent = "";
        requiredElement("#query-description").textContent = "";
        clear(host);
        return;
    }

    host.setAttribute("aria-busy", "true");
    try {
        const payload = await loadQuery(entry);
        if (token !== state.queryRenderToken || state.view !== "query" || state.queryId !== entry.id) return;

        requiredElement("#query-question").textContent = payload.query || entry.query || entry.label;
        requiredElement("#query-description").textContent = payload.description || entry.description || "";
        renderProvenance(requiredElement("#query-provenance"), payload);
        state.queryPlotQueue = state.queryPlotQueue.catch(() => {}).then(async () => {
            if (token !== state.queryRenderToken || state.view !== "query" || state.queryId !== entry.id) {
                return false;
            }
            await renderFigure(
                host,
                payload.figure,
                `Recorded query '${entry.label}'`,
                state.queryRendered,
            );
            state.queryRendered = true;
            return true;
        });
        const rendered = await state.queryPlotQueue;
        if (!rendered || token !== state.queryRenderToken || state.view !== "query") return;
        schedulePlotResize();
    } finally {
        if (token === state.queryRenderToken) host.removeAttribute("aria-busy");
    }
}

function shortHash(value) {
    const raw = value === undefined || value === null ? "" : String(value);
    return raw.length > 12 ? raw.slice(0, 12) : raw;
}

function renderProvenance(holder, payload, extras = []) {
    const values = snapshotFields(payload);
    const entries = [
        ["Repository", values.repository],
        ["Commit", shortHash(values.commit)],
        ["Tree", shortHash(values.tree)],
        ...extras,
    ];
    clear(holder);
    for (const [label, value] of entries) {
        if (value === undefined || value === null || value === "") continue;
        const item = createElement("span");
        item.append(createElement("strong", null, `${label}: `), document.createTextNode(String(value)));
        holder.append(item);
    }
}

function finiteNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") {
        return null;
    }
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
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
    requiredElement("#comparison-conclusion").textContent = aggregate.conclusion || "";
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
        const button = createElement("button", null, query.query || query.text || query.id);
        button.type = "button";
        button.dataset.comparisonQuery = query.id;
        queryCell.append(button);
        row.append(queryCell);

        const rankCell = appendCell(row, formatPair(lexical.answer_rank, graph.answer_rank, formatRank));
        if (finiteNumber(comparison.answer_rank_change) !== null) {
            rankCell.title = `Answer rank change: ${comparison.answer_rank_change > 0 ? "+" : ""}${comparison.answer_rank_change}`;
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
        const regression = comparison.regression;
        appendCell(
            row,
            typeof regression === "boolean" ? (regression ? "Yes" : "No") : "Not available",
            regression === true ? "regression" : "",
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
    wrapper.setAttribute("aria-label", `Full lexical and graph-expanded rankings for ${query.query || query.id}`);
    const table = createElement("table", "ranking-table");
    table.append(createElement(
        "caption",
        "ranking-caption",
        `Full rankings: ${query.query || query.id}`,
    ));
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
    renderProvenance(
        requiredElement("#comparison-provenance"),
        state.evaluation,
        [["Ranking budget", state.evaluation?.ranking_budget]],
    );
}

async function activateView() {
    setViewControls();
    syncQueryControls();
    if (state.view === "repository") await renderRepositoryFigure();
    if (state.view === "query") await renderQueryFigure();
    if (state.view === "comparison") renderComparison();
    schedulePlotResize();
}

function setQuery(queryId) {
    if (!state.queries.some((entry) => entry.id === queryId)) return;
    state.queryId = queryId;
    syncQueryControls();
    writeHash();
}

function bindEvents() {
    document.querySelectorAll("button[data-view]").forEach((button) => {
        button.addEventListener("click", () => {
            const nextView = button.dataset.view;
            if (!VALID_VIEWS.has(nextView)) return;
            state.view = nextView;
            writeHash();
            clearError();
            activateView().catch(showError);
        });
    });

    requiredElement("#query-select").addEventListener("change", (event) => {
        setQuery(event.currentTarget.value);
        clearError();
        renderQueryFigure().catch(showError);
    });
    requiredElement("#comparison-query-select").addEventListener("change", (event) => {
        setQuery(event.currentTarget.value);
        renderRankingComparison();
    });
    requiredElement("#comparison-table-body").addEventListener("click", (event) => {
        const button = event.target.closest("button[data-comparison-query]");
        if (!button) return;
        setQuery(button.dataset.comparisonQuery);
        renderRankingComparison();
        requiredElement("#ranking-comparison").scrollIntoView({ block: "nearest" });
    });
    window.addEventListener("hashchange", () => {
        readHash();
        writeHash();
        clearError();
        activateView().catch(showError);
    });
    window.addEventListener("resize", schedulePlotResize);
}

async function initialize() {
    try {
        state.manifest = await loadJson("/data/manifest.json");
        if (!isObject(state.manifest)) throw new Error("The data manifest is not an object.");
        state.queries = normalizeQueries(state.manifest);
        if (!state.queries.length) throw new Error("The data manifest does not declare any queries.");
        validateSnapshot("The data manifest", state.manifest);

        populateQuerySelect(requiredElement("#query-select"));
        populateQuerySelect(requiredElement("#comparison-query-select"));
        readHash();
        writeHash();
        syncQueryControls();

        const repositoryUrl = dataPath(repositoryAsset(state.manifest), "repository.json");
        const evaluationUrl = dataPath(
            state.manifest.evaluation || state.manifest.evaluation_asset || state.manifest.evaluation_file,
            "evaluation.json",
        );
        [state.repository, state.evaluation] = await Promise.all([
            loadJson(repositoryUrl),
            loadJson(evaluationUrl),
        ]);
        validateSnapshot("The repository artifact", state.repository);
        validateSnapshot("The evaluation artifact", state.evaluation);
        validateFigure("The repository artifact", state.repository?.figure);

        renderProvenance(requiredElement("#repository-provenance"), state.repository);
        renderComparison();
        bindEvents();
        await activateView();
        requiredElement("#loading-state").hidden = true;
    } catch (error) {
        showError(error);
    }
}

initialize();
