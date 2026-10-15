// Nodes page: the node list (polled while the page is visible), the Add node dialog and the node dialog (Overview,
// Media, Setup Health, Log, Settings). Everything goes through /api/nodes*; csrf_fetch.js adds the CSRF header.

(function () {
    'use strict';

    const POLL_MS = 10000;
    const LOG_LINES = 200;
    const DOCS_URL = 'https://github.com/stevezau/media_preview_generator/blob/main/docs/guides.md#remote-nodes';
    const ROLE_WORKER = 'worker';
    const ROLE_PLEX_DB = 'plex_db';

    const $ = (sel, el) => (el || document).querySelector(sel);
    const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));
    const esc = (value) => escapeHtml(value == null ? '' : String(value));

    async function api(method, url, body) {
        const opts = { method };
        if (body !== undefined) {
            opts.headers = { 'Content-Type': 'application/json' };
            opts.body = JSON.stringify(body);
        }
        let r;
        try {
            r = await fetch(url, opts);
        } catch (_) {
            return { ok: false, status: 0, data: { error: 'Network error' } };
        }
        let data = null;
        try { data = await r.json(); } catch (_) { /* non-JSON */ }
        return { ok: r.ok, status: r.status, data };
    }

    function errorText(r, fallback) {
        const error = r.data && r.data.error;
        if (typeof error === 'string' && error) return error;
        if (error && typeof error === 'object' && error.message) return error.message;
        return r.status ? `${fallback} (HTTP ${r.status})` : `${fallback} (no answer from this app)`;
    }

    function initTooltips(scope) {
        if (typeof window._initBootstrapTooltips === 'function') window._initBootstrapTooltips(scope);
    }

    // A tooltip open on an element that is about to be replaced would otherwise stay on screen.
    function disposeTooltips(scope) {
        if (scope && typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(scope);
    }

    function infoIcon(title) {
        return `<button type="button" class="info-icon" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="${esc(title)}"><i class="bi bi-info-circle"></i></button>`;
    }

    function pill(tone, text, extra) {
        const cls = tone && tone !== 'off' ? ` ${tone}` : '';
        const attrs = extra && extra.title ? ` title="${esc(extra.title)}"` : '';
        return `<span class="pill${cls}${extra && extra.mono ? ' mono' : ''}"${attrs}>${esc(text)}</span>`;
    }

    // ---------- facts about a node ---------------------------------------------------------------------------

    // [tone, chip text]. Green ready, amber needs attention, red down, grey not checked / switched off.
    const STATES = {
        ready: ['ok', '✓ Ready'],
        update_needed: ['warn', 'Update needed'],
        access_problem: ['warn', 'Access problem'],
        gpu_lost: ['warn', 'GPU lost'],
        no_ffmpeg: ['bad', 'FFmpeg missing'],
        offline: ['bad', 'Offline'],
        key_refused: ['bad', 'Key refused'],
        connecting: ['off', 'Connecting…'],
        disabled: ['off', 'Turned off'],
    };
    // The row's button says Fix for these, and opens the tab where the fix is.
    const FIX_TAB = { access_problem: 'media', key_refused: 'settings', update_needed: 'health', gpu_lost: 'health', no_ffmpeg: 'health' };

    function stateOf(node) {
        return STATES[node.state] || ['off', node.state || 'Unknown'];
    }

    function roles(node) {
        return Array.isArray(node.roles) ? node.roles : [];
    }

    function isWorker(node) {
        return roles(node).includes(ROLE_WORKER);
    }

    function isPlexDb(node) {
        return roles(node).includes(ROLE_PLEX_DB) || !!node.legacy;
    }

    function isWindows(node) {
        return String((node.platform || {}).os || '').toLowerCase().startsWith('win');
    }

    function gpus(node) {
        return Array.isArray(node.gpus) ? node.gpus : [];
    }

    function formatBytes(bytes) {
        const n = Number(bytes);
        if (!Number.isFinite(n) || n < 0) return '';
        const gb = n / 1e9;
        if (gb >= 10) return `${Math.round(gb)} GB`;
        if (gb >= 1) return `${gb.toFixed(1)} GB`;
        return `${Math.round(n / 1e6)} MB`;
    }

    function ago(seconds) {
        const t = Number(seconds);
        if (!t) return '';
        const s = Math.max(0, Math.round(Date.now() / 1000 - t));
        if (s < 60) return `${s} s ago`;
        if (s < 3600) return `${Math.round(s / 60)} min ago`;
        if (s < 86400) return `${Math.round(s / 3600)} h ago`;
        const days = Math.round(s / 86400);
        return `${days} ${days === 1 ? 'day' : 'days'} ago`;
    }

    // busy is how many files the node is running right now.
    function busyText(busy) {
        const running = Number(busy);
        if (!Number.isFinite(running)) return '';
        return running > 0 ? `${running} ${running === 1 ? 'file' : 'files'} running` : 'idle';
    }

    function nameOf(item) {
        return (item && (item.name || item.id)) || '';
    }

    // used_by is {groups: [{id, name}], servers: [{id, name}]} on a node row and a [{kind, id, name}] list on a
    // refused DELETE.
    function usedByParts(usedBy) {
        const parts = [];
        if (Array.isArray(usedBy)) {
            usedBy.forEach((item) => {
                const label = item.kind === 'server' ? 'Plex server' : 'worker group';
                if (nameOf(item)) parts.push(`${label} "${nameOf(item)}"`);
            });
            return parts;
        }
        if (usedBy && typeof usedBy === 'object') {
            (usedBy.servers || []).forEach((s) => { if (nameOf(s)) parts.push(`Plex server "${nameOf(s)}"`); });
            (usedBy.groups || []).forEach((g) => { if (nameOf(g)) parts.push(`worker group "${nameOf(g)}"`); });
        }
        return parts;
    }

    function roleChips(node) {
        let html = '';
        if (isWorker(node)) html += pill('role', 'Worker');
        if (isPlexDb(node)) html += pill('role', 'Plex database');
        if (isWindows(node)) html += pill('off', 'Windows');
        if (node.legacy) {
            html += pill('off', 'Older Plex helper')
                + infoIcon("Keeps working as it is. Update it to a Plex database node for Setup Health and Windows support.");
        }
        return html;
    }

    function hardwareChips(node) {
        let html = '';
        gpus(node).forEach((gpu) => {
            const failed = gpu.status && gpu.status !== 'ok';
            const name = gpu.name || gpu.device || gpu.type || 'GPU';
            html += pill(failed ? 'warn' : 'off', failed ? `${name} · not working` : name, failed && gpu.error ? { title: gpu.error } : null);
        });
        const cores = node.cpu && Number(node.cpu.cores);
        if (cores) html += pill('off', `${cores} CPU`);
        return html;
    }

    function versionLine(node) {
        const bits = [];
        if (node.version) bits.push(`v${node.version}`);
        const seen = ago(node.last_seen);
        if (seen) bits.push(`seen ${seen}`);
        return bits.join(' · ');
    }

    // ---------- list ---------------------------------------------------------------------------------------------

    const page = { nodes: [], appVersion: '', loaded: false, timer: null, loading: false, openId: null };

    function nodeIcon(node) {
        if (isWindows(node)) return 'bi-windows';
        if (!isWorker(node) && isPlexDb(node)) return 'bi-database';
        return 'bi-pc-display-horizontal';
    }

    function nodeRow(node) {
        const [tone, text] = stateOf(node);
        const fixTab = FIX_TAB[node.state];
        const used = usedByParts(node.used_by);
        const busy = isWorker(node) ? busyText(node.busy) : '';
        const reason = node.reason ? `<span class="nd-faint">${esc(node.reason)}</span>` : '';
        const version = versionLine(node);
        const name = esc(node.name || node.id);
        return `
            <article class="nd-row${node.enabled === false ? ' off' : ''}" id="node-${esc(node.id)}" data-id="${esc(node.id)}" data-state="${esc(node.state || '')}" data-tone="${esc(tone)}">
                <div class="nd-ico" aria-hidden="true"><i class="bi ${nodeIcon(node)}"></i></div>
                <div class="nd-id">
                    <h2 class="nd-name" title="${name}">${name}</h2>
                    <div class="nd-url" title="${esc(node.url)}">${esc(node.url)}</div>
                </div>
                <div class="nd-chips">${roleChips(node)}${hardwareChips(node)}${busy ? `<span class="nd-faint nd-busy">${esc(busy)}</span>` : ''}${used.length ? `<span class="nd-faint">used by ${esc(used.join(', '))}</span>` : ''}</div>
                <div class="nd-status">${pill(tone, text)}${reason}${version ? `<span class="nd-faint nd-version">${esc(version)}</span>` : ''}</div>
                <div class="nd-acts">
                    <button type="button" class="btn btn-sm ${fixTab ? 'btn-warning' : 'btn-outline-secondary'} nd-open" data-id="${esc(node.id)}" data-tab="${fixTab || 'overview'}" aria-label="${fixTab ? 'Fix' : 'Open'} ${name}">${fixTab ? 'Fix' : 'Open'}</button>
                </div>
            </article>`;
    }

    function emptyState() {
        return `
            <div class="nd-empty">
                <span class="nd-ico" aria-hidden="true"><i class="bi bi-pc-display-horizontal"></i></span>
                <strong>No nodes yet</strong>
                <p>A node is another machine running this app in node mode. It can lend its GPU or CPU to your workers, or open Plex's database on the machine where Plex runs, Windows included.</p>
                <a href="${DOCS_URL}" target="_blank" rel="noopener">How to run a node <i class="bi bi-box-arrow-up-right"></i></a>
                <button type="button" class="btn btn-primary nd-add"><i class="bi bi-plus-lg me-1"></i>Add node</button>
            </div>`;
    }

    function renderList() {
        const list = $('#nodeList');
        if (!list) return;
        disposeTooltips(list);
        list.innerHTML = page.nodes.length ? page.nodes.map(nodeRow).join('') : emptyState();
        initTooltips(list);
        const checkNow = $('#nodesCheckNow');
        if (checkNow) checkNow.disabled = !page.nodes.length;
    }

    async function loadNodes() {
        if (page.loading) return;
        page.loading = true;
        const r = await api('GET', '/api/nodes');
        page.loading = false;
        if (!r.ok) {
            // A failed poll keeps the list already on screen; only the first load shows the error.
            if (!page.loaded) {
                $('#nodeList').innerHTML = `<div class="nd-state"><div class="alert alert-danger mb-0">${esc(errorText(r, "Couldn't load nodes"))}</div></div>`;
            }
            return;
        }
        page.nodes = Array.isArray(r.data && r.data.nodes) ? r.data.nodes : [];
        page.appVersion = (r.data && r.data.app_version) || '';
        const first = !page.loaded;
        page.loaded = true;
        renderList();
        if (page.openId) refreshOpenNode();
        if (first) openFromHash();
    }

    function nodeById(id) {
        return page.nodes.find((n) => String(n.id) === String(id)) || null;
    }

    function replaceNode(row) {
        if (!row || !row.id) return;
        const at = page.nodes.findIndex((n) => String(n.id) === String(row.id));
        if (at >= 0) page.nodes[at] = row;
        else page.nodes.push(row);
        renderList();
    }

    async function checkAll() {
        const btn = $('#nodesCheckNow');
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Checking…';
        await Promise.all(page.nodes.map((n) => api('POST', `/api/nodes/${encodeURIComponent(n.id)}/check`)));
        btn.innerHTML = '<i class="bi bi-arrow-repeat me-1"></i>Check now';
        await loadNodes();
    }

    function startPolling() {
        if (page.timer) return;
        page.timer = setInterval(() => { if (!document.hidden) loadNodes(); }, POLL_MS);
    }

    function openFromHash() {
        const hash = decodeURIComponent((window.location.hash || '').slice(1));
        if (hash === 'add') {
            openAdd();
            return;
        }
        if (!hash.startsWith('node-')) return;
        const id = hash.slice('node-'.length);
        if (!nodeById(id)) return;
        const row = document.getElementById(`node-${id}`);
        if (row) row.scrollIntoView({ block: 'center' });
        openNode(id);
    }

    // ---------- path mappings + Test access ------------------------------------------------------------------------

    // Why the node didn't see a file, from the access check's reason.
    const NOT_VISIBLE = {
        unmapped: 'no mapping covers this folder',
        outside_roots: 'outside the folders the node is allowed to read',
        missing: 'not found on the node',
        different_file: 'a different file is at that path (size or modified time differ)',
        unreadable: "the node can't read it",
    };

    function isUnder(path, prefix) {
        const p = String(path || '');
        const raw = String(prefix || '');
        // Trimming the trailing slash would leave nothing of a "/" row, which covers every path.
        if (/^\/+$/.test(raw)) return p.startsWith('/');
        const pre = raw.replace(/\/+$/, '');
        return !!pre && (p === pre || p.startsWith(`${pre}/`));
    }

    // One editor for Add node step 2 and the node dialog's Media tab.
    function createMappingEditor(root, { getNodeId, onChange }) {
        root.innerHTML = `
            <div class="nd-map">
                <div class="nd-map-head">
                    <h3>Where <span class="nd-map-name"></span> sees your media</h3>
                    <button type="button" class="btn btn-sm btn-outline-secondary nd-test"><i class="bi bi-play-fill me-1"></i>Test access</button>
                    <button type="button" class="info-icon" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="Checks real files from each library on the node." data-explain-title="Test access" data-explain-template="infoNodeTestAccessTpl" aria-label="About Test access"><i class="bi bi-info-circle"></i></button>
                </div>
                <div class="nd-map-cols">
                    <span>Path on this app ${infoIcon('Where this app sees a library folder, inside its own container.')}</span><span></span>
                    <span>Path on the node ${infoIcon('Where the node sees that same folder on its machine.')}</span><span></span>
                </div>
                <div class="nd-map-rows"></div>
                <div><button type="button" class="btn btn-sm btn-outline-secondary nd-add-row"><i class="bi bi-plus-lg me-1"></i>Add row</button></div>
                <div class="sm-warnline bad d-none nd-map-error" role="alert"></div>
                <div class="nd-map-results" aria-live="polite"></div>
                <div class="sm-hint">Test access takes 3 real files from each library and checks the node sees each one with the same size and modified time.</div>
            </div>`;
        const rows = $('.nd-map-rows', root);
        const results = $('.nd-map-results', root);
        const errorLine = $('.nd-map-error', root);
        let lastLibraries = [];

        function changed() {
            if (typeof onChange === 'function') onChange();
        }

        function addRow(mapping) {
            const m = mapping || {};
            const row = document.createElement('div');
            row.className = 'nd-map-row';
            row.innerHTML = `
                <input type="text" class="form-control font-monospace nd-app" aria-label="Path on this app" placeholder="/data/Movies" value="${esc(m.app_prefix)}">
                <span class="nd-arrow" aria-hidden="true">→</span>
                <input type="text" class="form-control font-monospace nd-node" aria-label="Path on the node" placeholder="/mnt/nas/Movies" value="${esc(m.node_prefix)}">
                <div class="nd-chip-slot"></div>
                <button type="button" class="btn btn-sm btn-outline-danger nd-remove" aria-label="Remove mapping" title="Remove mapping"><i class="bi bi-x-lg"></i></button>`;
            $('.nd-remove', row).addEventListener('click', () => {
                row.remove();
                if (!rows.children.length) addRow();
                changed();
            });
            rows.appendChild(row);
            return row;
        }

        // Long paths share a prefix, so show each field's end (the folder that differs) and the whole path on hover.
        // Only works on a visible field: callers run it again once the editor is on screen.
        function revealEnds() {
            $$('.nd-map-row input', rows).forEach((input) => {
                input.title = input.value;
                input.scrollLeft = input.scrollWidth;
            });
        }

        function set(mappings) {
            rows.innerHTML = '';
            results.innerHTML = '';
            errorLine.classList.add('d-none');
            lastLibraries = [];
            const list = Array.isArray(mappings) ? mappings.filter((m) => m && (m.app_prefix || m.node_prefix)) : [];
            if (list.length) list.forEach(addRow);
            else addRow();
            initTooltips(root);
            requestAnimationFrame(revealEnds);
        }

        // null (with the half-filled field marked) when a row has only one side.
        function read() {
            const out = [];
            let incomplete = null;
            $$('.nd-map-row', rows).forEach((row) => {
                const app = $('.nd-app', row);
                const node = $('.nd-node', row);
                app.classList.remove('is-invalid');
                node.classList.remove('is-invalid');
                const a = app.value.trim();
                const n = node.value.trim();
                if (!a && !n) return;
                if (!a || !n) {
                    (a ? node : app).classList.add('is-invalid');
                    incomplete = incomplete || (a ? node : app);
                    return;
                }
                out.push({ app_prefix: a, node_prefix: n });
            });
            if (incomplete) {
                showError('Fill in both paths, or remove the row.');
                incomplete.focus();
                return null;
            }
            errorLine.classList.add('d-none');
            return out;
        }

        function showError(text) {
            errorLine.textContent = text;
            errorLine.classList.toggle('d-none', !text);
        }

        function rowFor(appRoot) {
            let best = null;
            let bestLen = -1;
            $$('.nd-map-row', rows).forEach((row) => {
                const prefix = $('.nd-app', row).value.trim();
                if (isUnder(appRoot, prefix) && prefix.length > bestLen) {
                    best = row;
                    bestLen = prefix.length;
                }
            });
            return best;
        }

        function libraryChip(lib) {
            const files = Array.isArray(lib.files) ? lib.files : [];
            const checked = Number.isFinite(Number(lib.checked)) ? Number(lib.checked) : files.length;
            const seen = files.filter((f) => f.ok).length;
            const title = [lib.server, lib.app_root].filter(Boolean).join(' · ');
            if (!checked) return pill('off', `${lib.library}: no files to test`, { title });
            return pill(lib.ok ? 'ok' : 'bad', `${lib.ok ? '✓' : '✗'} ${lib.library} ${seen}/${checked}`, { title });
        }

        function failureCard(lib, index) {
            const files = Array.isArray(lib.files) ? lib.files : [];
            const bad = files.find((f) => !f.ok) || {};
            // A library with no files sampled failed on this app's side; its error says why.
            const why = (!files.length && lib.error) || NOT_VISIBLE[bad.kind] || bad.message || 'not visible on the node';
            const tried = bad.node_path || bad.path || '';
            let html = `<div class="nd-fail" data-library="${esc(lib.library)}">`
                + `<div><span class="nd-fail-h">${esc(lib.library)}: ${esc(String(why).replace(/\.$/, ''))}.</span>`
                + (tried ? ` <span class="text-muted">Tried</span> <span class="nd-mono">${esc(tried)}</span>` : '')
                + '</div>';
            const nearby = lib.nearby && typeof lib.nearby === 'object' ? lib.nearby : {};
            Object.keys(nearby).forEach((dir) => {
                const names = Array.isArray(nearby[dir]) ? nearby[dir] : [];
                html += `<div class="nd-found-row"><span class="nd-faint">Folders the node does see under <span class="nd-mono">${esc(dir)}</span>:</span>`
                    + (names.length ? names.slice(0, 12).map((n) => pill('off', n, { mono: true })).join('') : '<span class="nd-faint">none</span>')
                    + '</div>';
            });
            if (lib.suggestion && lib.suggestion.to) {
                html += '<div class="nd-found-row">'
                    + `<span class="nd-diff">${lib.suggestion.from ? `<s>${esc(lib.suggestion.from)}</s> → ` : ''}<b>${esc(lib.suggestion.to)}</b></span>`
                    + `<button type="button" class="btn btn-sm btn-primary nd-use" data-index="${index}">Use this</button></div>`;
            }
            return `${html}</div>`;
        }

        function renderResults(libraries) {
            lastLibraries = libraries;
            revealEnds();
            $$('.nd-chip-slot', rows).forEach((slot) => { slot.innerHTML = ''; });
            if (!libraries.length) {
                results.innerHTML = '<div class="sm-hint">No libraries to test yet. Add a media server with libraries first.</div>';
                return;
            }
            const loose = [];
            libraries.forEach((lib) => {
                const row = rowFor(lib.app_root);
                if (row) $('.nd-chip-slot', row).insertAdjacentHTML('beforeend', libraryChip(lib));
                else loose.push(libraryChip(lib));
            });
            // A library with no video yet has nothing to test; its chip says so and it isn't a failure.
            const passed = (lib) => lib.ok || lib.empty === true;
            const failures = libraries.map((lib, i) => (passed(lib) ? '' : failureCard(lib, i))).join('');
            const allOk = libraries.every(passed);
            results.innerHTML = (loose.length ? `<div class="nd-lib-chips">${loose.join('')}</div>` : '')
                + (allOk ? `<div class="sm-vmsg ok"><i class="bi bi-check-circle-fill"></i>The node sees every library, same files.</div>` : failures);
        }

        // The suggestion swaps the node-side path of the row covering the library (two rows may share a node path, so
        // it is found by the library's folder first); with no such row it becomes a new row.
        function applySuggestion(lib) {
            const { from, to } = lib.suggestion;
            const match = (lib.app_root && rowFor(lib.app_root))
                || $$('.nd-map-row', rows).find((row) => $('.nd-node', row).value.trim() === from);
            if (match) {
                $('.nd-node', match).value = to;
            } else {
                const empty = $$('.nd-map-row', rows).find((row) => !$('.nd-app', row).value.trim() && !$('.nd-node', row).value.trim());
                const row = empty || addRow();
                $('.nd-app', row).value = lib.app_root || '';
                $('.nd-node', row).value = to;
            }
            revealEnds();
            changed();
        }

        async function test() {
            const mappings = read();
            if (mappings === null) return;
            const btn = $('.nd-test', root);
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Testing…';
            results.innerHTML = '';
            try {
                const id = await getNodeId();
                if (!id) return;
                const r = await api('POST', `/api/nodes/${encodeURIComponent(id)}/test-access`, { path_mappings: mappings });
                if (!r.ok) {
                    showError(errorText(r, "Couldn't test access"));
                    return;
                }
                showError('');
                renderResults(Array.isArray(r.data && r.data.libraries) ? r.data.libraries : []);
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-play-fill me-1"></i>Test access';
            }
        }

        $('.nd-add-row', root).addEventListener('click', () => { addRow(); changed(); });
        $('.nd-test', root).addEventListener('click', test);
        rows.addEventListener('input', changed);
        results.addEventListener('click', (event) => {
            const use = event.target.closest('.nd-use');
            if (!use) return;
            const lib = lastLibraries[Number(use.dataset.index)];
            if (!lib || !lib.suggestion) return;
            applySuggestion(lib);
            test();
        });

        return {
            set,
            read,
            test,
            revealEnds,
            showError,
            applySuggestion,
            setName(name) { $('.nd-map-name', root).textContent = name || 'the node'; },
        };
    }

    // ---------- Add node -----------------------------------------------------------------------------------------

    const add = { probe: null, node: null, editor: null };

    function probeErrorText(error) {
        const kind = (error && error.kind) || '';
        switch (kind) {
            case 'auth':
                return "Key refused. Copy the key again from the node's log, or use the NODE_KEY you set.";
            case 'unreachable':
                return 'Nothing answered at this address. Check the address and port, and that the node is running.';
            case 'protocol':
                // A node on another protocol says which version it runs; anything else isn't a node of this app.
                if (error.node_version) return error.message;
                return `Something answered, but not as a node this app can use. Update the node to the same version as this app${page.appVersion ? ` (${page.appVersion})` : ''}.`;
            case 'request':
                return "The node didn't accept the request. Update the node and this app to the same version.";
            default:
                return (error && error.message) || "Couldn't connect to the node.";
        }
    }

    function addError(text) {
        const el = $('#addNodeError');
        el.textContent = text || '';
        el.classList.toggle('d-none', !text);
    }

    function infoRoles(info) {
        const node = (info && info.node) || {};
        return Array.isArray(node.roles) ? node.roles : [];
    }

    function renderProbe(info) {
        const card = $('#addNodeProbe');
        const node = info.node || {};
        const worker = info.worker || {};
        const version = node.version || '';
        // version_match is null for a node without the worker role: only a worker must match this app's version.
        const same = info.version_match !== false;
        const rows = [];
        rows.push(`<div class="nd-found-row">${pill('ok', '✓ Connected')}`
            + (same
                ? `<span class="nd-faint">version ${esc(version || 'unknown')}${info.version_match === true ? ' · same as this app' : ''}</span>`
                : `${pill('warn', 'Update needed')}<span class="nd-faint">version ${esc(version || 'unknown')}; this app is ${esc(page.appVersion || 'newer')}. Update the node so they match before it gets work.</span>`)
            + '</div>');
        let found = infoRoles(info).map((role) => pill('role', role === ROLE_PLEX_DB ? 'Plex database' : 'Worker')).join('');
        if (String((info.platform || {}).os || '').toLowerCase().startsWith('win')) found += pill('off', 'Windows');
        if (info.legacy) found += pill('off', 'Older Plex helper');
        (Array.isArray(worker.gpus) ? worker.gpus : []).forEach((gpu) => {
            const okGpu = !gpu.status || gpu.status === 'ok';
            const label = [gpu.device, gpu.name].filter(Boolean).join(' · ');
            found += pill(okGpu ? 'off' : 'warn', `${label} · test decode ${okGpu ? '✓' : '✗'}`, gpu.error ? { title: gpu.error } : null);
        });
        const cores = worker.cpu && Number(worker.cpu.cores);
        if (cores) found += pill('off', `${cores} CPU cores`);
        const free = worker.temp && formatBytes(worker.temp.free_bytes);
        if (free) found += pill('off', `${free} free temp`);
        rows.push(`<div class="nd-found-row"><span class="nd-found-k">Found</span>${found || '<span class="nd-faint">no roles reported</span>'}</div>`);
        const plexDb = info.plex_db;
        if (plexDb && typeof plexDb === 'object') {
            rows.push(`<div class="nd-found-row"><span class="nd-found-k">Plex</span>`
                + (plexDb.db_present === false
                    ? `${pill('bad', 'Database not found')}<span class="nd-faint nd-mono">${esc(plexDb.config_dir || '')}</span>`
                    : `${pill('ok', '✓ Database found')}<span class="nd-faint nd-mono">${esc(plexDb.config_dir || '')}</span>`)
                + '</div>');
        }
        (Array.isArray(worker.notes) ? worker.notes : []).forEach((note) => {
            rows.push(`<div class="nd-found-row"><span class="nd-found-k">${esc(note.title || 'Note')}</span>`
                + `${note.chip ? pill('warn', note.chip) : ''}<span class="nd-faint">${esc(note.message || '')}</span></div>`);
        });
        card.innerHTML = `<div class="nd-found">${rows.join('')}</div>`;
        card.classList.remove('d-none');
        initTooltips(card);
    }

    function setAddStep(step) {
        const worker = add.probe && infoRoles(add.probe).includes(ROLE_WORKER);
        $('#addNodeStep1').classList.toggle('d-none', step !== 1);
        $('#addNodeStep2').classList.toggle('d-none', step !== 2);
        $('#addNodeConnect').classList.toggle('d-none', step !== 1);
        $('#addNodeNext').classList.toggle('d-none', step !== 1 || !add.probe);
        $('#addNodeFinish').classList.toggle('d-none', step !== 2);
        $('#addNodeNext').textContent = worker ? 'Next: where it sees your media →' : 'Add node';
        const label = $('#addNodeStepLabel');
        label.textContent = add.probe && !worker ? '' : `Step ${step} of 2`;
        $('#addNodeCancel').textContent = step === 2 ? 'Finish later' : 'Cancel';
        $('#addNodeFootMsg').textContent = step === 2 ? 'The node is saved. You can set this later on its Media tab.' : '';
    }

    function openAdd() {
        add.probe = null;
        add.node = null;
        ['#addNodeName', '#addNodeUrl', '#addNodeKey'].forEach((sel) => {
            const el = $(sel);
            el.value = '';
            el.classList.remove('is-invalid');
        });
        addError('');
        $('#addNodeSaveError').classList.add('d-none');
        $('#addNodeProbe').classList.add('d-none');
        $('#addNodeProbe').innerHTML = '';
        setAddStep(1);
        bootstrap.Modal.getOrCreateInstance($('#addNodeModal')).show();
    }

    // Editing the address or key after connecting means the result shown is no longer about these values.
    function forgetProbe() {
        if (!add.probe) return;
        add.probe = null;
        $('#addNodeProbe').classList.add('d-none');
        setAddStep(1);
    }

    async function connect() {
        const urlEl = $('#addNodeUrl');
        const keyEl = $('#addNodeKey');
        const url = urlEl.value.trim();
        const key = keyEl.value.trim();
        urlEl.classList.toggle('is-invalid', !url);
        keyEl.classList.toggle('is-invalid', !key);
        if (!url || !key) {
            addError(!url ? "Enter the node's address, for example http://host.lan:9494." : "Enter the node's key.");
            (!url ? urlEl : keyEl).focus();
            return;
        }
        addError('');
        const btn = $('#addNodeConnect');
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Connecting…';
        const r = await api('POST', '/api/nodes/probe', { url, key });
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-plug me-1"></i>Connect';
        if (!r.ok || !r.data || r.data.ok === false || !r.data.info) {
            add.probe = null;
            $('#addNodeProbe').classList.add('d-none');
            const error = r.data && typeof r.data.error === 'object' ? r.data.error : { message: errorText(r, "Couldn't connect") };
            addError(probeErrorText(error));
            setAddStep(1);
            return;
        }
        const twin = r.data.info.already_added;
        if (twin) {
            add.probe = null;
            $('#addNodeProbe').classList.add('d-none');
            addError(alreadyAddedText(twin.name));
            setAddStep(1);
            return;
        }
        add.probe = r.data.info;
        const nameEl = $('#addNodeName');
        if (!nameEl.value.trim() && add.probe.node && add.probe.node.name) nameEl.value = add.probe.node.name;
        renderProbe(add.probe);
        setAddStep(1);
    }

    function alreadyAddedText(name) {
        return `This node is already added${name ? ` as "${name}"` : ''}. Open it from the list instead.`;
    }

    // An older Plex helper reports no name of its own; its address's host stands in.
    function hostOf(url) {
        try { return new URL(url).hostname; } catch (_) { return ''; }
    }

    async function saveNewNode() {
        const url = $('#addNodeUrl').value.trim();
        const name = $('#addNodeName').value.trim() || ((add.probe.node || {}).name) || hostOf(url);
        const body = { name, url, key: $('#addNodeKey').value.trim(), path_mappings: [] };
        const btn = $('#addNodeNext');
        btn.disabled = true;
        const r = await api('POST', '/api/nodes', body);
        btn.disabled = false;
        if (!r.ok) {
            if (r.status === 409) {
                addError(alreadyAddedText(r.data && r.data.name));
            } else {
                addError(errorText(r, "Couldn't save the node"));
            }
            return;
        }
        add.node = r.data;
        replaceNode(add.node);
        if (!infoRoles(add.probe).includes(ROLE_WORKER)) {
            bootstrap.Modal.getOrCreateInstance($('#addNodeModal')).hide();
            if (typeof showToast === 'function') showToast('Node added', `${add.node.name || name} is set up.`, 'success');
            return;
        }
        const suggested = Array.isArray(add.node.path_mappings) && add.node.path_mappings.length
            ? add.node.path_mappings
            : (Array.isArray(add.probe.suggested_path_mappings) ? add.probe.suggested_path_mappings : []);
        add.editor.setName(add.node.name || name);
        add.editor.set(suggested);
        setAddStep(2);
    }

    async function finishAdd() {
        const mappings = add.editor.read();
        if (mappings === null) return;
        const btn = $('#addNodeFinish');
        btn.disabled = true;
        const r = await api('PUT', `/api/nodes/${encodeURIComponent(add.node.id)}`, { path_mappings: mappings });
        btn.disabled = false;
        const errorEl = $('#addNodeSaveError');
        if (!r.ok) {
            errorEl.textContent = errorText(r, "Couldn't save the mappings");
            errorEl.classList.remove('d-none');
            return;
        }
        errorEl.classList.add('d-none');
        replaceNode(r.data);
        bootstrap.Modal.getOrCreateInstance($('#addNodeModal')).hide();
        if (typeof showToast === 'function') showToast('Node added', `${(r.data && r.data.name) || add.node.name} is set up.`, 'success');
    }

    // ---------- node dialog --------------------------------------------------------------------------------------

    const detail = { editor: null, healthSeq: 0, healthStartedAt: 0 };

    function openNodeObj() {
        return page.openId ? nodeById(page.openId) : null;
    }

    function renderHeader(node) {
        const [tone, text] = stateOf(node);
        $('#nodeModalName').textContent = node.name || node.id;
        const state = $('#nodeModalState');
        state.className = `pill${tone !== 'off' ? ` ${tone}` : ''}`;
        state.textContent = text;
        $('#nodeModalUrl').textContent = node.url || '';
        $('#nodeModalIcon').innerHTML = `<i class="bi ${nodeIcon(node)}"></i>`;
    }

    function kv(label, valueHtml) {
        return `<div class="markers-kv-label">${esc(label)}</div><div>${valueHtml}</div>`;
    }

    function renderOverview(node) {
        const [tone, text] = stateOf(node);
        const cpu = node.cpu || {};
        const rows = [kv('State', `${pill(tone, text)}${node.reason ? `<span class="text-muted">${esc(node.reason)}</span>` : ''}`)];
        rows.push(kv('Roles', roleChips(node) || '<span class="text-muted">none reported yet</span>'));
        if (node.version) {
            const same = !page.appVersion || node.version === page.appVersion;
            rows.push(kv('Version', `<span class="nd-mono">${esc(node.version)}</span>`
                + (same ? '<span class="text-muted">same as this app</span>' : `<span class="text-muted">this app is ${esc(page.appVersion)}</span>`)));
        }
        const seen = ago(node.last_seen);
        if (seen) rows.push(kv('Last answered', esc(seen)));
        const platform = node.platform || {};
        if (platform.os) rows.push(kv('System', esc([platform.os, platform.arch].filter(Boolean).join(' · '))));
        if (cpu.cores) {
            const load = Number.isFinite(Number(cpu.load1)) && cpu.load1 !== null ? ` · load ${Number(cpu.load1).toFixed(1)}` : '';
            rows.push(kv('CPU', `${esc(cpu.cores)} cores${esc(load)}${infoIcon('Load is the average number of busy cores over the last minute.')}`));
        }
        if (cpu.ram_total) {
            rows.push(kv('Memory', esc(`${formatBytes(cpu.ram_available)} free of ${formatBytes(cpu.ram_total)}`)));
        }
        if (isWorker(node)) {
            const busy = busyText(node.busy);
            if (busy) rows.push(kv('Busy', esc(busy)));
        }
        const used = usedByParts(node.used_by);
        rows.push(kv('Used by', used.length ? esc(used.join(', ')) : `<span class="text-muted">nothing yet</span>${isWorker(node) ? ` · <a href="/settings#section-workers">Add it to a worker group</a>` : ''}`));
        const overview = $('#nodeOverview');
        disposeTooltips(overview);
        overview.innerHTML = `<section class="sm-sec"><div class="sm-sec-h">Node</div><div class="markers-kv">${rows.join('')}</div></section>`;
        initTooltips(overview);

        const gpuSection = $('#nodeGpuSection');
        gpuSection.classList.toggle('d-none', !isWorker(node));
        const list = $('#nodeGpuList');
        const items = gpus(node);
        list.innerHTML = items.length
            ? items.map((gpu) => {
                const okGpu = !gpu.status || gpu.status === 'ok';
                return `<div class="nd-gpu"><b>${esc(gpu.name || gpu.device || 'GPU')}</b>`
                    + pill('off', [gpu.type, gpu.device].filter(Boolean).join(' · '), { mono: true })
                    + (okGpu ? pill('ok', '✓ Working') : pill('bad', '✗ Not working'))
                    + (gpu.error ? `<span class="nd-faint">${esc(gpu.error)}</span>` : '')
                    + '</div>';
            }).join('')
            : '<div class="sm-hint">No GPU found. CPU work still runs on this node.</div>';
    }

    function renderSettings(node) {
        $('#nodeSetName').value = node.name || '';
        $('#nodeSetUrl').value = node.url || '';
        // The stored key comes back masked; posting the mask back keeps it.
        $('#nodeSetKey').value = '****';
        $('#nodeSetEnabled').checked = node.enabled !== false;
        $('#nodeSettingsResult').textContent = '';
        $('#nodeRemoveResult').classList.add('d-none');
    }

    function showTab(tab) {
        const trigger = $(`#nodeModal .sm-rail [data-tab="${tab}"]`);
        if (trigger && !trigger.closest('.d-none')) bootstrap.Tab.getOrCreateInstance(trigger).show();
    }

    function openNode(id, tab) {
        const node = nodeById(id);
        if (!node) return;
        page.openId = String(node.id);
        renderHeader(node);
        renderOverview(node);
        renderSettings(node);
        $('#nodeTabMediaLi').classList.toggle('d-none', !isWorker(node));
        detail.editor.setName(node.name || node.id);
        detail.editor.set(node.path_mappings);
        $('#nodeMediaResult').textContent = '';
        $('#nodeLog').textContent = '';
        $('#nodeHealthBody').innerHTML = '';
        $('#nodeHealthSummary').innerHTML = '';
        setHealthBadge(null);
        const wanted = tab === 'media' && !isWorker(node) ? 'settings' : (tab || 'overview');
        showTab(wanted);
        // Loaded on open for the rail's count badge, whichever tab opens first.
        loadHealth();
        if (wanted === 'log') loadLog();
        if (window.location.hash !== `#node-${node.id}`) history.replaceState(null, '', `#node-${node.id}`);
        bootstrap.Modal.getOrCreateInstance($('#nodeModal')).show();
    }

    // A poll refreshes what the open dialog shows about the node, never the fields being edited.
    function refreshOpenNode() {
        const node = openNodeObj();
        if (!node) return;
        renderHeader(node);
        renderOverview(node);
    }

    async function rescanGpus() {
        const node = openNodeObj();
        if (!node) return;
        const btn = $('#nodeRescanGpus');
        const result = $('#nodeRescanResult');
        btn.disabled = true;
        result.className = 'sm-tres';
        result.innerHTML = '<span class="sm-spin"></span>Checking the GPUs…';
        const r = await api('POST', `/api/nodes/${encodeURIComponent(node.id)}/rescan-gpus`);
        btn.disabled = false;
        if (!r.ok) {
            result.className = 'sm-tres bad';
            result.textContent = errorText(r, "Couldn't rescan");
            return;
        }
        replaceNode(r.data);
        refreshOpenNode();
        const count = gpus(r.data).length;
        result.className = 'sm-tres ok';
        result.textContent = count ? `Found ${count} ${count === 1 ? 'GPU' : 'GPUs'}` : 'No GPU found';
    }

    async function saveMappings() {
        const node = openNodeObj();
        if (!node) return;
        const mappings = detail.editor.read();
        if (mappings === null) return;
        const result = $('#nodeMediaResult');
        const btn = $('#nodeSaveMappings');
        btn.disabled = true;
        const r = await api('PUT', `/api/nodes/${encodeURIComponent(node.id)}`, { path_mappings: mappings });
        btn.disabled = false;
        if (!r.ok) {
            result.className = 'sm-tres bad';
            result.textContent = errorText(r, "Couldn't save");
            return;
        }
        replaceNode(r.data);
        refreshOpenNode();
        result.className = 'sm-tres ok';
        result.textContent = 'Saved';
    }

    async function saveSettings() {
        const node = openNodeObj();
        if (!node) return;
        const nameEl = $('#nodeSetName');
        const urlEl = $('#nodeSetUrl');
        const keyEl = $('#nodeSetKey');
        nameEl.classList.toggle('is-invalid', !nameEl.value.trim());
        urlEl.classList.toggle('is-invalid', !urlEl.value.trim());
        keyEl.classList.toggle('is-invalid', !keyEl.value.trim());
        const result = $('#nodeSettingsResult');
        if (!nameEl.value.trim() || !urlEl.value.trim() || !keyEl.value.trim()) {
            result.className = 'sm-tres bad';
            result.textContent = 'Name, address and key are all needed.';
            return;
        }
        const body = {
            name: nameEl.value.trim(),
            url: urlEl.value.trim(),
            key: keyEl.value.trim(),
            enabled: $('#nodeSetEnabled').checked,
        };
        const btn = $('#nodeSettingsSave');
        btn.disabled = true;
        const r = await api('PUT', `/api/nodes/${encodeURIComponent(node.id)}`, body);
        btn.disabled = false;
        if (!r.ok) {
            result.className = 'sm-tres bad';
            result.textContent = errorText(r, "Couldn't save");
            return;
        }
        replaceNode(r.data);
        refreshOpenNode();
        detail.editor.setName(r.data.name || r.data.id);
        keyEl.value = '****';
        result.className = 'sm-tres ok';
        result.textContent = 'Saved';
    }

    async function removeNode() {
        const node = openNodeObj();
        if (!node) return;
        const out = $('#nodeRemoveResult');
        out.classList.add('d-none');
        const ok = await window.appConfirm(
            `Remove node "${node.name || node.id}"? The node keeps running; this app stops using it.`,
            { title: 'Remove node', confirmText: 'Remove' },
        );
        if (!ok) return;
        const r = await api('DELETE', `/api/nodes/${encodeURIComponent(node.id)}`);
        if (r.status === 409) {
            const parts = usedByParts(r.data && r.data.used_by);
            out.textContent = parts.length
                ? `Still in use by ${parts.join(', ')}. Take it out of those first.`
                : errorText(r, 'Still in use');
            out.classList.remove('d-none');
            return;
        }
        if (!r.ok) {
            out.textContent = errorText(r, "Couldn't remove the node");
            out.classList.remove('d-none');
            return;
        }
        page.nodes = page.nodes.filter((n) => String(n.id) !== String(node.id));
        renderList();
        bootstrap.Modal.getOrCreateInstance($('#nodeModal')).hide();
        if (typeof showToast === 'function') showToast('Node removed', `${node.name || node.id} was removed.`, 'success');
    }

    async function loadLog() {
        const node = openNodeObj();
        if (!node) return;
        const pre = $('#nodeLog');
        pre.textContent = 'Loading…';
        const r = await api('GET', `/api/nodes/${encodeURIComponent(node.id)}/log?lines=${LOG_LINES}`);
        if (!r.ok) {
            pre.textContent = errorText(r, "Couldn't read the node's log");
            return;
        }
        const lines = Array.isArray(r.data && r.data.lines) ? r.data.lines : [];
        // The API gives each time in this app's own zone (ISO 8601); shown as the app's own log shows its times.
        const shownTime = (time) => String(time || '').replace('T', ' ').replace(/(?:[+-]\d\d:\d\d|Z)$/, '');
        pre.textContent = lines.length
            ? lines.map((line) => [shownTime(line.time), line.level, line.message].filter(Boolean).join(' ')).join('\n')
            : 'No log lines yet.';
        pre.scrollTop = pre.scrollHeight;
    }

    // ---------- node Setup Health --------------------------------------------------------------------------------

    function setHealthBadge(partition) {
        const badge = $('#nodeHealthTabBadge');
        if (!partition || (!partition.mustFix.length && !partition.recommended.length)) {
            badge.className = 'sm-nb d-none';
            badge.textContent = '';
            return;
        }
        const bad = partition.mustFix.length > 0;
        badge.className = `sm-nb ${bad ? 'bad' : 'warn'}`;
        badge.textContent = String(bad ? partition.mustFix.length : partition.recommended.length);
    }

    // Same buckets as server Setup Health: critical + failing must be fixed, recommended + failing is recommended,
    // everything passing is all good; informational rows only show when they say they're meant to be seen.
    function partitionChecks(sections) {
        const mustFix = [];
        const recommended = [];
        const allGood = [];
        (sections || []).forEach((section) => {
            (section.checks || []).forEach((check) => {
                if ((check.severity || 'info') === 'info' && check.informational !== true) return;
                const item = { check, sectionId: section.id || '', sectionTitle: section.title || section.id || '' };
                if (check.ok === false && check.severity === 'critical') mustFix.push(item);
                else if (check.ok === false && check.severity === 'recommended' && check.dismissed !== true) recommended.push(item);
                else allGood.push(item);
            });
        });
        return { mustFix, recommended, allGood };
    }

    function healthValue(value) {
        if (value === true) return 'On';
        if (value === false) return 'Off';
        if (value && typeof value === 'object') return JSON.stringify(value);
        return String(value);
    }

    function valueDiff(check, ok) {
        const has = (v) => v !== null && v !== undefined;
        const { current, recommended } = check;
        if (!has(current) && !has(recommended)) return '';
        if (has(current) && has(recommended) && !ok) {
            return '<div class="readiness-values d-flex flex-wrap gap-2 mt-1">'
                + `<div class="readiness-current px-2 py-1 rounded"><span class="readiness-label">Currently</span> <code>${esc(healthValue(current))}</code></div>`
                + '<div class="readiness-arrow text-body-tertiary align-self-center">→</div>'
                + `<div class="readiness-recommended px-2 py-1 rounded"><span class="readiness-label">Recommended</span> <code>${esc(healthValue(recommended))}</code></div></div>`;
        }
        if (has(current)) {
            return `<div class="readiness-currently text-muted mt-1">${ok ? '<i class="bi bi-check2 me-1"></i>' : ''}Currently <code>${esc(healthValue(current))}</code></div>`;
        }
        return `<div class="readiness-currently text-muted mt-1">Recommended <code>${esc(healthValue(recommended))}</code></div>`;
    }

    function checkRow(check) {
        const ok = check.ok !== false;
        const sev = check.severity || 'info';
        const tone = ok ? 'ok' : (sev === 'critical' ? 'bad' : 'warn');
        const row = document.createElement('div');
        row.className = `rd-row ${tone}`;
        let badge = '';
        if (!ok && check.fix_where === 'node') {
            badge = '<span class="badge bg-danger-subtle text-danger-emphasis border border-danger-subtle" title="This app can\'t change this for you: fix it on the node\'s machine.">Fix on the node</span>';
        } else if (ok && sev === 'critical') {
            badge = '<span class="badge bg-success-subtle text-success-emphasis border border-success-subtle" title="Required check, passing.">Required</span>';
        }
        const tooltip = check.tooltip || check.reason || '';
        const explanation = (check.reason ? `<p>${esc(check.reason)}</p>` : '') + (check.explanation || '');
        const info = tooltip
            ? `<button type="button" class="info-icon" data-bs-toggle="tooltip" data-bs-placement="top" title="${esc(tooltip)}" data-explain-title="${esc(check.label || 'About this check')}" aria-label="Explain ${esc(check.label || '')}"><i class="bi bi-info-circle"></i></button>`
            : '';
        const reason = check.reason ? `<div class="rd-reason" title="${esc(check.reason)}">${esc(check.reason)}</div>` : '';
        row.innerHTML = '<span class="rd-sev" aria-hidden="true"></span>'
            + `<div class="rd-main"><div class="rd-title"><span>${esc(check.label || check.id || '')}</span>${badge}${info}</div>${reason}${valueDiff(check, ok)}</div>`
            + '<div class="rd-acts"></div>';
        // Backend explanations are authored text (the same contract as server Setup Health), set as a DOM property.
        if (check.explanation) {
            const infoBtn = $('.info-icon', row);
            if (infoBtn) infoBtn._explanationHtml = explanation;
        }
        const acts = $('.rd-acts', row);
        if (!ok && ['media', 'settings', 'overview'].includes(check.fix_tab)) {
            const fix = document.createElement('button');
            fix.type = 'button';
            fix.className = 'btn btn-sm btn-warning nd-health-fix';
            fix.textContent = check.fix_label || 'Fix';
            fix.addEventListener('click', () => {
                showTab(check.fix_tab);
                if (check.fix_tab !== 'media') return;
                if (check.suggestion && check.suggestion.to) {
                    detail.editor.applySuggestion({ suggestion: check.suggestion, app_root: check.suggestion.app_root || '' });
                    detail.editor.test();
                } else if (check.fix_action === 'test_access') {
                    detail.editor.test();
                }
            });
            acts.appendChild(fix);
        }
        if (typeof check.help_url === 'string' && check.help_url.startsWith('/')) {
            const link = document.createElement('a');
            link.className = 'btn btn-sm btn-outline-secondary';
            link.href = check.help_url;
            link.textContent = check.help_label || 'Open';
            acts.appendChild(link);
        }
        return row;
    }

    function bucket(tier, title, items, expanded, iconHtml) {
        const det = document.createElement('details');
        det.className = 'readiness-bucket';
        det.dataset.tier = tier;
        if (expanded) det.open = true;
        const sum = document.createElement('summary');
        sum.className = 'd-flex align-items-center gap-2 user-select-none';
        sum.innerHTML = `${iconHtml}<span class="fw-semibold">${esc(title)}</span><span class="badge ms-1">${items.length}</span>`;
        det.appendChild(sum);
        const inner = document.createElement('div');
        inner.className = 'readiness-bucket-body';
        let lastSection = null;
        items.forEach((item) => {
            if (item.sectionId !== lastSection && item.sectionTitle) {
                lastSection = item.sectionId;
                const head = document.createElement('div');
                head.className = 'rd-cat';
                head.textContent = item.sectionTitle;
                inner.appendChild(head);
            }
            inner.appendChild(checkRow(item.check));
        });
        det.appendChild(inner);
        return det;
    }

    function renderHealth(node, data) {
        const body = $('#nodeHealthBody');
        disposeTooltips(body);
        body.innerHTML = '';
        $('#nodeHealthTitle').textContent = `${node.name || node.id} · Setup Health`;
        const partition = partitionChecks(data.sections);
        setHealthBadge(partition);
        const summary = [];
        if (partition.mustFix.length) summary.push(pill('bad', `${partition.mustFix.length} must fix`));
        if (partition.recommended.length) summary.push(pill('warn', `${partition.recommended.length} recommended`));
        if (!summary.length) summary.push(pill('ok', '✓ All good'));
        $('#nodeHealthSummary').innerHTML = summary.join('');
        if (partition.mustFix.length) {
            body.appendChild(bucket('critical', 'Must fix', partition.mustFix, true, '<i class="bi bi-x-octagon-fill text-danger me-2"></i>'));
        }
        if (partition.recommended.length) {
            body.appendChild(bucket('recommended', 'Recommended', partition.recommended, !partition.mustFix.length, '<i class="bi bi-exclamation-triangle-fill text-warning me-2"></i>'));
        }
        if (partition.allGood.length) {
            body.appendChild(bucket('ok', 'All good', partition.allGood, !partition.mustFix.length && !partition.recommended.length, '<i class="bi bi-check-circle-fill text-success me-2"></i>'));
        }
        if (!body.children.length) body.innerHTML = '<div class="sm-hint">No checks to show yet.</div>';
        const firstFix = $('.nd-health-fix', body);
        if (firstFix) firstFix.classList.add('rd-primary');
        initTooltips(body);
    }

    async function loadHealth() {
        const node = openNodeObj();
        if (!node) return;
        const seq = ++detail.healthSeq;
        detail.healthStartedAt = Date.now();
        const body = $('#nodeHealthBody');
        body.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Checking…</div>';
        const r = await api('GET', `/api/nodes/${encodeURIComponent(node.id)}/health`);
        if (seq !== detail.healthSeq || page.openId !== String(node.id)) return;
        if (!r.ok || !r.data) {
            body.innerHTML = `<div class="small text-muted"><i class="bi bi-exclamation-circle me-1"></i>${esc(errorText(r, "Couldn't check this node"))} · <a href="#" class="nd-health-retry">Retry</a></div>`;
            return;
        }
        renderHealth(node, r.data);
    }

    // ---------- wiring -------------------------------------------------------------------------------------------

    function wire() {
        add.editor = createMappingEditor($('#addNodeMappings'), { getNodeId: async () => add.node && add.node.id });
        detail.editor = createMappingEditor($('#nodeMediaEditor'), {
            getNodeId: async () => page.openId,
            onChange: () => {
                const result = $('#nodeMediaResult');
                result.className = 'sm-tres warn';
                result.textContent = 'Not saved yet';
            },
        });

        $('#nodesAddBtn').addEventListener('click', openAdd);
        $('#nodesCheckNow').addEventListener('click', checkAll);
        $('#nodeList').addEventListener('click', (event) => {
            const open = event.target.closest('.nd-open');
            if (open) {
                openNode(open.dataset.id, open.dataset.tab);
                return;
            }
            if (event.target.closest('.nd-add')) openAdd();
        });

        $('#addNodeConnect').addEventListener('click', connect);
        $('#addNodeNext').addEventListener('click', saveNewNode);
        $('#addNodeFinish').addEventListener('click', finishAdd);
        ['#addNodeUrl', '#addNodeKey'].forEach((sel) => $(sel).addEventListener('input', () => {
            $(sel).classList.remove('is-invalid');
            forgetProbe();
        }));
        $('#addNodeModal').addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && event.target.matches('#addNodeStep1 input')) {
                event.preventDefault();
                if (add.probe) saveNewNode();
                else connect();
            }
        });
        $('#addNodeModal').addEventListener('hidden.bs.modal', () => {
            if (window.location.hash === '#add') history.replaceState(null, '', window.location.pathname);
            if (add.node) loadNodes();
        });

        $('#nodeModal').addEventListener('hidden.bs.modal', () => {
            page.openId = null;
            if (window.location.hash.startsWith('#node-')) history.replaceState(null, '', window.location.pathname);
        });
        $$('#nodeModal .sm-rail [data-bs-toggle="tab"]').forEach((trigger) => {
            trigger.addEventListener('shown.bs.tab', () => {
                // Opening the dialog already loaded it; switching to the tab later checks again.
                if (trigger.dataset.tab === 'health' && Date.now() - detail.healthStartedAt > 2000) loadHealth();
                if (trigger.dataset.tab === 'log') loadLog();
                if (trigger.dataset.tab === 'media') detail.editor.revealEnds();
            });
        });
        $('#nodeRescanGpus').addEventListener('click', rescanGpus);
        $('#nodeSaveMappings').addEventListener('click', saveMappings);
        $('#nodeSettingsSave').addEventListener('click', saveSettings);
        $('#nodeRemove').addEventListener('click', removeNode);
        $('#nodeLogRefresh').addEventListener('click', loadLog);
        $('#nodeHealthRefresh').addEventListener('click', loadHealth);
        $('#nodeHealthBody').addEventListener('click', (event) => {
            if (!event.target.closest('.nd-health-retry')) return;
            event.preventDefault();
            loadHealth();
        });

        window.addEventListener('hashchange', openFromHash);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) loadNodes(); });
        loadNodes();
        startPolling();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();
})();
