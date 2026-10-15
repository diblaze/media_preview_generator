// Nodes page: the node list (polled while the page is visible), the Add node dialog and the node dialog (Overview,
// Media, Setup Health, Log, Settings). Everything goes through /api/nodes*; csrf_fetch.js adds the CSRF header.

(function () {
    'use strict';

    const POLL_MS = 10000;
    // Check now: the server checks in the background, so the list is read again every second until no node is
    // still being checked (a node that doesn't answer takes up to ~40 s), giving up after a minute.
    const CHECK_POLL_MS = 1000;
    const CHECK_WAIT_MS = 60000;
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

    // Disabling the focused button drops focus to <body>, outside the dialog, where Esc and Tab no longer reach it;
    // focus goes back to the button once it works again.
    function enable(btn) {
        btn.disabled = false;
        if (!document.activeElement || document.activeElement === document.body) btn.focus();
    }

    // The first field in scope a user can type in: focus given there instead of falling to the page.
    function firstField(scope) {
        return $$('input, select, textarea', scope).find((el) => !el.disabled && el.getClientRects().length) || null;
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

    // The row's button says Fix for these, and opens the tab where the fix is.
    const FIX_TAB = {
        access_problem: 'media',
        key_refused: 'settings',
        update_needed: 'health',
        gpu_lost: 'health',
        no_ffmpeg: 'health',
        temp_problem: 'health',
        offline: 'health',
        // Its Remove button is there.
        duplicate: 'settings',
    };
    const NODE_TABS = ['overview', 'media', 'health', 'log', 'settings'];

    // [tone, chip text], from the names and colours every page uses (app.js NODE_STATES, read when used: this file
    // loads before app.js); here Ready gets a tick.
    function stateOf(node) {
        const state = (window.NODE_STATES || {})[node.state];
        if (!state) return ['off', node.state || 'Unknown'];
        return [state.tone, node.state === 'ready' ? `✓ ${state.label}` : state.label];
    }

    // In these the node didn't just answer as itself, so its cores, GPUs, load and files running are an old answer
    // (or another machine's): the card says when it was seen instead.
    const OLD_FACTS = ['offline', 'connecting', 'disabled', 'key_refused', 'duplicate'];
    // Only these can have files running on them.
    const TAKES_FILES = ['ready', 'gpu_lost', 'access_problem'];
    // Only these give workers: in every other state the node gets no work at all, CPU work included.
    const GIVES_WORKERS = ['ready', 'gpu_lost'];

    function factsAreCurrent(node) {
        return !OLD_FACTS.includes(node.state);
    }

    function filesRunningText(node) {
        return isWorker(node) && TAKES_FILES.includes(node.state) ? busyText(node.busy) : '';
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
            html += pill('off', 'Older Plex database node')
                + infoIcon("Keeps working as it is. Update it to the latest version for Setup Health and Windows support.");
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

    const page = { nodes: [], appVersion: '', loaded: false, timer: null, loading: false, checking: false, openId: null };

    function nodeIcon(node) {
        if (isWindows(node)) return 'bi-windows';
        if (!isWorker(node) && isPlexDb(node)) return 'bi-database';
        return 'bi-pc-display-horizontal';
    }

    // A Ready card whose Setup Health still has rows to fix says how many, counted as the dialog's rail badge counts.
    function toFixBadge(node) {
        const counts = node.to_fix || {};
        const mustFix = Number(counts.must_fix) || 0;
        const recommended = Number(counts.recommended) || 0;
        if (node.state !== 'ready' || (!mustFix && !recommended)) return null;
        return mustFix ? { tone: 'bad', count: mustFix, fix: true } : { tone: 'warn', count: recommended, fix: false };
    }

    function nodeRow(node) {
        const [tone, text] = stateOf(node);
        const badge = toFixBadge(node);
        const fixTab = FIX_TAB[node.state] || (badge && badge.fix ? 'health' : '');
        const openTab = fixTab || (badge ? 'health' : 'overview');
        const used = usedByParts(node.used_by);
        const busy = filesRunningText(node);
        const hardware = factsAreCurrent(node) ? hardwareChips(node) : '';
        // A turned-off node's reason ("Turned off on the Nodes page") only repeats its chip.
        const reason = node.reason && node.state !== 'disabled' ? `<span class="nd-faint">${esc(node.reason)}</span>` : '';
        const version = versionLine(node);
        const name = esc(node.name || node.id);
        return `
            <article class="nd-row${node.enabled === false ? ' off' : ''}" id="node-${esc(node.id)}" data-id="${esc(node.id)}" data-state="${esc(node.state || '')}" data-tone="${esc(tone)}">
                <div class="nd-ico" aria-hidden="true"><i class="bi ${nodeIcon(node)}"></i></div>
                <div class="nd-id">
                    <h2 class="nd-name" title="${name}">${name}</h2>
                    <div class="nd-url" title="${esc(node.url)}">${esc(node.url)}</div>
                </div>
                <div class="nd-chips">${roleChips(node)}${hardware}${busy ? `<span class="nd-faint nd-busy">${esc(busy)}</span>` : ''}${used.length ? `<span class="nd-faint">used by ${esc(used.join(', '))}</span>` : ''}</div>
                <div class="nd-status">${pill(tone, text)}${badge ? `<span class="pill ${badge.tone} nd-to-fix" title="Open Setup Health to see what to fix">${badge.count} to fix</span>` : ''}${reason}${version ? `<span class="nd-faint nd-version">${esc(version)}</span>` : ''}</div>
                <div class="nd-acts">
                    <button type="button" class="btn btn-sm ${fixTab ? 'btn-warning' : 'btn-outline-secondary'} nd-open" data-id="${esc(node.id)}" data-tab="${openTab}" aria-label="${fixTab ? 'Fix' : 'Open'} ${name}">${fixTab ? 'Fix' : 'Open'}</button>
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

    // A poll redraws only the rows that changed, and a redrawn row hands keyboard focus to its own new button:
    // replacing every row every poll dropped the focus of a keyboard user to the page.
    function renderRows(list) {
        const old = new Map($$(':scope > .nd-row', list).map((el) => [el.dataset.id, el]));
        $$(':scope > :not(.nd-row)', list).forEach((el) => el.remove());
        let before = list.firstElementChild;
        page.nodes.forEach((node) => {
            const html = nodeRow(node);
            let el = old.get(String(node.id));
            old.delete(String(node.id));
            if (!el || el._markup !== html) {
                const tpl = document.createElement('template');
                tpl.innerHTML = html.trim();
                const fresh = tpl.content.firstElementChild;
                fresh._markup = html;
                const hadFocus = el && el.contains(document.activeElement);
                if (el) {
                    disposeTooltips(el);
                    if (before === el) before = fresh;
                    el.replaceWith(fresh);
                }
                initTooltips(fresh);
                if (hadFocus) $('.nd-open', fresh).focus();
                el = fresh;
            }
            if (el !== before) list.insertBefore(el, before);
            before = el.nextElementSibling;
        });
        old.forEach((el) => {
            disposeTooltips(el);
            el.remove();
        });
    }

    function renderList() {
        const list = $('#nodeList');
        if (!list) return;
        if (page.nodes.length) {
            renderRows(list);
        } else {
            disposeTooltips(list);
            list.innerHTML = emptyState();
        }
        const checkNow = $('#nodesCheckNow');
        // With no node there is nothing to check: the button is hidden, not left greyed out.
        if (checkNow) {
            checkNow.disabled = !page.nodes.length || page.checking;
            checkNow.classList.toggle('d-none', !page.nodes.length);
        }
    }

    // Answers the rows it showed, or null when it read nothing (a read already under way, or a failed one).
    async function loadNodes() {
        if (page.loading) return null;
        page.loading = true;
        const r = await api('GET', '/api/nodes');
        page.loading = false;
        if (!r.ok) {
            // A failed poll keeps the list already on screen; only the first load shows the error.
            if (!page.loaded) {
                $('#nodeList').innerHTML = `<div class="nd-state"><div class="alert alert-danger mb-0">${esc(errorText(r, "Couldn't load nodes"))}</div></div>`;
            }
            return null;
        }
        page.nodes = Array.isArray(r.data && r.data.nodes) ? r.data.nodes : [];
        page.appVersion = (r.data && r.data.app_version) || '';
        const first = !page.loaded;
        page.loaded = true;
        renderList();
        if (page.openId) refreshOpenNode();
        if (first) openFromHash();
        return page.nodes;
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
        page.checking = true;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Checking…';
        const r = await api('POST', '/api/nodes/check');
        // Only reads begun after the request say whether its checks are done (a poll already under way doesn't).
        let rows = r.ok && r.data && Array.isArray(r.data.nodes) ? r.data.nodes : [];
        const until = Date.now() + CHECK_WAIT_MS;
        while (rows.some((n) => n.checking) && Date.now() < until) {
            await new Promise((resolve) => setTimeout(resolve, CHECK_POLL_MS));
            rows = (await loadNodes()) || rows;
        }
        page.checking = false;
        btn.innerHTML = '<i class="bi bi-arrow-repeat me-1"></i>Check now';
        if (page.nodes.length) enable(btn);
        else btn.disabled = true;
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
        // "node-<id>", "node-<id>/<tab>", or "node-<id>/media/test" (the bell's Test access: open Media and run it).
        const [id, tab, action] = hash.slice('node-'.length).split('/');
        if (!nodeById(id)) return;
        const row = document.getElementById(`node-${id}`);
        if (row) row.scrollIntoView({ block: 'center' });
        openNode(id, NODE_TABS.includes(tab) ? tab : undefined);
        if (tab === 'media' && action === 'test' && isWorker(nodeById(id))) detail.editor.test();
    }

    // ---------- path mappings + Test access ------------------------------------------------------------------------

    // Why the node didn't see a file, from the access check's reason.
    const NOT_VISIBLE = {
        unmapped: 'no mapping covers this folder',
        outside_roots: 'it resolves to a folder no mapping covers. If it\'s a link, add a row for the folder it points into',
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
    // What to do first when there is no library to map, from media_setup ({servers, libraries}).
    function noLibrariesHtml(setup) {
        const servers = setup && Number(setup.servers);
        if (servers) {
            return `Your media servers have no libraries loaded yet. <a href="/servers">Refresh libraries on Media Servers</a> first.`;
        }
        return 'No media server yet. <a href="/servers">Add a media server</a> first.';
    }

    function createMappingEditor(root, { getNodeId, onChange, onTested }) {
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
            // The mini labels stand in for the column headers on a phone, where the fields stack.
            row.innerHTML = `
                <span class="nd-mini-lab nd-mini-app" aria-hidden="true">Path on this app</span>
                <input type="text" class="form-control font-monospace nd-app" aria-label="Path on this app" placeholder="/data/Movies" value="${esc(m.app_prefix)}">
                <span class="nd-arrow" aria-hidden="true">→</span>
                <span class="nd-mini-lab nd-mini-node" aria-hidden="true">Path on the node</span>
                <input type="text" class="form-control font-monospace nd-node" aria-label="Path on the node" placeholder="/mnt/nas/Movies" value="${esc(m.node_prefix)}">
                <div class="nd-chip-slot"></div>
                <button type="button" class="btn btn-sm btn-outline-danger nd-remove" aria-label="Remove mapping" title="Remove mapping"><i class="bi bi-x-lg"></i></button>`;
            $('.nd-remove', row).addEventListener('click', () => {
                // The removed button took focus with it, to the page, where Esc no longer closes the dialog.
                const neighbour = row.nextElementSibling || row.previousElementSibling;
                row.remove();
                if (!rows.children.length) addRow();
                changed();
                (neighbour ? $('.nd-remove', neighbour) : $('.nd-add-row', root)).focus();
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

        // Library names more than one server uses (every Plex has its "Films"): those chips and cards name the server.
        let repeatedNames = new Set();

        function libraryLabel(lib) {
            return repeatedNames.has(lib.library) && lib.server ? `${lib.library} (${lib.server})` : lib.library;
        }

        function libraryChip(lib) {
            const files = Array.isArray(lib.files) ? lib.files : [];
            const checked = Number.isFinite(Number(lib.checked)) ? Number(lib.checked) : files.length;
            const seen = files.filter((f) => f.ok).length;
            const title = [lib.server, lib.app_root].filter(Boolean).join(' · ');
            const name = libraryLabel(lib);
            if (!checked) return pill('off', `${name}: no files to test`, { title });
            return pill(lib.ok ? 'ok' : 'bad', `${lib.ok ? '✓' : '✗'} ${name} ${seen}/${checked}`, { title });
        }

        function failureCard(lib, index) {
            const files = Array.isArray(lib.files) ? lib.files : [];
            const bad = files.find((f) => !f.ok) || {};
            // A library with no files sampled failed on this app's side, and one the node lacks entirely is said
            // so plainly; either way its error says why.
            // A link's own message names the folder it points into, which the user has to map.
            const why = lib.error || (bad.kind === 'outside_roots' && bad.message) || NOT_VISIBLE[bad.kind] || bad.message
                || 'not visible on the node';
            const tried = bad.node_path || bad.path || '';
            let html = `<div class="nd-fail" data-library="${esc(lib.library)}">`
                + `<div><span class="nd-fail-h">${esc(libraryLabel(lib))}: ${esc(String(why).replace(/\.$/, ''))}.</span>`
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

        function skippedNote(skipped) {
            if (!skipped.length) return '';
            // A result is one folder: a library with two folders is still one library skipped.
            const byLibrary = new Map();
            skipped.forEach((lib) => {
                const name = [lib.server, lib.library].filter(Boolean).join(' · ');
                if (!byLibrary.has(name)) byLibrary.set(name, lib.error);
            });
            const count = byLibrary.size;
            const items = Array.from(byLibrary, ([name, error]) => `<li><b>${esc(name)}</b> <span class="nd-faint">${esc(error)}</span></li>`).join('');
            return `<details class="nd-note nd-skipped"><summary><i class="bi bi-info-circle" aria-hidden="true"></i>`
                + `${count} ${count === 1 ? 'library' : 'libraries'} skipped: this app can't open ${count === 1 ? 'it' : 'them'}.</summary>`
                + `<ul>${items}</ul><a href="/servers">Fix on Media Servers</a></details>`;
        }

        function unmappedNote(unmapped) {
            if (!unmapped.length) return '';
            const items = unmapped.map((lib) => `<li><b>${esc(libraryLabel(lib))}</b> <span class="nd-faint">${esc(lib.error)}</span></li>`).join('');
            return `<details class="nd-note nd-skipped"><summary><i class="bi bi-info-circle" aria-hidden="true"></i>`
                + `${unmapped.length} ${unmapped.length === 1 ? 'library is' : 'libraries are'} not mapped on this node.</summary><ul>${items}</ul></details>`;
        }

        function note(html) {
            results.innerHTML = html ? `<div class="sm-hint nd-map-note">${html}</div>` : '';
        }

        function renderResults(libraries, setup) {
            lastLibraries = libraries;
            const servers = {};
            libraries.forEach((lib) => { (servers[lib.library] = servers[lib.library] || new Set()).add(lib.server); });
            repeatedNames = new Set(Object.keys(servers).filter((name) => servers[name].size > 1));
            revealEnds();
            $$('.nd-chip-slot', rows).forEach((slot) => { slot.innerHTML = ''; });
            if (!libraries.length) {
                note(`No libraries to test yet. ${noLibrariesHtml(setup)}`);
                return;
            }
            // A library this app couldn't sample itself was never asked about: not the node's problem, so they all
            // share one collapsed note instead of a chip and a red card each.
            const unmapped = libraries.filter((lib) => lib.mapped === false);
            const skipped = libraries.filter((lib) => lib.mapped !== false && !Number(lib.checked) && lib.empty !== true && !!lib.error);
            const loose = [];
            libraries.forEach((lib) => {
                if (skipped.includes(lib) || unmapped.includes(lib)) return;
                const row = rowFor(lib.app_root);
                if (row) $('.nd-chip-slot', row).insertAdjacentHTML('beforeend', libraryChip(lib));
                else loose.push(libraryChip(lib));
            });
            // A library with no video yet has nothing to test; its chip says so and it isn't a failure.
            const passed = (lib) => lib.ok || lib.empty === true || skipped.includes(lib) || unmapped.includes(lib);
            const failures = libraries.map((lib, i) => (passed(lib) ? '' : failureCard(lib, i))).join('');
            const allOk = libraries.every(passed) && skipped.length + unmapped.length < libraries.length;
            const okText = skipped.length ? 'The node sees every library it was asked about, same files.' : 'The node sees every library, same files.';
            results.innerHTML = (loose.length ? `<div class="nd-lib-chips">${loose.join('')}</div>` : '')
                + (allOk ? `<div class="sm-vmsg ok"><i class="bi bi-check-circle-fill"></i>${okText}</div>` : failures)
                + skippedNote(skipped) + unmappedNote(unmapped);
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
                renderResults(Array.isArray(r.data && r.data.libraries) ? r.data.libraries : [], r.data && r.data.media_setup);
                if (onTested) onTested();
            } finally {
                enable(btn);
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
            note,
            revealEnds,
            showError,
            applySuggestion,
            setName(name) { $('.nd-map-name', root).textContent = name || 'the node'; },
        };
    }

    // ---------- Add node -----------------------------------------------------------------------------------------

    // finished: step 2 was saved with Finish; closing it any other way still keeps its rows.
    const add = { probe: null, node: null, editor: null, finished: false };

    function probeErrorText(error) {
        const kind = (error && error.kind) || '';
        switch (kind) {
            case 'auth':
                return "Key refused. Copy the key again from the node's log (Docker) or node.json (Windows), or use the NODE_KEY (or AGENT_TOKEN) you set.";
            case 'unreachable':
                // What to check, worded by the app by why nothing answered: the node's card says the same.
                return error.advice || error.message || "Couldn't connect to the node.";
            case 'protocol':
                // A node on another protocol says which version it runs; anything else isn't a node of this app.
                if (error.node_version) return error.message;
                if (error.reason === 'this_app') {
                    return "This address is this app itself, not a node. Use the address of the machine running the node, with the node's port (9494 unless you set NODE_PORT).";
                }
                if (error.reason === 'other_app') {
                    return "This address is another copy of this app, not a node. Run that machine as a node (MODE=node) and use the node's port (9494 unless you set NODE_PORT).";
                }
                if (error.reason === 'web_page') {
                    return "This address answers with a web page, not a node. Check the address and the port: a node uses 9494 unless you set NODE_PORT.";
                }
                if (error.reason === 'not_a_node') {
                    return 'Something else answers at this address, not a node. Check the address and the port: a node uses 9494 unless you set NODE_PORT.';
                }
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
                : `${pill('warn', 'Update needed')}<span class="nd-faint">version ${esc(version || 'unknown')}; this app is ${esc(page.appVersion || 'another version')}. Run the same version on both before it gets work.</span>`)
            + '</div>');
        let found = infoRoles(info).map((role) => pill('role', role === ROLE_PLEX_DB ? 'Plex database' : 'Worker')).join('');
        if (String((info.platform || {}).os || '').toLowerCase().startsWith('win')) found += pill('off', 'Windows');
        if (info.legacy) found += pill('off', 'Older Plex database node');
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
        add.finished = false;
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
        enable(btn);
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
        if (!nameEl.value.trim()) nameEl.value = suggestedName(add.probe, url);
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

    // The node's own name (NODE_NAME), else its address's host. A container started without NODE_NAME reports its
    // hostname, which Docker sets to the container id: hex digits nobody recognises.
    function suggestedName(info, url) {
        const own = ((info && info.node) || {}).name || '';
        return own && !/^[0-9a-f]{12,64}$/.test(own) ? own : hostOf(url);
    }

    async function saveNewNode() {
        const url = $('#addNodeUrl').value.trim();
        const name = $('#addNodeName').value.trim() || suggestedName(add.probe, url);
        const body = { name, url, key: $('#addNodeKey').value.trim(), path_mappings: [] };
        const btn = $('#addNodeNext');
        btn.disabled = true;
        const r = await api('POST', '/api/nodes', body);
        enable(btn);
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
        const setup = add.probe.media_setup;
        if (!suggested.length && setup && !Number(setup.libraries)) add.editor.note(noLibrariesHtml(setup));
        setAddStep(2);
        // Step 1's field or Next button had focus and is hidden now.
        (firstField($('#addNodeStep2')) || $('#addNodeModal')).focus();
    }

    async function finishAdd() {
        const mappings = add.editor.read();
        if (mappings === null) return;
        const btn = $('#addNodeFinish');
        btn.disabled = true;
        const r = await api('PUT', `/api/nodes/${encodeURIComponent(add.node.id)}`, { path_mappings: mappings, edit_tag: add.node.edit_tag });
        enable(btn);
        const errorEl = $('#addNodeSaveError');
        if (!r.ok) {
            errorEl.textContent = errorText(r, "Couldn't save the mappings");
            errorEl.classList.remove('d-none');
            return;
        }
        errorEl.classList.add('d-none');
        add.finished = true;
        replaceNode(r.data);
        bootstrap.Modal.getOrCreateInstance($('#addNodeModal')).hide();
        if (typeof showToast === 'function') showToast('Node added', `${(r.data && r.data.name) || add.node.name} is set up.`, 'success');
    }

    // Finish later (or Esc) in step 2: the rows shown there, suggested or typed, are saved so the Media tab has them.
    async function keepStep2Rows() {
        if (add.finished || $('#addNodeStep2').classList.contains('d-none')) return;
        const rows = add.editor.read();
        if (!rows || !rows.length) return;
        const r = await api('PUT', `/api/nodes/${encodeURIComponent(add.node.id)}`, { path_mappings: rows, edit_tag: add.node.edit_tag });
        if (r.ok) replaceNode(r.data);
    }

    // ---------- node dialog --------------------------------------------------------------------------------------

    // mapDirty / setDirty: the Media rows / the Settings fields were edited and not saved; discardOk lets one close
    // through after the user said Discard. editTag: the node's edit_tag as the dialog opened it (or as its own last
    // save left it); polls don't move it, so a save over another tab's change is refused, not applied on top.
    // rescanning: a Rescan GPUs is under way, so a poll leaves its button off. healthState: the node's state when
    // Setup Health was last read. editedField: the field last typed in, where Keep editing puts the cursor back.
    const detail = {
        editor: null, healthSeq: 0, healthStartedAt: 0, mapDirty: false, setDirty: false, discardOk: false, editTag: undefined,
        rescanning: false, healthState: undefined, editedField: null,
    };

    function setMapDirty(dirty) {
        detail.mapDirty = dirty;
        if (!dirty && !detail.setDirty) hideDiscard();
    }

    function setSettingsDirty(dirty) {
        detail.setDirty = dirty;
        if (!dirty && !detail.mapDirty) hideDiscard();
    }

    function hideDiscard() {
        $('#nodeDiscardFoot').classList.add('d-none');
    }

    // Back to the field last edited (else the first field of the tab holding the edits): the hidden bar took focus
    // with it, to the page.
    function keepEditing() {
        hideDiscard();
        const pane = $(`#node-tab-${detail.mapDirty ? 'media' : 'settings'}`);
        const edited = detail.editedField;
        const field = edited && pane.contains(edited) && edited.getClientRects().length ? edited : firstField(pane);
        (field || $('#nodeModal')).focus();
    }

    // The Settings fields a Setup Health Fix names (its row's ``fix_field``).
    const FIX_FIELDS = { url: '#nodeSetUrl', key: '#nodeSetKey' };

    // A tab opened from a button on another tab: that button is hidden now, so focus goes to the field the button
    // names ("Change the key" to the key), else the tab's first field.
    function showTabFocused(tab, fieldName) {
        showTab(tab);
        const pane = $(`#node-tab-${tab}`);
        const named = FIX_FIELDS[fieldName] ? $(FIX_FIELDS[fieldName], pane) : null;
        const field = named && !named.disabled && named.getClientRects().length ? named : firstField(pane);
        (field || $('#nodeModal')).focus();
    }

    // Close, Esc and the backdrop all end in hide(): while there are edits, ask first, on the tab that holds them.
    function guardClose(event) {
        if (event.target !== $('#nodeModal') || detail.discardOk || !(detail.mapDirty || detail.setDirty)) return;
        event.preventDefault();
        showTab(detail.mapDirty ? 'media' : 'settings');
        $('#nodeDiscardText').textContent = detail.mapDirty && detail.setDirty
            ? 'Discard the unsaved mappings and settings?'
            : `Discard the unsaved ${detail.mapDirty ? 'mappings' : 'settings'}?`;
        $('#nodeDiscardFoot').classList.remove('d-none');
        $('#nodeKeepEditing').focus();
    }

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
        // A turned-off node's reason only repeats its chip: say what that means and where to undo it instead.
        const why = node.state === 'disabled'
            ? '<span class="text-muted">No work goes to it and it isn\'t checked. To use it again, turn on "Send work to this node" on its <a href="#" class="nd-to-settings">Settings tab</a>.</span>'
            : (node.reason ? `<span class="text-muted">${esc(node.reason)}</span>` : '');
        const rows = [kv('State', `${pill(tone, text)}${why}`)];
        rows.push(kv('Roles', roleChips(node) || '<span class="text-muted">none reported yet</span>'));
        if (node.version) {
            const same = !page.appVersion || node.version === page.appVersion;
            // Only a worker runs this app's code; a Plex database node shows its Plex database role's own version.
            const compared = !isWorker(node) ? ''
                : (same ? '<span class="text-muted">same as this app</span>' : `<span class="text-muted">this app is ${esc(page.appVersion)}</span>`);
            rows.push(kv('Version', `<span class="nd-mono">${esc(node.version)}</span>${compared}`));
        }
        const seen = ago(node.last_seen);
        if (seen) rows.push(kv('Last answered', esc(seen)));
        const platform = node.platform || {};
        if (platform.os) rows.push(kv('System', esc([platform.os, platform.arch].filter(Boolean).join(' · '))));
        // Load and free memory are only worth showing from a node that just answered as itself.
        const current = factsAreCurrent(node);
        if (cpu.cores) {
            const load = current && Number.isFinite(Number(cpu.load1)) && cpu.load1 !== null ? ` · load ${Number(cpu.load1).toFixed(1)}` : '';
            rows.push(kv('CPU', `${esc(cpu.cores)} cores${esc(load)}${load ? infoIcon('Load is the average number of busy cores over the last minute.') : ''}`));
        }
        if (cpu.ram_total && current) {
            rows.push(kv('Memory', esc(`${formatBytes(cpu.ram_available)} free of ${formatBytes(cpu.ram_total)}`)));
        }
        const busy = filesRunningText(node);
        if (busy) rows.push(kv('Busy', esc(busy)));
        const used = usedByParts(node.used_by);
        let useIt = '';
        if (isWorker(node)) useIt = ' · <a href="/settings#section-workers">Add it to a worker group</a>';
        else if (isPlexDb(node)) useIt = " · <a href=\"/servers\">Pick it on a Plex server's Connection tab</a>";
        rows.push(kv('Used by', used.length ? esc(used.join(', ')) : `<span class="text-muted">nothing yet</span>${useIt}`));
        redrawKeepingFocus($('#nodeOverview'), `<section class="sm-sec"><div class="sm-sec-h">Node</div><div class="markers-kv">${rows.join('')}</div></section>`);

        const gpuSection = $('#nodeGpuSection');
        gpuSection.classList.toggle('d-none', !isWorker(node));
        const list = $('#nodeGpuList');
        const items = gpus(node);
        // An old answer (or another machine's) says nothing about whether a GPU works now.
        redrawKeepingFocus(list, items.length
            ? items.map((gpu) => {
                const okGpu = !gpu.status || gpu.status === 'ok';
                const status = okGpu ? pill('ok', '✓ Working') : pill('bad', '✗ Not working');
                return `<div class="nd-gpu"><b>${esc(gpu.name || gpu.device || 'GPU')}</b>`
                    + pill('off', [gpu.type, gpu.device].filter(Boolean).join(' · '), { mono: true })
                    + (current ? status : '')
                    + (current && gpu.error ? `<span class="nd-faint">${esc(gpu.error)}</span>` : '')
                    + (current && gpu.vulkan ? `<div class="w-100">${window.MPGShared.vulkanLine(gpu.vulkan)}</div>` : '')
                    + '</div>';
            }).join('')
            : `<div class="sm-hint">No GPU found.${GIVES_WORKERS.includes(node.state) ? ' CPU work still runs on this node.' : ''}</div>`);
        // A node that isn't answering as itself can't rescan its GPUs. Focus moves to the dialog first: disabling the
        // focused button would drop it to the page, where Esc no longer closes the dialog.
        const rescan = $('#nodeRescanGpus');
        if (!detail.rescanning) {
            if (!current && document.activeElement === rescan) $('#nodeModal').focus();
            rescan.disabled = !current;
        }
    }

    // Redrawn only when it changed, with focus given back to the same link or button: a poll rewrote it every 10 s
    // under the focus, which fell to the page, where Esc no longer closes the dialog.
    function redrawKeepingFocus(el, html) {
        if (el._markup === html) return;
        const key = (node) => `${node.tagName}|${node.className}|${node.textContent}`;
        const focused = el.contains(document.activeElement) ? key(document.activeElement) : null;
        disposeTooltips(el);
        el.innerHTML = html;
        el._markup = html;
        initTooltips(el);
        if (focused === null) return;
        const same = $$('a, button', el).find((node) => key(node) === focused);
        (same || el.closest('.modal')).focus();
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
        detail.editTag = node.edit_tag;
        detail.mapDirty = detail.setDirty = detail.discardOk = false;
        detail.editedField = null;
        hideDiscard();
        renderHeader(node);
        renderOverview(node);
        renderSettings(node);
        $('#nodeTabMediaLi').classList.toggle('d-none', !isWorker(node));
        detail.editor.setName(node.name || node.id);
        detail.editor.set(node.path_mappings);
        $('#nodeMediaResult').textContent = '';
        if (isWorker(node) && !(Array.isArray(node.path_mappings) && node.path_mappings.length)) suggestMappings(node.id);
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

    // Not marked as an edit: closing without saving loses nothing, the same rows come back next time.
    async function suggestMappings(id) {
        const r = await api('GET', `/api/nodes/${encodeURIComponent(id)}/suggested-mappings`);
        if (!r.ok || !r.data || page.openId !== String(id) || detail.mapDirty) return;
        const rows = Array.isArray(r.data.path_mappings) ? r.data.path_mappings : [];
        if (rows.length) {
            detail.editor.set(rows);
            const result = $('#nodeMediaResult');
            result.className = 'sm-tres warn';
            result.textContent = 'Suggested from your libraries. Not saved yet';
        } else if (r.data.media_setup && !Number(r.data.media_setup.libraries)) {
            detail.editor.note(noLibrariesHtml(r.data.media_setup));
        }
    }

    // A poll refreshes what the open dialog shows about the node, never the fields being edited. Setup Health is
    // read again when the node's state moved (or when asked): its badge said the old count until the dialog was
    // opened again.
    function refreshOpenNode(health) {
        const node = openNodeObj();
        if (!node) {
            closeRemovedNode();
            return;
        }
        renderHeader(node);
        renderOverview(node);
        if (health || node.state !== detail.healthState) loadHealth();
    }

    // The open node was removed in another tab: nothing in the dialog can be saved any more, so it closes and says why.
    function closeRemovedNode() {
        const id = page.openId;
        if (!id) return;
        const name = $('#nodeModalName').textContent || id;
        page.openId = null;
        page.nodes = page.nodes.filter((n) => String(n.id) !== String(id));
        renderList();
        detail.discardOk = true;
        bootstrap.Modal.getOrCreateInstance($('#nodeModal')).hide();
        if (typeof showToast === 'function') showToast('Node removed', `${name} was removed in another tab.`, 'warning');
    }

    async function rescanGpus() {
        const node = openNodeObj();
        if (!node) return;
        const btn = $('#nodeRescanGpus');
        const result = $('#nodeRescanResult');
        detail.rescanning = true;
        btn.disabled = true;
        result.className = 'sm-tres';
        result.innerHTML = '<span class="sm-spin"></span>Checking the GPUs…';
        const r = await api('POST', `/api/nodes/${encodeURIComponent(node.id)}/rescan-gpus`);
        detail.rescanning = false;
        enable(btn);
        if (r.status === 404) {
            closeRemovedNode();
            return;
        }
        if (!r.ok) {
            result.className = 'sm-tres bad';
            result.textContent = errorText(r, "Couldn't rescan");
            // A poll during the rescan may have found the node gone: its button goes off again.
            refreshOpenNode();
            return;
        }
        replaceNode(r.data);
        refreshOpenNode();
        // A GPU that failed its test can't take work, so it isn't counted as found.
        const all = gpus(r.data);
        const working = all.filter((gpu) => !gpu.status || gpu.status === 'ok').length;
        const broken = all.length - working;
        result.className = `sm-tres ${working ? 'ok' : 'warn'}`;
        result.textContent = (working ? `Found ${working} working ${working === 1 ? 'GPU' : 'GPUs'}` : 'No working GPU found')
            + (broken ? ` · ${broken} not working` : '');
    }

    async function saveMappings() {
        const node = openNodeObj();
        if (!node) return;
        const mappings = detail.editor.read();
        if (mappings === null) return;
        const result = $('#nodeMediaResult');
        const btn = $('#nodeSaveMappings');
        btn.disabled = true;
        const r = await api('PUT', `/api/nodes/${encodeURIComponent(node.id)}`, { path_mappings: mappings, edit_tag: detail.editTag });
        enable(btn);
        if (r.status === 404) {
            closeRemovedNode();
            return;
        }
        if (!r.ok) {
            result.className = 'sm-tres bad';
            result.textContent = errorText(r, "Couldn't save");
            return;
        }
        detail.editTag = r.data.edit_tag;
        replaceNode(r.data);
        refreshOpenNode();
        setMapDirty(false);
        result.className = 'sm-tres ok';
        result.textContent = 'Saved';
        // What was just saved is what the node runs with now: check it sees the files there.
        detail.editor.test();
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
        // A new key or address is tried first: a key the node refuses is never saved (it used to show "Saved", then
        // "Key refused" on the next check). A node that doesn't answer may just be down, so that still saves.
        let unanswered = '';
        if (body.key !== '****' || body.url !== (node.url || '')) {
            result.className = 'sm-tres';
            result.innerHTML = '<span class="sm-spin"></span>Asking the node…';
            const probe = await api('POST', '/api/nodes/probe', { id: node.id, url: body.url, key: body.key });
            const error = probe.data && typeof probe.data.error === 'object' ? probe.data.error : null;
            if (probe.ok && probe.data && probe.data.ok === false && error && error.kind === 'auth') {
                enable(btn);
                keyEl.classList.add('is-invalid');
                result.className = 'sm-tres bad';
                result.textContent = `${probeErrorText(error)} Nothing was saved.`;
                return;
            }
            if (!probe.ok) {
                enable(btn);
                result.className = 'sm-tres bad';
                result.textContent = errorText(probe, "Couldn't check the key");
                return;
            }
            if (probe.data && probe.data.ok === false) unanswered = probeErrorText(error || {});
        }
        const r = await api('PUT', `/api/nodes/${encodeURIComponent(node.id)}`, { ...body, edit_tag: detail.editTag });
        enable(btn);
        if (r.status === 404) {
            closeRemovedNode();
            return;
        }
        if (!r.ok) {
            result.className = 'sm-tres bad';
            result.textContent = errorText(r, "Couldn't save");
            return;
        }
        detail.editTag = r.data.edit_tag;
        replaceNode(r.data);
        // A new key or address ends or starts a Must fix.
        refreshOpenNode(true);
        detail.editor.setName(r.data.name || r.data.id);
        keyEl.value = '****';
        setSettingsDirty(false);
        result.className = unanswered ? 'sm-tres warn' : 'sm-tres ok';
        result.textContent = unanswered ? `Saved. ${unanswered}` : 'Saved';
    }

    async function removeNode() {
        const node = openNodeObj();
        if (!node) return;
        const out = $('#nodeRemoveResult');
        out.classList.add('d-none');
        // A node still in use can't be removed: say by what before asking, not after.
        const inUse = usedByParts(node.used_by);
        if (inUse.length) {
            out.textContent = `Still in use by ${inUse.join(', ')}. Take it out of those first.`;
            out.classList.remove('d-none');
            return;
        }
        const ok = await window.appConfirm(
            `Remove node "${node.name || node.id}"? The node keeps running; this app stops using it.`,
            { title: 'Remove node', confirmText: 'Remove' },
        );
        if (!ok) return;
        const r = await api('DELETE', `/api/nodes/${encodeURIComponent(node.id)}`);
        if (r.status === 404) {
            closeRemovedNode();
            return;
        }
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
        // Cleared first: a poll landing while the dialog fades out would read this as removed in another tab.
        page.openId = null;
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
            : String((r.data && r.data.message) || 'No log lines yet.');
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
    // A failing informational row is a note about something other than the node (libraries this app can't open):
    // one neutral line above the buckets.
    function partitionChecks(sections) {
        const mustFix = [];
        const recommended = [];
        const allGood = [];
        const notes = [];
        (sections || []).forEach((section) => {
            (section.checks || []).forEach((check) => {
                if ((check.severity || 'info') === 'info' && check.informational !== true) return;
                const item = { check, sectionId: section.id || '', sectionTitle: section.title || section.id || '' };
                if (check.ok === false && (check.severity || 'info') === 'info') notes.push(item);
                else if (check.ok === false && check.severity === 'critical') mustFix.push(item);
                else if (check.ok === false && check.severity === 'recommended' && check.dismissed !== true) recommended.push(item);
                else allGood.push(item);
            });
        });
        return { mustFix, recommended, allGood, notes };
    }

    function noteLine(check) {
        const link = typeof check.help_url === 'string' && check.help_url.startsWith('/')
            ? ` <a href="${esc(check.help_url)}">${esc(check.help_label || 'Open')}</a>` : '';
        const title = check.reason ? ` title="${esc(check.reason)}"` : '';
        return `<div class="nd-note"${title}><i class="bi bi-info-circle" aria-hidden="true"></i><span>${esc(check.label)}.${link}</span></div>`;
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
                showTabFocused(check.fix_tab, check.fix_field);
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
        if (!ok && typeof check.docs_url === 'string' && check.docs_url.startsWith('https://')) {
            const link = document.createElement('a');
            link.className = 'btn btn-sm btn-outline-secondary nd-health-docs';
            link.href = check.docs_url;
            link.target = '_blank';
            link.rel = 'noopener';
            link.innerHTML = `${esc(check.help_label || 'How to fix this')} <i class="bi bi-box-arrow-up-right" aria-hidden="true"></i>`;
            acts.appendChild(link);
        }
        return row;
    }

    function bucket(tier, title, items, expanded, iconHtml) {
        const det = document.createElement('details');
        det.className = 'readiness-bucket';
        det.dataset.tier = tier;
        // Bootstrap's focus trap counts <details> as focusable but focusing one does nothing: as the dialog's last
        // such element, a Shift+Tab out of the dialog stayed on the page behind it. Its <summary> stays tabbable.
        det.tabIndex = -1;
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
        // Only notes (a turned-off node): no verdict, the note says it.
        if (!summary.length && partition.allGood.length) summary.push(pill('ok', '✓ All good'));
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
        if (partition.notes.length) body.insertAdjacentHTML('afterbegin', partition.notes.map((item) => noteLine(item.check)).join(''));
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
        detail.healthState = node.state;
        const body = $('#nodeHealthBody');
        // Its rows are about to be replaced: a focused Fix or Retry would drop focus to the page.
        if (body.contains(document.activeElement)) $('#nodeModal').focus();
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
        // The Settings phone picker: every entry but Nodes is a section of the Settings page.
        const settingsPicker = $('#settingsMobileSection');
        if (settingsPicker) settingsPicker.addEventListener('change', () => { window.location.href = settingsPicker.value; });
        add.editor = createMappingEditor($('#addNodeMappings'), { getNodeId: async () => add.node && add.node.id });
        detail.editor = createMappingEditor($('#nodeMediaEditor'), {
            getNodeId: async () => page.openId,
            onChange: () => {
                setMapDirty(true);
                const result = $('#nodeMediaResult');
                result.className = 'sm-tres warn';
                result.textContent = 'Not saved yet';
            },
            // A Test access of the saved rows ends or starts an access problem: Setup Health and its badge said the
            // old count until the dialog was opened again.
            onTested: loadHealth,
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
        // The name is optional (the node's own is used), so typing starts at the address.
        $('#addNodeModal').addEventListener('shown.bs.modal', () => {
            if (!$('#addNodeStep1').classList.contains('d-none')) $('#addNodeUrl').focus();
        });
        // Finish later, Esc and the backdrop save the rows shown (keepStep2Rows). A row with one side filled in is
        // refused as Save refuses it ("Fill in both paths, or remove the row."): closing on it saved no row at all.
        $('#addNodeModal').addEventListener('hide.bs.modal', (event) => {
            if (event.target !== $('#addNodeModal') || add.finished || !add.node) return;
            if ($('#addNodeStep2').classList.contains('d-none')) return;
            if (add.editor.read() === null) event.preventDefault();
        });
        $('#addNodeModal').addEventListener('hidden.bs.modal', async () => {
            if (window.location.hash === '#add') history.replaceState(null, '', window.location.pathname);
            // Opened from code, the dialog gives focus back to nothing: the page's Add node button takes it.
            $('#nodesAddBtn').focus();
            if (!add.node) return;
            await keepStep2Rows();
            loadNodes();
        });

        $('#nodeModal').addEventListener('hide.bs.modal', guardClose);
        $('#nodeKeepEditing').addEventListener('click', keepEditing);
        $('#nodeModal').addEventListener('input', (event) => { detail.editedField = event.target; });
        $('#nodeDiscardConfirm').addEventListener('click', () => {
            detail.discardOk = true;
            bootstrap.Modal.getOrCreateInstance($('#nodeModal')).hide();
        });
        ['input', 'change'].forEach((type) => $('#node-tab-settings').addEventListener(type, (event) => {
            if (event.target.matches('#nodeSetName, #nodeSetUrl, #nodeSetKey, #nodeSetEnabled')) setSettingsDirty(true);
        }));
        $('#nodeModal').addEventListener('hidden.bs.modal', () => {
            const closed = page.openId;
            detail.mapDirty = detail.setDirty = detail.discardOk = false;
            hideDiscard();
            page.openId = null;
            if (window.location.hash.startsWith('#node-')) history.replaceState(null, '', window.location.pathname);
            // Opened from code, the dialog gives focus back to nothing: back to the node's own button (found by id, as
            // a poll may have redrawn its row), or Add node once the node was removed.
            const back = closed && document.querySelector(`#node-${CSS.escape(closed)} .nd-open`);
            (back || $('#nodesAddBtn')).focus();
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
        $('#nodeOverview').addEventListener('click', (event) => {
            if (!event.target.closest('.nd-to-settings')) return;
            event.preventDefault();
            showTab('settings');
            $('#nodeSetEnabled').focus();
        });

        window.addEventListener('hashchange', openFromHash);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) loadNodes(); });
        loadNodes();
        startPolling();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();
})();
