/* Odometer service record: shared by the main page (index.html) and the Cockpit widget (service-widget.html).
   All URLs are relative, so it works on the extension's own port and through BlueOS's /extensionv2/ proxy. */

const esc = t => String(t ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const parseTime = s => new Date(String(s).replace(' ', 'T'));

async function api(path, body) {
    const opts = body === undefined ? { cache: 'no-store' }
        : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const resp = await fetch(path, opts);
    const json = await resp.json().catch(() => ({}));
    if (!resp.ok || json.status === 'error') throw new Error(json.message || `${path}: HTTP ${resp.status}`);
    return json;
}

let toastTimer = 0;
function toast(text, error = false) {
    // Notices appear as a strip of Dymo tape; the page provides <div class="toast" id="toast">
    const t = document.getElementById('toast');
    t.textContent = text; t.className = `toast show ${error ? 'error' : ''}`;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = 'toast', 3500);
}

/* Thruster naming from /thrusters data (Port/Starboard on a boat, Thruster n on an ROV) */
function thrusterNameOf(thrusters, id) {
    const layout = (thrusters && thrusters.layout) || {};
    for (const grid of layout.grids || []) if (grid.names && grid.names[id]) return grid.names[id];
    return `${layout.unit === 'motor' ? 'Motor' : 'Thruster'} ${id}`;
}
function unitWordOf(thrusters) {
    return ((thrusters && thrusters.layout) || {}).unit === 'motor' ? 'Motor' : 'Thruster';
}

// The BlueOS Self Test extension, as seen from this browser (same host, its own port), or null if not running
function selftestBaseOf(stats) {
    const url = stats && stats.selftest && stats.selftest.url;
    if (!url) return null;
    return `${location.protocol}//${location.hostname}:${new URL(url).port}`;
}

const DYMO = { 'Repair': '#b3261e', 'Replacement': '#1f4e8c', 'Maintenance': '#2e7d32', 'Inspection': '#8a6d00',
               'Motor Sticking': '#c2410c', 'Thruster Sticking': '#c2410c', 'Add Device': '#5b2a86', 'Note': '#2b2b2b',
               'Self Test': '#00796b' };

/**
 * The service record panel: a form whose type dropdown filters the list ("All records" by default) and sets the
 * type of the next record, and the list of records, newest first.
 *
 * opts: formEl, listEl            where to render
 *       editable                  show re-date and two-step delete on each record (the Cockpit widget leaves these out)
 *       getRecords()              current records (/maintenance)
 *       getThrusters()            current /thrusters data, for the motor picker and tags
 *       getStats()                current /stats data, for the Self Test link
 *       reload()                  refetch records after a change
 *       onAdded()                 optional, after a record is added (e.g. refresh run hours after a reset)
 */
