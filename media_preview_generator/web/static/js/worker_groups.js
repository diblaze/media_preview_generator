/* Shared capacity editor and dashboard controls. Runtime counts come from the server. */
(function () {
    'use strict';
    const JOBS = { previews: 'Video previews', intro_credits: 'Intro & Credits', loudness: 'Plex loudness' };
    const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const clone = value => JSON.parse(JSON.stringify(value));
    const escape = value => {
        const el = document.createElement('span');
        el.textContent = String(value ?? '');
        return el.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    };
    let snapshot = null;
    let draft = null;
    let dirty = false;
    let editing = null;
    let saving = false;
    let loading = null;
    let requestedEditor = new URLSearchParams(window.location.search).get('worker_group');
    const pending = new Set();
    const settings = () => document.getElementById('workerGroupSettings');
    const dashboard = () => document.getElementById('workerGroupDashboard');

    async function request(method, path = '', body) {
        const response = await fetch('/api/worker-groups' + path, {
            method, headers: { 'Content-Type': 'application/json', 'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '' },
            ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(data.error || data.message || 'Worker settings could not be saved.');
            error.conflict = response.status === 409;
            throw error;
        }
        return data;
    }

    function message(text, error = false) {
        for (const id of ['workerGroupMessage', 'workerGroupLiveMessage']) {
            const el = document.getElementById(id);
            if (!el) continue;
            el.textContent = text;
            el.className = 'small mt-2 ' + (error ? 'text-danger-emphasis' : 'text-body-secondary');
        }
    }

    function resourceName(group) {
        if (group.resource === 'cpu') return 'CPU';
        const gpu = (snapshot.hardware || []).find(item => item.device === group.device);
        return gpu?.name || group.device || 'Unavailable GPU';
    }

    function hours(group) {
        if (group.availability.mode === 'always') return 'Always available';
        return group.availability.windows.map(window => {
            const days = window.days.length === 7 ? 'Daily' : window.days.map(day => DAYS[day]).join(', ');
            return `${days} ${window.start}–${window.end}${window.end < window.start ? ' next day' : ''}`;
        }).join('; ');
    }

    function nextTime(value) {
        if (!value) return '';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '';
        try {
            return new Intl.DateTimeFormat(undefined, {
                weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: snapshot.timezone,
            }).format(date) + ' · ' + snapshot.timezone;
        } catch (_) { return date.toLocaleString(); }
    }

    function status(group) {
        const row = (snapshot.capacity?.groups || []).find(item => item.id === group.id) || {};
        const labels = {
            disabled: 'Disabled', outside_hours: 'Outside hours', scheduled: 'Outside hours',
            hardware_unavailable: 'Hardware unavailable', unavailable: 'Hardware unavailable',
            active: 'Within group hours', off_hours: 'Outside hours', draining: 'Finishing current files',
            available: 'Available', open: 'Available', busy: 'Workers busy', paused: 'Globally paused',
            quiet_hours: 'Global pause schedule active',
        };
        const nextOpening = group.enabled && group.availability.mode === 'scheduled'
            && ['off_hours', 'outside_hours', 'scheduled', 'draining'].includes(row.state)
            && Date.parse(row.next_available_at) > Date.now() ? row.next_available_at : null;
        return {
            ...row, next_available_at: nextOpening, available: row.available ?? 0, busy: row.busy ?? 0, finishing: row.finishing ?? 0,
            label: !group.enabled ? 'Disabled' : row.state === 'draining' && nextOpening ? 'Outside hours' : labels[row.state] || 'Configured',
        };
    }

    function activity(state) {
        const parts = [];
        if (!snapshot.processing_paused && state.available) parts.push(`${state.available} available`);
        if (state.busy) parts.push(`${state.busy} ${snapshot.processing_paused ? 'paused' : 'running'}`);
        if (state.finishing) parts.push(`${state.finishing} finishing${snapshot.processing_paused ? ' after resume' : ''}`);
        return parts;
    }

    function resourceDescription(group) {
        if (group.resource === 'cpu') return '<span>CPU</span>';
        const hardware = resourceName(group);
        if (group.name === hardware) return '<span>GPU</span>';
        return `<details class="worker-group-hardware"><summary>GPU hardware</summary><span>${escape(hardware)}</span></details>`;
    }

    function renderRows(container, live) {
        const groups = live ? snapshot.groups : (draft || snapshot.groups);
        const rows = groups.map(group => {
            const state = status(group);
            const busy = pending.has(group.id);
            const controls = live ? `
                <div class="worker-group-capacity"><span class="worker-group-control-label">Workers</span><div class="worker-group-scale" role="group" aria-label="Scale ${escape(group.name)}">
                    <button type="button" class="btn btn-sm btn-outline-secondary" data-scale="-1" data-id="${escape(group.id)}" aria-label="Remove worker from ${escape(group.name)}" ${!group.enabled || busy ? 'disabled' : ''}><i class="bi bi-dash-lg" aria-hidden="true"></i></button>
                    <output aria-label="Desired workers" class="worker-group-count">${group.enabled ? group.count : 0}</output>
                    <button type="button" class="btn btn-sm btn-outline-secondary" data-scale="1" data-id="${escape(group.id)}" aria-label="Add worker to ${escape(group.name)}" ${busy || group.enabled && group.count >= (snapshot.limits?.[group.resource] || 32) ? 'disabled' : ''}><i class="bi bi-plus-lg" aria-hidden="true"></i></button>
                </div></div><a class="btn btn-sm btn-outline-secondary worker-group-edit" href="/settings?worker_group=${encodeURIComponent(group.id)}#section-workers">Edit<span class="visually-hidden"> ${escape(group.name)}</span></a>` : `<div class="worker-group-capacity"><span class="worker-group-control-label">Workers</span><span class="worker-group-count" aria-label="Configured workers">${group.count}</span></div><button type="button" class="btn btn-sm btn-outline-secondary worker-group-edit" data-edit="${escape(group.id)}" ${saving ? 'disabled' : ''}>Edit<span class="visually-hidden"> ${escape(group.name)}</span></button>`;
            const showState = live && !['Within group hours', 'Available', 'Workers busy', 'Configured'].includes(state.label);
            const counts = live ? activity(state) : [];
            const availability = !group.enabled && live ? `${group.count} saved worker${group.count === 1 ? '' : 's'}` : hours(group);
            return `<div class="worker-group-row" data-group-id="${escape(group.id)}">
                <div class="worker-group-description"><strong>${escape(group.name)}</strong><div class="worker-group-meta small text-body-secondary">${resourceDescription(group)}<span>${group.job_types.map(kind => escape(JOBS[kind] || kind)).join(' · ')}</span></div>
                <div class="worker-group-availability small text-body-secondary"><span>${escape(availability)}</span>${live ? `<span class="worker-group-state" ${showState ? '' : 'hidden'}>${escape(state.label)}${state.next_available_at ? ' · Next ' + escape(nextTime(state.next_available_at)) : ''}</span>${counts.length ? `<span class="worker-group-counts">${counts.map(count => `<span>${escape(count)}</span>`).join('')}</span>` : ''}` : ''}</div></div>
                <div class="worker-group-actions">${controls}<label class="form-check form-switch mb-0"><input class="form-check-input" type="checkbox" role="switch" data-enable="${escape(group.id)}" aria-label="Enable ${escape(group.name)}" ${group.enabled ? 'checked' : ''} ${busy || saving ? 'disabled' : ''}><span class="small">Enabled</span></label></div>
            </div>`;
        }).join('');
        const removed = live ? (snapshot.capacity?.groups || []).filter(row => !groups.some(group => group.id === row.id) && row.finishing > 0).map(row => `<div class="worker-group-row" data-retired-group="${escape(row.id)}"><div><strong>${escape(row.name || 'Removed group')}</strong><div class="small text-body-secondary">Removed · ${row.finishing} finishing${snapshot.processing_paused ? ' (paused)' : ''} · ${escape(row.resource === 'cpu' ? 'CPU' : row.device || 'GPU')}</div></div></div>`).join('') : '';
        const markup = rows + removed || '<p class="text-body-secondary mb-2">No worker groups configured. Jobs wait until a compatible group is available.</p>';
        if (container._groupMarkup === markup) return;
        container._groupMarkup = markup;
        container.innerHTML = markup;
        container.querySelectorAll('[data-scale]').forEach(button => button.addEventListener('click', () => scale(button.dataset.id, { delta: Number(button.dataset.scale) })));
        container.querySelectorAll('[data-edit]').forEach(button => button.addEventListener('click', () => edit(button.dataset.edit)));
        container.querySelectorAll('[data-enable]').forEach(input => input.addEventListener('change', () => {
            if (live) scale(input.dataset.enable, { enabled: input.checked });
            else { ensureDraft(); dirty = true; draft.find(group => group.id === input.dataset.enable).enabled = input.checked; renderSettings(); }
        }));
    }

    function warnings() {
        return (snapshot.warnings || []).map(warning => typeof warning === 'string' ? warning : warning.message || '').filter(Boolean);
    }

    function renderDashboard() {
        const mount = dashboard();
        if (!mount || !snapshot) return;
        if (!document.getElementById('workerGroupLiveRows')) mount.innerHTML = '<p id="workerGroupHold" class="worker-group-hold small text-warning-emphasis mb-2" role="status" hidden></p><div id="workerGroupLiveRows"></div><div id="workerGroupLiveWarnings" class="small text-warning-emphasis mt-2"></div><div id="workerGroupLiveMessage" role="status" aria-live="polite"></div><p class="small text-body-secondary mt-2 mb-0">Worker counts set simultaneous tasks, not CPU cores. Current files finish when a group is reduced.</p><a href="/settings#section-workers" class="small d-inline-block mt-2">Manage groups and availability</a>';
        const hold = document.getElementById('workerGroupHold');
        hold.hidden = !snapshot.processing_paused;
        const owners = (snapshot.pause_reasons || []).map(reason => reason === 'quiet_hours' ? 'global pause schedule' : 'manual pause');
        const resumeHint = owners.includes('global pause schedule')
            ? owners.includes('manual pause') ? 'Resume processing and wait for the pause schedule to end.' : 'Processing resumes when the pause schedule ends.'
            : 'Resume processing to use available groups.';
        hold.textContent = 'Processing paused' + (owners.length ? ': ' + owners.join(' and ') : '') + '. ' + resumeHint;
        renderRows(document.getElementById('workerGroupLiveRows'), true);
        document.getElementById('workerGroupLiveWarnings').textContent = warnings().join(' ');
    }

    function renderSettings() {
        const mount = settings();
        if (!mount || !snapshot) return;
        if (!document.getElementById('workerGroupRows')) mount.innerHTML = `
            <div class="d-flex align-items-center justify-content-between gap-2 mb-2"><h3 class="settings-subheading mb-0">Worker groups</h3><button type="button" class="btn btn-sm btn-outline-primary" id="workerGroupAdd"><i class="bi bi-plus-lg me-1" aria-hidden="true"></i>Add group</button></div>
            <p class="form-text mt-0">Choose the jobs each group can run and when. Groups on the same resource add their worker counts.</p>
            <div id="workerGroupCapacity" class="small text-body-secondary mb-2"></div><div id="workerGroupRows"></div><div id="workerGroupWarnings" class="alert alert-warning py-2 mt-3" hidden></div>
            <button type="button" class="btn btn-sm btn-outline-secondary mt-2" id="workerGroupAddCpu">Add CPU group for loudness</button>
            <div id="workerGroupEditor" class="worker-group-editor" hidden></div>
            <div class="d-flex align-items-center gap-2 mt-3" id="workerGroupApplyRow" hidden><button type="button" class="btn btn-primary btn-sm" id="workerGroupApply">Apply group changes</button><button type="button" class="btn btn-outline-secondary btn-sm" id="workerGroupCancel">Discard changes</button><span class="small text-body-secondary">Unsaved group changes</span></div>
            <div id="workerGroupMessage" role="status" aria-live="polite"></div>
            <p class="form-text mt-3 mb-0">Current files finish when a group closes or is reduced. GPU jobs may still use CPU stages or fallback. Chapter thumbnails are part of Video previews. The global job limit still applies.</p>`;
        const capacity = snapshot.capacity || {};
        document.getElementById('workerGroupCapacity').textContent = capacity.current && capacity.peak ? `${dirty ? 'Saved schedule' : 'Scheduled'} now: CPU ${capacity.current.cpu} · GPU ${capacity.current.gpu}. Weekly peak: CPU ${capacity.peak.cpu} · GPU ${capacity.peak.gpu}. ${snapshot.timezone}.` : '';
        renderRows(document.getElementById('workerGroupRows'), false);
        const warning = document.getElementById('workerGroupWarnings');
        warning.textContent = warnings().join(' ');
        warning.hidden = !warning.textContent;
        document.getElementById('workerGroupApplyRow').hidden = !dirty;
        document.getElementById('workerGroupApply').disabled = saving;
        const editorApply = document.getElementById('workerGroupEditorApply');
        if (editorApply) editorApply.disabled = saving || !dirty;
        document.getElementById('workerGroupCancel').disabled = saving;
        document.getElementById('workerGroupAdd').disabled = saving;
        document.getElementById('workerGroupAddCpu').disabled = saving;
        if (saving) document.getElementById('workerGroupEditor').querySelectorAll('input,select,button').forEach(control => { control.disabled = true; });
        document.getElementById('workerGroupAdd').onclick = () => add(false);
        document.getElementById('workerGroupAddCpu').onclick = () => add(true);
        document.getElementById('workerGroupApply').onclick = () => save().catch(() => {});
        document.getElementById('workerGroupCancel').onclick = async () => { draft = null; dirty = false; editing = null; renderEditor(); await load(true); message('Unsaved group changes discarded. Latest groups loaded.'); };
    }

    function ensureDraft() { if (!draft) draft = clone(snapshot.groups); }
    function edit(id) { ensureDraft(); editing = id; renderSettings(); renderEditor(); document.getElementById('workerGroupName').focus(); }
    function add(loudness) {
        ensureDraft();
        dirty = true;
        const id = window.crypto?.randomUUID?.() || 'group-' + Date.now().toString(36) + Math.random().toString(36).slice(2);
        draft.push({ id, name: loudness ? 'CPU loudness' : 'New worker group', enabled: true, resource: 'cpu', device: null, count: 1,
            job_types: loudness ? ['loudness'] : ['previews', 'intro_credits', 'loudness'], availability: { mode: 'always', windows: [] } });
        edit(id);
    }

    function renderEditor() {
        const container = document.getElementById('workerGroupEditor');
        if (!container) return;
        const group = draft?.find(item => item.id === editing);
        container.hidden = !group;
        if (!group) return;
        const devices = (snapshot.hardware || []).filter(gpu => gpu.device);
        if (group.device && !devices.some(gpu => gpu.device === group.device)) devices.push({ device: group.device, name: group.device + ' (unavailable)' });
        container.innerHTML = `
            <div class="d-flex align-items-center justify-content-between gap-2"><h4 class="h6 mb-0">Edit group</h4><button type="button" class="btn btn-sm btn-outline-secondary" id="workerGroupClose">Close editor</button></div>
            <div class="d-flex flex-wrap align-items-center gap-2 mt-2 mb-3"><button type="button" class="btn btn-sm btn-primary" id="workerGroupEditorApply" ${saving || !dirty ? 'disabled' : ''}>Apply group changes</button><span class="small text-body-secondary">Save all group edits. Closing keeps your draft.</span></div>
            <div class="row g-3"><div class="col-12"><label class="form-label" for="workerGroupName">Name</label><input id="workerGroupName" class="form-control" maxlength="100" value="${escape(group.name)}"></div>
            <div class="col-sm-8"><label class="form-label" for="workerGroupResource">Resource</label><select id="workerGroupResource" class="form-select"><option value="cpu">CPU</option>${devices.map(gpu => `<option value="${escape(gpu.device)}" ${group.resource === 'gpu' && group.device === gpu.device ? 'selected' : ''}>${escape(gpu.name || gpu.device)}${gpu.status === 'failed' ? ' (unavailable)' : ''}</option>`).join('')}</select></div>
            <div class="col-sm-4"><label class="form-label" for="workerGroupCount">Workers</label><input id="workerGroupCount" type="number" min="1" max="${snapshot.limits?.[group.resource] || 32}" value="${group.count}" class="form-control"><div class="form-text">Simultaneous tasks, not CPU cores.</div></div></div>
            <fieldset class="mt-3"><legend class="form-label">Jobs allowed on this group</legend><div class="d-flex flex-wrap gap-3">${Object.entries(JOBS).map(([kind, label]) => `<label class="form-check mb-0"><input type="checkbox" class="form-check-input" data-kind="${kind}" ${group.job_types.includes(kind) ? 'checked' : ''} ${kind === 'loudness' && group.resource !== 'cpu' ? 'disabled' : ''}><span>${label}</span></label>`).join('')}</div>${group.resource !== 'cpu' ? '<p class="form-text mb-0">Plex loudness requires CPU workers.</p>' : ''}</fieldset>
            <div class="mt-3"><label class="form-label" for="workerGroupAvailability">Availability</label><select class="form-select" id="workerGroupAvailability"><option value="always">Always available</option><option value="scheduled" ${group.availability.mode === 'scheduled' ? 'selected' : ''}>Weekly hours</option></select></div>
            <div id="workerGroupWindows" ${group.availability.mode === 'always' ? 'hidden' : ''}>${group.availability.windows.map((window, index) => windowEditor(window, index)).join('')}<button type="button" id="workerGroupAddWindow" class="btn btn-outline-secondary btn-sm mt-2">Add time window</button><p class="form-text">${escape(snapshot.timezone || 'App timezone')}. Days select when the window starts: Mon 23:00–07:00 ends Tuesday. Overlapping windows in this group count once.</p></div>
            <div class="d-flex flex-wrap gap-2 mt-3"><button type="button" class="btn btn-sm btn-outline-secondary" id="workerGroupDuplicate">Duplicate group</button><button type="button" class="btn btn-sm btn-outline-danger" id="workerGroupRemove">Remove group</button></div>`;
        const changed = () => { dirty = true; renderSettings(); message('Group changes are not saved until you apply them.'); };
        document.getElementById('workerGroupEditorApply').onclick = () => save().catch(() => {});
        document.getElementById('workerGroupName').oninput = event => { group.name = event.target.value; changed(); };
        document.getElementById('workerGroupCount').oninput = event => { group.count = Number(event.target.value); changed(); };
        document.getElementById('workerGroupResource').onchange = event => {
            group.resource = event.target.value === 'cpu' ? 'cpu' : 'gpu'; group.device = group.resource === 'cpu' ? null : event.target.value;
            if (group.resource === 'gpu') group.job_types = group.job_types.filter(kind => kind !== 'loudness');
            changed(); renderEditor();
        };
        container.querySelectorAll('[data-kind]').forEach(input => input.onchange = () => {
            group.job_types = [...container.querySelectorAll('[data-kind]:checked')].map(input => input.dataset.kind); changed();
        });
        document.getElementById('workerGroupAvailability').onchange = event => {
            group.availability.mode = event.target.value;
            if (event.target.value === 'scheduled' && !group.availability.windows.length) group.availability.windows.push(defaultWindow());
            changed(); renderEditor();
        };
        document.getElementById('workerGroupAddWindow').onclick = () => { group.availability.windows.push(defaultWindow()); changed(); renderEditor(); };
        container.querySelectorAll('[data-window]').forEach(row => {
            const window = group.availability.windows[Number(row.dataset.window)];
            row.querySelectorAll('[data-day]').forEach(input => input.onchange = () => { window.days = [...row.querySelectorAll('[data-day]:checked')].map(input => Number(input.dataset.day)); changed(); });
            for (const key of ['start', 'end']) row.querySelector('[data-time="' + key + '"]').onchange = event => { window[key] = event.target.value; changed(); };
            row.querySelector('[data-remove-window]').onclick = () => { group.availability.windows.splice(Number(row.dataset.window), 1); changed(); renderEditor(); };
        });
        document.getElementById('workerGroupClose').onclick = () => { editing = null; if (!dirty) draft = null; renderSettings(); renderEditor(); document.getElementById(dirty ? 'workerGroupApply' : 'workerGroupAdd').focus(); };
        document.getElementById('workerGroupDuplicate').onclick = () => { const duplicate = clone(group); duplicate.id = 'group-' + Date.now().toString(36) + Math.random().toString(36).slice(2); duplicate.name += ' copy'; dirty = true; draft.push(duplicate); edit(duplicate.id); };
        document.getElementById('workerGroupRemove').onclick = () => { draft = draft.filter(item => item.id !== group.id); editing = null; changed(); renderEditor(); };
    }

    function defaultWindow() { return { days: [0, 1, 2, 3, 4, 5, 6], start: '23:00', end: '07:00' }; }
    function windowEditor(window, index) {
        return `<fieldset class="worker-group-window mt-3" data-window="${index}"><legend class="small fw-semibold">Window ${index + 1}</legend><div class="d-flex flex-wrap gap-2 mb-2">${DAYS.map((day, number) => `<label class="worker-group-day"><input type="checkbox" class="form-check-input m-0" data-day="${number}" ${window.days.includes(number) ? 'checked' : ''}><span>${day}</span></label>`).join('')}</div><div class="row g-2 align-items-end"><div class="col"><label class="form-label small" for="wgStart${index}">Start</label><input type="time" class="form-control" id="wgStart${index}" data-time="start" value="${escape(window.start)}"></div><div class="col"><label class="form-label small" for="wgEnd${index}">End</label><input type="time" class="form-control" id="wgEnd${index}" data-time="end" value="${escape(window.end)}"></div><div class="col-auto"><button type="button" class="btn btn-sm btn-outline-secondary" data-remove-window aria-label="Remove window ${index + 1}"><i class="bi bi-trash" aria-hidden="true"></i></button></div></div></fieldset>`;
    }

    function validate() {
        for (const group of draft) {
            if (!group.name.trim()) return 'Every group needs a name.';
            if (!Number.isInteger(group.count) || group.count < 1 || group.count > (snapshot.limits?.[group.resource] || 32)) return `${group.name}: enter a whole worker count between 1 and ${snapshot.limits?.[group.resource] || 32}. Disable a group to use zero workers.`;
            if (!group.job_types.length) return `${group.name}: select at least one job type.`;
            if (group.resource === 'gpu' && group.job_types.includes('loudness')) return `${group.name}: loudness requires CPU workers.`;
            if (group.availability.mode === 'scheduled') {
                if (!group.availability.windows.length) return `${group.name}: add a time window or choose Always available.`;
                for (const window of group.availability.windows) {
                    if (!window.days.length) return `${group.name}: select at least one start day for every window.`;
                    if (!/^\d{2}:\d{2}$/.test(window.start) || !/^\d{2}:\d{2}$/.test(window.end) || window.start === window.end) return `${group.name}: each window needs different valid start and end times.`;
                }
            }
        }
        return '';
    }

    async function save() {
        if (!draft || !dirty) return;
        if (saving) throw new Error('Worker groups are still saving.');
        const error = validate();
        if (error) { message(error, true); throw new Error(error); }
        saving = true; renderSettings();
        try {
            snapshot = await request('PUT', '', { groups: draft, revision: snapshot.revision });
            draft = null; dirty = false; editing = null; renderSettings(); renderEditor(); renderDashboard();
            message(snapshot.warning || 'Worker groups saved. Current files finish; new assignments use these settings.', !!snapshot.warning);
        } catch (error) {
            message(error.conflict ? 'Worker groups changed elsewhere. Your draft is preserved. Discard it and reload the latest groups before editing again.' : error.message, true);
            if (error.conflict) { await load(true); }
            throw error;
        } finally { saving = false; renderSettings(); renderEditor(); }
    }

    async function scale(id, body) {
        if (pending.has(id)) return;
        const focused = document.activeElement;
        const restoreFocus = focused?.dataset.id === id || focused?.dataset.enable === id;
        pending.add(id); renderDashboard();
        try {
            const updated = await request('POST', '/' + encodeURIComponent(id) + '/scale', body);
            if (!snapshot || updated.revision >= snapshot.revision) snapshot = updated;
            renderDashboard(); message(snapshot.warning || 'Worker group updated. Current files finish before reduced slots stop.', !!snapshot.warning);
        } catch (error) { message(error.message, true); }
        finally {
            pending.delete(id); renderDashboard();
            if (restoreFocus && document.activeElement === document.body) {
                const selector = 'delta' in body ? `[data-id="${CSS.escape(id)}"][data-scale="${body.delta}"]` : `[data-enable="${CSS.escape(id)}"]`;
                dashboard()?.querySelector(selector)?.focus();
            }
        }
    }

    async function load(force = false) {
        if (loading) return loading;
        loading = (async () => {
            try {
                const data = await request('GET');
                if (snapshot && data.revision < snapshot.revision) return;
                // An open draft retains its original revision; a newer read must not authorize overwriting concurrent edits.
                if (draft && snapshot && !force) { snapshot.capacity = data.capacity; snapshot.warnings = data.warnings; snapshot.hardware = data.hardware; }
                else if (draft && snapshot) { snapshot.capacity = data.capacity; }
                else snapshot = data;
                renderSettings(); renderDashboard();
                if (requestedEditor && settings()) {
                    const id = requestedEditor; requestedEditor = null;
                    if (snapshot.groups.some(group => group.id === id)) {
                        edit(id);
                        document.getElementById('workerGroupEditor').scrollIntoView({ block: 'nearest' });
                    } else message('That worker group no longer exists. Choose a group below or add one.', true);
                }
            } catch (error) {
                for (const mount of [settings(), dashboard()]) if (mount && !snapshot) mount.innerHTML = '<p class="text-danger small">Could not load worker groups. Reload the page to try again.</p>';
                message(error.message, true);
            } finally { loading = null; }
        })();
        return loading;
    }
    window.WorkerGroups = { load, save, hasDraft: () => dirty, refreshHardware: () => load(true) };
    window.addEventListener('beforeunload', event => { if (dirty && !saving) { event.preventDefault(); event.returnValue = ''; } });
    document.addEventListener('DOMContentLoaded', () => {
        if (!settings() && !dashboard()) return;
        load();
        setInterval(() => { if (!document.hidden && !pending.size && !saving) load(); }, dashboard() ? 5000 : 10000);
    });
})();
