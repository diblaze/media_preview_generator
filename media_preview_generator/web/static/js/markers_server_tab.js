// Servers → Edit → "Intro & Credits" tab: this server's switch, capability status, the Plex database-write
// confirmation and what happens when Plex or Emby has markers of its own. Also the Libraries tab's Intro & Credits
// column (markers.library_ids), shown while the switch is on. servers.js calls the window globals exported at the bottom.

(function () {
    'use strict';

    // Jellyfin and Emby restart after a plugin install, so the status is checked again once it's likely back.
    const INSTALL_RECHECK_MS = 20000;
    // Mirrors markers.settings.is_sports_library so the column shows before the status check returns; the status
    // response's default_selected replaces it when it arrives.
    const SPORTS_NAME_RE = /\bsports?\b/i;
    const EMBY_MANUAL_GUIDE_URL = 'https://github.com/stevezau/media_preview_generator/blob/main/docs/guides.md#emby-the-media-preview-bridge-for-emby-plugin';
    const RESTARTING = { jellyfin: 'Jellyfin', emby: 'Emby' };
    const EMBY_NO_PREMIERE_NOTE = "Viewers can't skip intros: this Emby server has no Emby Premiere key. Skip Credits (Up Next) still works.";

    const $ = (sel, el) => (el || document).querySelector(sel);
    const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

    const tab = {
        server: null,
        status: null,
        loadSeq: 0,
        // Library id → the Intro & Credits switch as the user last left it; empty until they flip one.
        libraryChoices: new Map(),
        // Library id → default_selected from the status check (null until it answers).
        statusDefaults: null,
        pendingConfirmation: null,
        // Nodes with the Plex database role, from /api/nodes (null until it answers).
        plexDbNodes: null,
        // Every node from /api/nodes, to name a saved choice whose node hasn't reported its roles yet.
        allNodes: [],
    };

    function vendorOf(server) {
        return String((server && server.type) || '').toLowerCase();
    }

    function isPlex(server) {
        return vendorOf(server) === 'plex';
    }

    function storedMarkers(server) {
        const markers = server && server.markers;
        return markers && typeof markers === 'object' ? markers : {};
    }

    function storedPlex(server) {
        const plex = storedMarkers(server).plex;
        return plex && typeof plex === 'object' ? plex : {};
    }

    function storedEmby(server) {
        const emby = storedMarkers(server).emby;
        return emby && typeof emby === 'object' ? emby : {};
    }

    // How this server reaches Plex's database, as stored: {enabled, node_id}. enabled false = this app opens it;
    // true = through the Plex database node node_id (the node record holds the address and key).
    function storedAgent(server) {
        const agent = storedPlex(server).agent;
        return agent && typeof agent === 'object' ? agent : {};
    }

    // The select's values besides a node id: this app, and the entry that opens the Nodes page.
    const DB_LOCAL = '';
    // Switched on with no node behind it (a helper the upgrade couldn't move to the Nodes page): kept as it is on
    // save, and refused with "no node" until a node is chosen, so this app never opens its own copy instead.
    const DB_OLD_HELPER = '__old_helper__';

    function storedDbChoice(server) {
        const agent = storedAgent(server);
        if (!agent.enabled) return DB_LOCAL;
        return agent.node_id ? String(agent.node_id) : DB_OLD_HELPER;
    }

    function oldHelperLabel(server) {
        const url = storedAgent(server).url;
        return url ? `Through the Plex database node at ${url}` : "Through a Plex database node that isn't on the Nodes page";
    }

    // A node's roles are known once it has answered; until then (just started, or turned off) it may be one.
    function isPlexDbNode(node) {
        if (!node) return false;
        const roles = Array.isArray(node.roles) ? node.roles : [];
        return !roles.length || roles.includes('plex_db') || !!node.legacy;
    }

    function dbNodeOptionText(name, node) {
        return `Through a Plex database node: ${name}${node && node.enabled === false ? ' · turned off' : ''}`;
    }

    // ---------- status block ---------------------------------------------------

    // `detailHtml` (authored literal HTML) makes a click open it in the info modal; _initBootstrapTooltips (below, on
    // render) adds "Click for more." and the pointer, as for every other ⓘ.
    function infoIcon(title, detailHtml) {
        const detail = detailHtml ? ` data-explain-html="${escapeHtml(detailHtml)}"` : '';
        return `<button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="${escapeHtml(title)}"${detail}><i class="bi bi-info-circle"></i></button>`;
    }

    function badge(tone, text, extraClass) {
        const tones = {
            ok: 'bg-success-subtle text-success-emphasis border border-success-subtle',
            warn: 'bg-warning-subtle text-warning-emphasis border border-warning-subtle',
            bad: 'bg-danger-subtle text-danger-emphasis border border-danger-subtle',
            off: 'bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle',
        };
        return `<span class="badge ${tones[tone]}${extraClass ? ` ${extraClass}` : ''}">${escapeHtml(text)}</span>`;
    }

    function kvRow(label, valueHtml) {
        return `<div class="markers-kv-label">${escapeHtml(label)}</div><div>${valueHtml}</div>`;
    }

    function kvGrid(rows) {
        return `<div class="markers-kv small">${rows.join('')}</div>`;
    }

    // The same-host-path advice may already be in the message; the hint is only added when it isn't.
    const SAME_HOST_ADVICE_RE = /same[ -]host path|unraid/i;

    function warningLine(capability) {
        if (!capability.message) return '';
        const rawHint = (capability.details || {}).hint;
        const hint = rawHint && !SAME_HOST_ADVICE_RE.test(capability.message) ? rawHint : '';
        return `<div class="alert alert-warning small text-break py-2 mb-0 mt-3" role="status">
            <i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml(capability.message)}
            ${hint ? `<div class="mt-1">${escapeHtml(hint)}</div>` : ''}
        </div>`;
    }

    function canShowText(types) {
        const words = (types || []).map(String);
        if (!words.length) return '—';
        words[0] = words[0].charAt(0).toUpperCase() + words[0].slice(1);
        return words.join(' · ');
    }

    // True/false when Plex reported its detection prefs; null when it couldn't be read (row left out).
    function plexDetectionOn(detection) {
        if (!detection || typeof detection !== 'object') return null;
        const values = [detection.intro, detection.credits];
        if (values.every((v) => v == null)) return null;
        return values.some((v) => v != null && v !== 'never');
    }

    // The status payload has no "local" flag. For needs_local_db: lock_holder is absent (network/unrecognised
    // filesystem) or anything but false → network share; lock_holder === false → local disk, wrong path.
    function plexDbOnNetworkShare(capability) {
        const details = capability.details || {};
        return capability.state === 'needs_local_db' && details.lock_holder !== false;
    }

    function dirname(path) {
        const text = String(path || '');
        const cut = text.lastIndexOf('/');
        return cut > 0 ? text.slice(0, cut) : text;
    }

    // The agent block of the capability details (url, version, state, which Plex it serves): only there
    // when an agent is configured, so its presence is what switches this tab's Plex rows to the agent wording.
    function agentDetails(capability) {
        const agent = (capability.details || {}).agent;
        return agent && typeof agent === 'object' ? agent : null;
    }

    const AGENT_BADGES = {
        connected: ['ok', '✓ Connected'],
        unreachable: ['bad', "Can't reach it"],
        rejected: ['bad', 'Key refused'],
        incompatible: ['warn', 'Update needed'],
        disabled: ['warn', 'Turned off'],
        no_role: ['bad', 'Role off'],
        no_node: ['bad', 'No node'],
        unconfirmed_node: ['bad', 'Not confirmed'],
    };

    function renderPlexStatus(status) {
        const capability = status.capability || {};
        const details = capability.details || {};
        const agent = agentDetails(capability);
        const rows = [
            agent
                ? kvRow(
                    'How markers get here',
                    `Written into this Plex server's database, through the Plex database node`
                        + infoIcon('The Plex database node does the database write on the Plex machine. This app never touches the file itself.'),
                )
                : kvRow(
                    'How markers get here',
                    `Written into this Plex server's database${infoIcon('Plex has no API or plugins for this. Only on the same machine as Plex.')}`,
                ),
        ];
        if (details.plex_pass === true) rows.push(kvRow('Plex Pass', badge('ok', '✓ Active')));
        if (details.plex_pass === false) rows.push(kvRow('Plex Pass', badge('bad', '✕ Not active')));
        if (details.db_path) {
            const disk = plexDbOnNetworkShare(capability)
                ? badge('bad', `✕ network share${details.fs_type ? ` (${details.fs_type})` : ''}`)
                : badge('ok', '✓ local disk');
            const whereTip = agent
                ? 'The folder of Plex\'s library database, as the Plex database node on the Plex machine sees it.'
                : 'The folder of Plex\'s library database, as this app sees it.';
            const whereDetail = agent
                ? '<p>Markers are written straight into this database, so it has to be on a local disk of that machine.</p>'
                : '<p>Markers are written straight into this database, so it has to be on a local disk of the machine Plex runs on: a database on a network share can\'t be written safely.</p>';
            rows.push(kvRow(
                agent ? 'Database location (on the Plex machine)' : 'Database location',
                `<span class="font-monospace text-break me-1">${escapeHtml(dirname(details.db_path))}</span>${disk}${infoIcon(whereTip, whereDetail)}`,
            ));
        }
        const detectionOn = plexDetectionOn(details.detection);
        const detectionTip = infoIcon(
            'Whether Plex finds intros and credits itself.',
            '<p>The setting is in Plex settings → Library → <em>Generate intro / credits video markers</em>.</p>'
                + '<p>When on, Plex can analyse a file again and replace our markers; "When Plex has its own markers" below '
                + 'decides what happens then.</p>',
        );
        if (detectionOn === true) {
            const keepsPlex = ((status.settings || {}).plex || {}).on_plex_redetect === 'keep_plex';
            const outcome = keepsPlex ? "it can replace ours; Plex's are kept" : 'it can replace ours; we put them back';
            rows.push(kvRow(
                "Plex's own detection",
                `${badge('warn', 'On', 'markers-detection')} <span class="text-muted">${escapeHtml(outcome)}</span>${detectionTip}`,
            ));
        } else if (detectionOn === false) {
            rows.push(kvRow("Plex's own detection", badge('off', 'Off', 'markers-detection') + detectionTip));
        }
        // A ready Plex can still carry a warning: Plex Pass couldn't be checked, so jobs wait instead of writing.
        const warning = capability.state === 'ready'
            ? warningLine({ message: capability.warning })
            : warningLine(capability);
        return kvGrid(rows) + warning;
    }

    function installButton(label) {
        return `<button type="button" class="btn btn-sm btn-primary ms-2 py-0" id="markersInstallPluginBtn">${escapeHtml(label)}</button>
            <span id="markersInstallResult" class="small ms-2"></span>`;
    }

    // The plugin reports its version but no minimum exists: what's missing is its markers feature, so the row names
    // the installed version and nothing it would have to invent.
    function outdatedPluginRow(details) {
        const installed = details.plugin_version ? `installed ${details.plugin_version}` : 'installed version unknown';
        return kvRow('Plugin', `${badge('warn', 'Update needed')} <span class="text-muted markers-plugin-installed">— ${escapeHtml(installed)}</span>`
            + infoIcon('This version of the plugin can\'t take intro and credits markers. Update installs the newest version.')
            + installButton('Update'));
    }

    function renderJellyfinStatus(status) {
        const capability = status.capability || {};
        const details = capability.details || {};
        const rows = [kvRow('How markers get here', 'Media Preview Bridge plugin')];
        const pluginStates = ['ready', 'plugin_outdated', 'needs_plugin'];
        if (capability.state === 'ready') {
            rows.push(kvRow('Plugin', badge('ok', details.plugin_version ? `${details.plugin_version} ✓` : 'Installed ✓')));
        } else if (capability.state === 'plugin_outdated') {
            rows.push(outdatedPluginRow(details));
        } else if (capability.state === 'needs_plugin') {
            rows.push(kvRow('Plugin', badge('bad', 'Not installed') + installButton('Install')));
        }
        rows.push(kvRow('Can show', escapeHtml(canShowText(status.can_show))));
        return kvGrid(rows) + (pluginStates.includes(capability.state) ? '' : warningLine(capability));
    }

    function renderEmbyStatus(status) {
        const capability = status.capability || {};
        const details = capability.details || {};
        const rows = [kvRow('How markers get here', 'Media Preview Bridge for Emby plugin')];
        const pluginStates = ['ready', 'plugin_outdated', 'needs_plugin'];
        if (capability.state === 'ready') {
            rows.push(kvRow('Plugin', badge('ok', details.plugin_version ? `${details.plugin_version} ✓` : 'Installed ✓')));
        } else if (capability.state === 'plugin_outdated') {
            rows.push(outdatedPluginRow(details));
        } else if (capability.state === 'needs_plugin') {
            // catalog_listed is false (not in Emby's catalog) or null (catalog couldn't be read, e.g. Emby
            // unreachable for that one call): either way there's no Install button to offer, only the manual guide.
            const action = details.catalog_listed === true
                ? installButton('Install')
                : ` · <a class="markers-manual-install" href="${EMBY_MANUAL_GUIDE_URL}" target="_blank" rel="noopener">Install by hand</a>`;
            rows.push(kvRow('Plugin', badge('bad', 'Not installed') + action));
        }
        // Owner decision R1: Emby always gets the decided credits start (even one that ends before the file does) —
        // "Can show" never says otherwise; the info icon explains why viewers still see the whole file after it.
        rows.push(kvRow('Can show', 'Intro · credits start'
            + infoIcon('Emby has no credits end: Skip Credits always skips to the end of the file, past any scene after the credits.')));
        // Only a confirmed "no key": null (Emby couldn't be asked, or has no such route) shows nothing.
        if (details.intro_skip_registered === false) {
            rows.push(kvRow('Emby Premiere', badge('warn', EMBY_NO_PREMIERE_NOTE, 'text-wrap text-start lh-base markers-premiere-note')
                + infoIcon('Emby only lets viewers skip intros on servers with Emby Premiere.')));
        }
        return kvGrid(rows) + (pluginStates.includes(capability.state) ? '' : warningLine(capability));
    }

    function dbChoiceValue() {
        const select = $('#markersPlexDatabase');
        return select ? select.value : DB_LOCAL;
    }

    function dbChoiceLabel(value) {
        const node = (tab.plexDbNodes || []).find((n) => String(n.id) === String(value));
        if (node) return node.name || String(node.id);
        if (value === DB_OLD_HELPER) return storedAgent(tab.server).url || 'Plex database node';
        return String(value || '');
    }

    // The status line under the selector: what the last check of the saved route found, and a way to ask again.
    // Nothing shows while this app opens the database itself.
    function renderAgentState(status) {
        const line = $('#markersAgentState');
        if (!line) return;
        const choice = dbChoiceValue();
        line.classList.toggle('d-none', choice === DB_LOCAL);
        if (choice === DB_LOCAL) {
            line.innerHTML = '';
            return;
        }
        const check = '<button type="button" class="btn btn-sm btn-outline-secondary ms-2 py-0" id="markersAgentCheckBtn">Check again</button>';
        const name = escapeHtml(dbChoiceLabel(choice));
        if (tab.server && choice !== storedDbChoice(tab.server)) {
            line.innerHTML = `${badge('off', 'Not set up')} <span class="text-muted">Save to use ${name}, then check.</span>`;
            return;
        }
        const agent = status ? agentDetails(status.capability || {}) : null;
        if (!status) {
            line.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Checking…';
            return;
        }
        if (!agent) {
            line.innerHTML = `${badge('off', 'Not set up')} <span class="text-muted">${name} hasn't been checked yet.</span>${check}`;
            return;
        }
        const [tone, text] = AGENT_BADGES[agent.state] || AGENT_BADGES.unreachable;
        const version = agent.version ? ` · v${escapeHtml(agent.version)}` : '';
        const note = agent.state === 'unreachable'
            ? `<div class="text-muted mt-1">Markers wait here until ${name} answers again. Nothing is lost.</div>`
            : '';
        line.innerHTML = `${badge(tone, text)} <span class="font-monospace text-break">${name}${version}</span>${check}${note}`;
    }

    function dbOption(value, text) {
        return `<option value="${escapeHtml(value)}">${escapeHtml(text)}</option>`;
    }

    // "Open it from this app", each Plex database node and the stored choice even when its node is gone. Keeps
    // whatever is selected now. Adding a node is the link beside it: as an entry here, arrowing through the list
    // opened the Nodes page on every key press that reached it.
    function renderDbOptions(server) {
        const select = $('#markersPlexDatabase');
        if (!select) return;
        const current = select.dataset.loaded === '1' ? select.value : storedDbChoice(server);
        const nodes = tab.plexDbNodes || [];
        let html = dbOption(DB_LOCAL, 'Open it from this app');
        nodes.forEach((node) => {
            html += dbOption(String(node.id), dbNodeOptionText(node.name || node.id, node));
        });
        const stored = storedDbChoice(server);
        if (stored === DB_OLD_HELPER) {
            html += dbOption(DB_OLD_HELPER, oldHelperLabel(server));
        } else if (stored !== DB_LOCAL && !nodes.some((n) => String(n.id) === stored)) {
            // Kept selectable so a save doesn't change it: the node list hasn't answered yet, the node no longer
            // reports the Plex database role, or it was removed.
            const known = (tab.allNodes || []).find((n) => String(n.id) === stored);
            const label = known ? (known.name || stored)
                : (tab.plexDbNodes === null ? stored : `${stored} (not on the Nodes page any more)`);
            html += dbOption(stored, dbNodeOptionText(label, known));
        }
        select.innerHTML = html;
        select.value = current;
        if (select.value !== current) select.value = stored;
        select.dataset.loaded = '1';
    }

    async function fetchPlexDbNodes(server) {
        const seq = tab.loadSeq;
        let all = [];
        try {
            const r = await fetch('/api/nodes');
            if (r.ok) {
                const data = await r.json();
                all = Array.isArray(data && data.nodes) ? data.nodes : [];
            }
        } catch (_e) {
            all = [];
        }
        if (seq !== tab.loadSeq) return;
        tab.allNodes = all;
        tab.plexDbNodes = all.filter(isPlexDbNode);
        renderDbOptions(server);
        renderAgentState(tab.status);
    }

    function onDbChoiceChange() {
        renderAgentState(tab.status);
    }

    function renderStatus(server, status) {
        const block = $('#markersStatusBlock');
        if (!block) return;
        const vendor = vendorOf(server);
        if (vendor === 'plex') block.innerHTML = renderPlexStatus(status);
        else if (vendor === 'jellyfin') block.innerHTML = renderJellyfinStatus(status);
        else if (vendor === 'emby') block.innerHTML = renderEmbyStatus(status);
        else block.innerHTML = warningLine(status.capability || {});
        if (typeof window._initBootstrapTooltips === 'function') window._initBootstrapTooltips($('#edit-tab-markers'));
    }

    function renderStatusError() {
        const block = $('#markersStatusBlock');
        if (!block) return;
        block.innerHTML = `<div class="small text-muted"><i class="bi bi-exclamation-circle me-1"></i>Couldn't check this server right now · <a href="#" class="markers-status-retry">Retry</a></div>`;
    }

    // refresh=1 drops the few-seconds-old cached answer first: what "Check again" is for.
    async function fetchStatus(server, { refresh = false } = {}) {
        const seq = tab.loadSeq;
        const block = $('#markersStatusBlock');
        if (block) block.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Checking…</div>';
        let data = null;
        try {
            const query = refresh ? '?refresh=1' : '';
            const r = await fetch(`/api/markers/servers/${encodeURIComponent(server.id)}/status${query}`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            data = await r.json();
        } catch (_e) {
            if (seq === tab.loadSeq) renderStatusError();
            return;
        }
        // The user may have opened another server while this was loading.
        if (seq !== tab.loadSeq) return;
        tab.status = data;
        renderStatus(server, data);
        if (isPlex(server)) renderAgentState(data);
        if (Array.isArray(data.libraries)) {
            tab.statusDefaults = new Map(data.libraries
                .filter((lib) => typeof lib.default_selected === 'boolean')
                .map((lib) => [String(lib.id), lib.default_selected]));
            if (!librariesTouched()) renderLibraryColumn();
        }
    }

    async function installPlugin(button) {
        const server = tab.server;
        if (!server) return;
        const seq = tab.loadSeq;
        const result = $('#markersInstallResult');
        const label = button.textContent;
        button.disabled = true;
        button.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${escapeHtml(label)}`;
        let data = null;
        let httpStatus = 0;
        try {
            const r = await fetch(`/api/servers/${encodeURIComponent(server.id)}/install-plugin`, {
                method: 'POST',
            });
            httpStatus = r.status;
            data = await r.json();
        } catch (_e) {
            data = null;
        }
        if (seq !== tab.loadSeq) return;
        if (!data || !data.ok) {
            button.disabled = false;
            button.textContent = label;
            if (result) {
                result.className = 'small ms-2 text-danger';
                const primary = (data && (data.error || data.message)) || `Install failed (HTTP ${httpStatus || '?'})`;
                if (data && data.manual) {
                    // Catalog doesn't list the plugin: point at the same manual-install guide the "Not installed"
                    // row links to.
                    result.innerHTML = `${escapeHtml(primary)}`
                        + ` · <a class="markers-manual-install" href="${EMBY_MANUAL_GUIDE_URL}" target="_blank" rel="noopener">Install by hand</a>`;
                } else {
                    result.textContent = primary;
                }
            }
            return;
        }
        if (result) {
            const restarting = RESTARTING[vendorOf(server)] || 'The server';
            result.className = 'small ms-2 text-muted';
            result.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${escapeHtml(restarting)} is restarting — checking again in 20 s…`;
        }
        setTimeout(() => {
            if (seq === tab.loadSeq && tab.server) fetchStatus(tab.server);
        }, INSTALL_RECHECK_MS);
    }

    // ---------- libraries ------------------------------------------------------

    function libraryDomId(libraryId) {
        return `markersLib-${String(libraryId).replace(/[^A-Za-z0-9_-]/g, '_')}`;
    }

    function librariesTouched() {
        return tab.libraryChoices.size > 0;
    }

    function selectedByDefault(id, name, kind) {
        if (tab.statusDefaults && tab.statusDefaults.has(id)) return tab.statusDefaults.get(id);
        return !(SPORTS_NAME_RE.test(name || '') || ['sport', 'sports'].includes(String(kind || '').toLowerCase()));
    }

    // Shows the Libraries tab's Intro & Credits column only while this server's switch is on.
    function syncLibraryColumn() {
        const on = !!tab.server && !!($('#markersEnabled') || {}).checked;
        $$('#editLibraryTable .markers-lib-col').forEach((el) => el.classList.toggle('d-none', !on));
    }

    // Fills the Intro & Credits cell of every row servers.js rendered. A switch the user flipped keeps its state
    // across a re-render (Refresh libraries); every other one shows the stored choice, or the default when none.
    function renderLibraryColumn() {
        const stored = storedMarkers(tab.server).library_ids;
        const chosen = Array.isArray(stored) ? stored.map(String) : null;
        $$('#editLibraryList tr[data-lib-id]').forEach((row) => {
            const cell = row.querySelector('.markers-lib-cell');
            if (!cell) return;
            const id = String(row.dataset.libId);
            const byDefault = selectedByDefault(id, row.dataset.libName, row.dataset.libKind);
            const checked = tab.libraryChoices.has(id)
                ? tab.libraryChoices.get(id)
                : (chosen === null ? byDefault : chosen.includes(id));
            const label = `Intro & Credits for ${row.dataset.libName || id}`;
            cell.innerHTML = `<div class="form-check form-switch edit-lib-switch">`
                + `<input type="checkbox" role="switch" class="form-check-input markers-lib-toggle" id="${escapeHtml(libraryDomId(id))}" data-id="${escapeHtml(id)}" data-default="${byDefault ? '1' : '0'}" aria-label="${escapeHtml(label)}"${checked ? ' checked' : ''}>`
                + '</div>';
        });
        syncLibraryColumn();
    }

    function readLibraryIds(server) {
        const stored = storedMarkers(server).library_ids;
        const storedIds = Array.isArray(stored) ? stored.map(String) : null;
        const toggles = $$('#editLibraryList .markers-lib-toggle');
        // Untouched switches keep the stored choice exactly (an explicit list equal to the defaults stays a list).
        if (!librariesTouched() || !toggles.length) return storedIds;
        const ticked = toggles.filter((el) => el.checked).map((el) => el.dataset.id);
        const defaults = toggles.filter((el) => el.dataset.default === '1').map((el) => el.dataset.id);
        const sameAsDefault = ticked.length === defaults.length && ticked.every((id) => defaults.includes(id));
        return sameAsDefault ? null : ticked;
    }

    // ---------- public surface for servers.js ------------------------------------

    function loadMarkersTab(server) {
        tab.server = server;
        tab.status = null;
        tab.libraryChoices = new Map();
        tab.statusDefaults = null;
        tab.loadSeq += 1;
        const markers = storedMarkers(server);
        const toggle = $('#markersEnabled');
        if (toggle) toggle.checked = !!markers.enabled;

        const plex = isPlex(server);
        const redetectGroup = $('#markersPlexRedetectGroup');
        if (redetectGroup) redetectGroup.classList.toggle('d-none', !plex);
        const keepPlex = plex && storedPlex(server).on_plex_redetect === 'keep_plex';
        const restoreRadio = $('#markersRedetectRestore');
        const keepRadio = $('#markersRedetectKeep');
        if (restoreRadio) restoreRadio.checked = !keepPlex;
        if (keepRadio) keepRadio.checked = keepPlex;

        const agentGroup = $('#markersPlexAgentGroup');
        if (agentGroup) agentGroup.classList.toggle('d-none', !plex);
        tab.plexDbNodes = null;
        tab.allNodes = [];
        const dbSelect = $('#markersPlexDatabase');
        if (dbSelect) delete dbSelect.dataset.loaded;
        if (plex) {
            renderDbOptions(server);
            renderAgentState(null);
            fetchPlexDbNodes(server);
        }

        const emby = vendorOf(server) === 'emby';
        const embyGroup = $('#markersEmbyRedetectGroup');
        if (embyGroup) embyGroup.classList.toggle('d-none', !emby);
        const keepEmby = emby && storedEmby(server).on_emby_redetect === 'keep_emby';
        const embyRestoreRadio = $('#markersEmbyRedetectRestore');
        const embyKeepRadio = $('#markersEmbyRedetectKeep');
        if (embyRestoreRadio) embyRestoreRadio.checked = !keepEmby;
        if (embyKeepRadio) embyKeepRadio.checked = keepEmby;

        renderLibraryColumn();
        fetchStatus(server);
    }

    function readMarkersFromForm(server) {
        // The form only describes the server it was loaded for; anything else keeps its stored block untouched.
        if (!tab.server || !server || tab.server.id !== server.id) return { ...storedMarkers(server) };
        const out = {
            enabled: !!($('#markersEnabled') || {}).checked,
            library_ids: readLibraryIds(server),
        };
        if (isPlex(server)) {
            const picked = document.querySelector('input[name="markersPlexRedetect"]:checked');
            out.plex = {
                db_write_confirmed_at: storedPlex(server).db_write_confirmed_at || server._markersConfirmedAt || null,
                on_plex_redetect: picked ? picked.value : (storedPlex(server).on_plex_redetect || 'restore'),
            };
            const choice = dbChoiceValue();
            if (choice === DB_OLD_HELPER) {
                out.plex.agent = { ...storedAgent(server) };
            } else if (Object.keys(storedAgent(server)).length || choice !== DB_LOCAL) {
                // A server that never had a route set and still opens the database itself posts no agent block, so a
                // save that only changed something else doesn't write a field the user never touched. One left on this
                // app keeps the node it remembers for when it is switched back (a helper the upgrade found off).
                const remembered = storedDbChoice(server) === DB_LOCAL ? storedAgent(server).node_id || null : null;
                out.plex.agent = choice === DB_LOCAL
                    ? { enabled: false, node_id: remembered }
                    : { enabled: true, node_id: choice };
            }
        }
        if (vendorOf(server) === 'emby') {
            const picked = document.querySelector('input[name="markersEmbyRedetect"]:checked');
            out.emby = { on_emby_redetect: picked ? picked.value : (storedEmby(server).on_emby_redetect || 'restore') };
        }
        return out;
    }

    function markersNeedsPlexConfirmation(server) {
        return isPlex(server)
            && !!($('#markersEnabled') || {}).checked
            && !storedPlex(server).db_write_confirmed_at
            && !server._markersConfirmedAt;
    }

    function confirmLocalText() {
        const capability = (tab.status && tab.status.capability) || {};
        if (!(capability.details || {}).db_path) return '';
        return plexDbOnNetworkShare(capability)
            ? 'Checked: network share ✕ — writes will stay off'
            : 'Checked: local disk ✓';
    }

    const CONFIRM_SAME_MACHINE_TEXT = 'The app must run on the same machine as Plex.';
    const CONFIRM_AGENT_TEXT = 'The Plex database node writes the markers on the Plex machine, so this app can run anywhere.';

    // The dialog is only ever opened while its own server's Edit form is on screen (both callers pass tab.server),
    // so the live selector is the freshest answer; storedAgent is only the fallback for a caller that doesn't.
    function agentEnabledForConfirm(server) {
        const select = $('#markersPlexDatabase');
        if (select && tab.server && server && tab.server.id === server.id) return select.value !== DB_LOCAL;
        return !!storedAgent(server).enabled;
    }

    function confirmPlexMarkers(server) {
        const target = server || tab.server;
        if (tab.pendingConfirmation) return tab.pendingConfirmation;
        const modalEl = $('#markersPlexConfirmModal');
        const toggle = $('#markersEnabled');
        if (!modalEl || !window.bootstrap || !window.bootstrap.Modal) {
            // No way to ask, so no database writes.
            if (toggle) toggle.checked = false;
            syncLibraryColumn();
            return Promise.resolve(false);
        }
        const agentOn = agentEnabledForConfirm(target);
        const sameMachineText = $('#markersPlexConfirmSameMachineText');
        if (sameMachineText) sameMachineText.textContent = agentOn ? CONFIRM_AGENT_TEXT : CONFIRM_SAME_MACHINE_TEXT;
        const sameMachineHint = $('#markersPlexConfirmSameMachineHint');
        if (sameMachineHint) sameMachineHint.classList.toggle('d-none', agentOn);
        const local = $('#markersPlexConfirmLocal');
        if (local) local.textContent = confirmLocalText();

        tab.pendingConfirmation = new Promise((resolve) => {
            let accepted = false;
            const okBtn = $('#markersPlexConfirmOk');
            const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
            // Bootstrap has no public API for stacked modals: the Edit dialog's focus trap would pull focus (and
            // Escape) back behind this one, so it is paused while this modal is open. Private API (Bootstrap
            // 5.3.2, see base.html); test_escape_closes_only_the_confirmation catches a break on upgrade.
            const editDialog = window.bootstrap.Modal.getInstance($('#editServerModal'));
            const editFocusTrap = editDialog && editDialog._focustrap;
            if (editFocusTrap) editFocusTrap.deactivate();
            const onOk = () => {
                accepted = true;
                if (target) target._markersConfirmedAt = new Date().toISOString();
                modal.hide();
            };
            const onShown = () => {
                const backdrops = $$('.modal-backdrop');
                if (backdrops.length) backdrops[backdrops.length - 1].style.zIndex = '1060';
            };
            const onHidden = () => {
                okBtn.removeEventListener('click', onOk);
                modalEl.removeEventListener('shown.bs.modal', onShown);
                modalEl.removeEventListener('hidden.bs.modal', onHidden);
                modalEl.style.zIndex = '';
                if (toggle) toggle.checked = accepted;
                syncLibraryColumn();
                // Bootstrap drops body.modal-open when any modal closes; the Edit dialog is still open.
                if ($('#editServerModal.show')) {
                    document.body.classList.add('modal-open');
                    if (editFocusTrap) editFocusTrap.activate();
                }
                tab.pendingConfirmation = null;
                resolve(accepted);
            };
            okBtn.addEventListener('click', onOk);
            modalEl.addEventListener('shown.bs.modal', onShown);
            modalEl.addEventListener('hidden.bs.modal', onHidden);
            // Stacked over the Edit dialog: this modal and (once shown) its backdrop sit above it.
            modalEl.style.zIndex = '1065';
            modal.show();
        });
        return tab.pendingConfirmation;
    }

    // The same section switch as the rail and the phone strip (servers.js exports it); the direct call is the
    // fallback for a page that loaded this file without servers.js.
    function showLibrariesTab() {
        const shared = window.MPGShared && window.MPGShared.activateSection;
        if (shared) {
            shared('edit-tab-libraries');
            return;
        }
        const trigger = $('#editServerModal [data-bs-target="#edit-tab-libraries"]');
        if (trigger && window.bootstrap && window.bootstrap.Tab) window.bootstrap.Tab.getOrCreateInstance(trigger).show();
    }

    function wire() {
        const dbSelect = $('#markersPlexDatabase');
        if (dbSelect) {
            dbSelect.addEventListener('change', onDbChoiceChange);
        }
        const agentState = $('#markersAgentState');
        if (agentState) {
            agentState.addEventListener('click', (event) => {
                if (!event.target.closest('#markersAgentCheckBtn')) return;
                event.preventDefault();
                // Saved settings are what the check uses, so an unsaved address says so rather than lying.
                if (tab.server) fetchStatus(tab.server, { refresh: true });
            });
        }
        const toggle = $('#markersEnabled');
        if (toggle) {
            // Ask as soon as the switch is flipped on (Save asks again if that was bypassed).
            toggle.addEventListener('change', () => {
                syncLibraryColumn();
                if (tab.server && markersNeedsPlexConfirmation(tab.server)) confirmPlexMarkers(tab.server);
            });
        }
        const list = $('#editLibraryList');
        if (list) {
            list.addEventListener('change', (event) => {
                const el = event.target;
                if (el.classList.contains('markers-lib-toggle')) tab.libraryChoices.set(el.dataset.id, el.checked);
            });
        }
        const pointer = $('#markersLibrariesPointer');
        if (pointer) {
            pointer.addEventListener('click', (event) => {
                if (!event.target.closest('.markers-libraries-link')) return;
                event.preventDefault();
                showLibrariesTab();
            });
        }
        const block = $('#markersStatusBlock');
        if (block) {
            block.addEventListener('click', (event) => {
                const retry = event.target.closest('.markers-status-retry');
                if (retry) {
                    event.preventDefault();
                    if (tab.server) fetchStatus(tab.server);
                    return;
                }
                const install = event.target.closest('#markersInstallPluginBtn');
                if (install) installPlugin(install);
            });
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();

    window.loadMarkersTab = loadMarkersTab;
    window.readMarkersFromForm = readMarkersFromForm;
    window.markersNeedsPlexConfirmation = markersNeedsPlexConfirmation;
    window.confirmPlexMarkers = confirmPlexMarkers;
    window.renderMarkersLibraryColumn = renderLibraryColumn;
})();