function createServiceRecord(opts) {
    const state = { editing: null, deleting: null, deleteStep: 1, type: 'All' };
    const unitWord = () => unitWordOf(opts.getThrusters());
    const thrusterName = id => thrusterNameOf(opts.getThrusters(), id);
    const eventTypes = () => ['Repair', 'Replacement', 'Maintenance', 'Inspection', `${unitWord()} Sticking`, 'Note', 'Add Device'];

    function renderForm() {
        const type = state.type, thrusters = opts.getThrusters();
        const sticking = type.endsWith(' Sticking');
        const motorPick = (['Repair', 'Replacement'].includes(type) || sticking) && thrusters && thrusters.thruster_count > 0;
        const reset = ['Repair', 'Replacement'].includes(type);
        const typesShown = [...new Set([...eventTypes(), ...opts.getRecords().map(m => m.event_type)])];
        opts.formEl.innerHTML = `
            <div class="lm-row">
                <select id="lm-type" title="Show records of this type; pick a type to add one">
                    <option value="All" ${type === 'All' ? 'selected' : ''}>All records</option>
                    ${typesShown.map(t => `<option ${t === type ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
                ${type === 'All' ? `<input type="text" placeholder="Pick a record type to add one" disabled style="flex: 3 1 220px">`
                : type === 'Add Device' ? `
                    <input type="text" id="lm-name" placeholder="Device name">
                    <select id="lm-ch">${Array.from({ length: 16 }, (_, n) => `<option value="${n + 1}">CH ${n + 1}</option>`).join('')}</select>
                    <input type="number" id="lm-off" value="1100" min="800" max="2200" title="Run time counts while the output differs from this PWM (lights 1100, gripper 1500)">`
                : `<input type="text" id="lm-details" placeholder="Details" style="flex: 3 1 220px">`}
                <button class="chip-btn go print" id="lm-print" ${type === 'All' ? 'disabled' : ''}>Add record</button>
            </div>
            ${motorPick ? `<div class="lm-row">
                ${thrusters.thrusters.map(t => `<label><input type="checkbox" class="lm-motor" value="${t.id}"> ${esc(thrusterName(t.id))}</label>`).join('')}
                ${reset ? `<label><input type="checkbox" id="lm-reset"> Reset run hours for selected ${unitWord().toLowerCase()}s</label>` : ''}
            </div>` : ''}`;
        const $ = id => document.getElementById(id);
        $('lm-type').onchange = e => { state.type = e.target.value; render(); };
        $('lm-print').onclick = async () => {
            const body = { event_type: type, thruster_ids: [...opts.formEl.querySelectorAll('.lm-motor:checked')].map(c => +c.value),
                           reset_run_hours: !!($('lm-reset') && $('lm-reset').checked) };
            if (type === 'Add Device') {
                Object.assign(body, { device_name: $('lm-name').value.trim(), device_channel: +$('lm-ch').value, device_off_pwm: +$('lm-off').value });
                if (!body.device_name) return toast('Enter a device name', true);
            } else {
                body.details = $('lm-details').value.trim();
                if (!body.details) return toast('Enter the details', true);
                if (sticking && !body.thruster_ids.length) return toast(`Pick the ${unitWord().toLowerCase()}(s)`, true);
            }
            try {
                await api('maintenance', body);
                toast('Record added');
                renderForm();
                await opts.reload();
                if (opts.onAdded) opts.onAdded();
            } catch (e) { toast(e.message, true); }
        };
    }

    function linkLogs(html) {
        // Imported Self Test records name their log; link it to the Self Test extension when it's running
        const base = selftestBaseOf(opts.getStats());
        return base ? html.replace(/(selftest_[A-Za-z0-9_-]+\.log)/g, `<a href="${base}/api/logs/$1" target="_blank" rel="noopener" style="color: inherit">$1</a>`) : html;
    }

    function renderList() {
        const records = [...opts.getRecords()].sort((a, b) => parseTime(b.timestamp) - parseTime(a.timestamp));
        const shown = records.filter(m => state.type === 'All' || m.event_type === state.type);
        if (!shown.length) {
            opts.listEl.innerHTML = `<div class="empty">${state.type === 'All' ? 'No service records yet.' : `No ${esc(state.type)} records yet.`}</div>`;
            return;
        }
        opts.listEl.innerHTML = shown.map(m => {
            const d = parseTime(m.timestamp), ts = esc(m.timestamp);
            const when = state.editing === m.timestamp
                ? `<input type="datetime-local" id="edit-when" value="${new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)}">`
                : `<div class="when" title="${esc(d.toLocaleString())}">${d.toLocaleDateString(undefined, { year: '2-digit', month: 'short', day: '2-digit' }).toUpperCase()}</div>`;
            let acts = '';
            if (!opts.editable) {
                // The Cockpit widget lists and adds only
            } else if (state.editing === m.timestamp) {
                acts = `<button class="chip-btn go" data-act="save" data-ts="${ts}" title="Save">✓</button><button class="chip-btn" data-act="cancel" title="Cancel">✕</button>`;
            } else if (state.deleting === m.timestamp) {
                acts = `<span class="confirm">${state.deleteStep === 1 ? 'Delete this record?' : 'Permanently delete?'}
                        <button class="chip-btn stop" data-act="confirm" data-ts="${ts}">${state.deleteStep === 1 ? 'Yes' : 'Delete'}</button>
                        <button class="chip-btn" data-act="cancel">No</button></span>`;
            } else {
                acts = `<button class="chip-btn" data-act="edit" data-ts="${ts}" title="Edit date and time">✎</button><button class="chip-btn" data-act="delete" data-ts="${ts}" title="Delete">🗑</button>`;
            }
            const motors = (m.thruster_ids || []).map(thrusterName).join(', ');
            const tag = motors ? ` <span class="tag">[${esc(motors)}${m.reset_run_hours ? ' · run hours reset' : ''}]</span>` : '';
            return `<div class="mrow">${when}<div><span class="dymo" style="background:${DYMO[m.event_type] || '#2b2b2b'}">${esc(m.event_type)}</span></div>
                    <div class="details">${linkLogs(esc(m.details))}${tag}</div>${opts.editable ? `<div class="acts">${acts}</div>` : ''}</div>`;
        }).join('');
        if (!opts.editable) return;
        opts.listEl.querySelectorAll('button[data-act]').forEach(b => b.onclick = async () => {
            const ts = b.dataset.ts;
            try {
                switch (b.dataset.act) {
                    case 'edit': Object.assign(state, { editing: ts, deleting: null }); break;
                    case 'save': {
                        const value = document.getElementById('edit-when').value;
                        if (!value) return toast('Pick a date and time', true);
                        await api('maintenance/update', { original_timestamp: ts, new_timestamp: new Date(value).toISOString() });
                        state.editing = null; toast('Record updated'); await opts.reload(); return;
                    }
                    case 'delete': Object.assign(state, { deleting: ts, deleteStep: 1, editing: null }); break;
                    case 'confirm':
                        if (state.deleteStep === 1) { state.deleteStep = 2; break; }
                        await api('maintenance/delete', { timestamp: ts });
                        state.deleting = null; toast('Record deleted'); await opts.reload(); return;
                    default: Object.assign(state, { editing: null, deleting: null });
                }
            } catch (e) { toast(e.message, true); }
            renderList();
        });
    }

    function render() { renderForm(); renderList(); }
    return { render, renderList };
}
