const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const svg = (d, extra = '') => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;
const icons = {
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
  chevron: svg('<path d="m9 18 6-6-6-6"/>'),
  edit: svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>'),
  trash: svg('<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'),
  upload: svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>'),
  download: svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>'),
  search: svg('<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>'),
  server: svg('<rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/><path d="M6 6.5h.01M6 17.5h.01"/>'),
  globe: svg('<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20"/>'),
};

const state = {
  countries: [],
  expanded: new Set(loadExpanded()),
  selectedSubnetId: null,
  devices: [],
  nextIp: null,
  filter: '',
};

// ---------- Helpers ----------

function loadExpanded() {
  try { return JSON.parse(localStorage.getItem('expanded') || '[]'); } catch { return []; }
}
function saveExpanded() {
  try { localStorage.setItem('expanded', JSON.stringify([...state.expanded])); } catch { /* ignore */ }
}

async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const res = await fetch('/api' + path, {
    method: opts.method || 'GET',
    headers: opts.body && !isForm ? { 'Content-Type': 'application/json' } : {},
    body: opts.body && !isForm ? JSON.stringify(opts.body) : opts.body,
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.error || `Request failed (${res.status})`);
  return data;
}

function toast(message, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => el.classList.add('hide'), 2800);
  setTimeout(() => el.remove(), 3200);
}

function findSubnet(id) {
  for (const country of state.countries) {
    const subnet = country.subnets.find((s) => s.id === id);
    if (subnet) return { country, subnet };
  }
  return null;
}

// ---------- Modals ----------

const modal = $('#modal');
modal.addEventListener('click', (e) => { if (e.target === modal) modal.close(); });

function openModal(html, onMount) {
  modal.innerHTML = html;
  if (!modal.open) modal.showModal();
  onMount?.(modal);
}
function closeModal() { modal.close(); }

function fieldHtml(f) {
  const label = `<span>${esc(f.label)}${f.required ? ' <em>*</em>' : ''}</span>`;
  const control = f.type === 'textarea'
    ? `<textarea name="${f.name}" rows="4" placeholder="${esc(f.placeholder || '')}">${esc(f.value || '')}</textarea>`
    : `<input name="${f.name}" value="${esc(f.value || '')}" placeholder="${esc(f.placeholder || '')}" autocomplete="off" spellcheck="false" ${f.mono ? 'class="mono"' : ''}>`;
  return `<label class="field">${label}${control}${f.hint ? `<small>${esc(f.hint)}</small>` : ''}</label>`;
}

function openForm({ title, subtitle, fields, submitLabel = 'Save', onSubmit }) {
  openModal(`
    <form class="modal" novalidate>
      <header class="modal-header"><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</header>
      <div class="modal-body">
        ${fields.map(fieldHtml).join('')}
        <div class="form-error" hidden></div>
      </div>
      <footer class="modal-footer">
        <button type="button" class="btn ghost" data-action="close-modal">Cancel</button>
        <button type="submit" class="btn primary">${esc(submitLabel)}</button>
      </footer>
    </form>`, (dlg) => {
    const form = $('form', dlg);
    $('input, textarea', form)?.focus();
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const errEl = $('.form-error', form);
      const btn = $('[type=submit]', form);
      errEl.hidden = true;
      btn.disabled = true;
      try {
        await onSubmit(Object.fromEntries(new FormData(form)));
        closeModal();
      } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
      } finally {
        btn.disabled = false;
      }
    });
  });
}

function confirmDialog({ title, message, confirmLabel = 'Delete', onConfirm }) {
  openModal(`
    <div class="modal">
      <header class="modal-header"><h2>${esc(title)}</h2></header>
      <div class="modal-body"><p>${message}</p></div>
      <footer class="modal-footer">
        <button type="button" class="btn ghost" data-action="close-modal">Cancel</button>
        <button type="button" class="btn danger" id="confirm-btn">${esc(confirmLabel)}</button>
      </footer>
    </div>`, (dlg) => {
    const btn = $('#confirm-btn', dlg);
    btn.focus();
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await onConfirm(); closeModal(); } catch (err) { toast(err.message, 'error'); btn.disabled = false; }
    });
  });
}

// ---------- Rendering ----------

function renderSidebar() {
  const list = $('#country-list');
  if (!state.countries.length) {
    list.innerHTML = '<li class="sidebar-empty">No countries yet.<br>Click <b>+</b> to add one.</li>';
    return;
  }
  list.innerHTML = state.countries.map((c) => `
    <li class="country ${state.expanded.has(c.id) ? 'open' : ''}">
      <div class="country-row" data-action="toggle-country" data-id="${c.id}">
        <span class="chevron">${icons.chevron}</span>
        <span class="country-name">${esc(c.name)}</span>
        <span class="badge">${c.subnets.length}</span>
        <span class="row-actions">
          <button class="icon-btn" data-action="add-subnet" data-id="${c.id}" title="Add subnet">${icons.plus}</button>
          <button class="icon-btn" data-action="edit-country" data-id="${c.id}" title="Rename">${icons.edit}</button>
          <button class="icon-btn danger" data-action="delete-country" data-id="${c.id}" title="Delete">${icons.trash}</button>
        </span>
      </div>
      <ul class="subnet-list">
        ${c.subnets.map((s) => `
          <li><button class="subnet-item ${s.id === state.selectedSubnetId ? 'active' : ''}" data-action="select-subnet" data-id="${s.id}">
            <span class="subnet-name">${esc(s.name)}</span><span class="subnet-cidr">${esc(s.cidr)}</span>
          </button></li>`).join('')}
        <li><button class="add-subnet-link" data-action="add-subnet" data-id="${c.id}">${icons.plus} Add subnet</button></li>
      </ul>
    </li>`).join('');
}

function stat(label, value, mono = false, extra = '') {
  return `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value ${mono ? 'mono' : ''}">${value}</div>${extra}</div>`;
}

function renderWelcome() {
  if (!state.countries.length) {
    return `<div class="card welcome">
      <div class="empty-icon">${icons.globe}</div>
      <h1>Welcome to IP Inventory</h1>
      <p>Start by adding a country, then create subnets inside it and record the devices on each one.
        Or, if you have a backup, restore it to pick up where you left off.</p>
      <button class="btn primary" data-action="add-country">${icons.plus} Add country</button>
      <button class="btn" data-action="restore-db">${icons.upload} Restore from backup</button>
    </div>`;
  }
  const subnets = state.countries.flatMap((c) => c.subnets);
  const devices = subnets.reduce((n, s) => n + s.device_count, 0);
  return `<div class="card welcome">
    <div class="empty-icon">${icons.server}</div>
    <h1>Select a subnet</h1>
    <p>Choose a subnet from the sidebar to view and manage its devices.</p>
    <div class="stats">
      ${stat('Countries', state.countries.length)}
      ${stat('Subnets', subnets.length)}
      ${stat('Devices', devices)}
    </div>
  </div>`;
}

function renderMain() {
  const main = $('#main');
  const found = state.selectedSubnetId && findSubnet(state.selectedSubnetId);
  if (!found) { main.innerHTML = renderWelcome(); return; }

  const { country, subnet } = found;
  const pct = subnet.usable_hosts ? Math.min(100, (subnet.device_count / subnet.usable_hosts) * 100) : 0;
  main.innerHTML = `
    <section class="card subnet-header">
      <div class="subnet-title-row">
        <div>
          <div class="crumb">${esc(country.name)}</div>
          <h1>${esc(subnet.name)}</h1>
        </div>
        <div class="header-actions">
          <button class="btn ghost" data-action="edit-subnet" data-id="${subnet.id}">${icons.edit} Edit</button>
          <button class="btn ghost" data-action="delete-subnet" data-id="${subnet.id}">${icons.trash} Delete</button>
        </div>
      </div>
      <div class="stats">
        ${stat('CIDR block', esc(subnet.cidr), true)}
        ${stat('Gateway', esc(subnet.gateway), true)}
        ${stat('Devices', `${subnet.device_count} <span class="muted">/ ${subnet.usable_hosts.toLocaleString()}</span>`, false,
          `<div class="usage-bar"><span style="width:${pct}%"></span></div>`)}
      </div>
    </section>

    <section class="card">
      <div class="toolbar">
        <h2>Devices <span class="count-pill" id="device-count">…</span></h2>
        <label class="search">${icons.search}<input id="device-search" placeholder="Search devices" value="${esc(state.filter)}"></label>
        <button class="btn" data-action="import-csv">${icons.upload} Import CSV</button>
        <button class="btn" data-action="export-csv">${icons.download} Export</button>
        <button class="btn primary" data-action="add-device">${icons.plus} Add device</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Hostname</th><th>IP address</th><th>Notes</th><th></th></tr></thead>
          <tbody id="device-rows"></tbody>
        </table>
      </div>
    </section>`;

  $('#device-search').addEventListener('input', (e) => { state.filter = e.target.value; renderDevices(); });
  renderDevices();
}

function renderDevices() {
  const tbody = $('#device-rows');
  if (!tbody) return;
  const gateway = findSubnet(state.selectedSubnetId)?.subnet.gateway;
  const q = state.filter.trim().toLowerCase();
  const rows = q
    ? state.devices.filter((d) => [d.hostname, d.ip, d.notes].some((v) => v.toLowerCase().includes(q)))
    : state.devices;

  $('#device-count').textContent = q ? `${rows.length} of ${state.devices.length}` : state.devices.length;

  if (!state.devices.length) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty">
      <div class="empty-icon">${icons.server}</div>
      <h3>No devices yet</h3>
      <p>Add a device manually or import several at once from a CSV file.</p>
      <button class="btn" data-action="import-csv">${icons.upload} Import CSV</button>
      <button class="btn primary" data-action="add-device">${icons.plus} Add device</button>
    </div></td></tr>`;
    return;
  }
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty"><h3>No matches</h3><p>No devices match “${esc(state.filter)}”.</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((d) => `
    <tr>
      <td class="hostname">${esc(d.hostname)}</td>
      <td class="ip">${esc(d.ip)}${d.ip === gateway ? '<span class="tag">Gateway</span>' : ''}</td>
      <td class="notes">${d.notes ? esc(d.notes) : '<span class="muted">—</span>'}</td>
      <td class="actions">
        <button class="icon-btn" data-action="edit-device" data-id="${d.id}" title="Edit">${icons.edit}</button>
        <button class="icon-btn danger" data-action="delete-device" data-id="${d.id}" title="Delete">${icons.trash}</button>
      </td>
    </tr>`).join('');
}

// ---------- Data loading ----------

async function loadTree() {
  state.countries = await api('/countries');
  if (state.selectedSubnetId && !findSubnet(state.selectedSubnetId)) selectNone();
  renderSidebar();
}

async function loadDevices() {
  const id = state.selectedSubnetId;
  if (!id) return;
  const data = await api(`/subnets/${id}/devices`);
  if (id !== state.selectedSubnetId) return;
  state.devices = data.devices;
  state.nextIp = data.next_ip;
  renderDevices();
}

async function refresh() {
  await loadTree();
  renderMain();
  await loadDevices();
}

function selectNone() {
  state.selectedSubnetId = null;
  state.devices = [];
  history.replaceState(null, '', location.pathname);
}

async function selectSubnet(id) {
  state.selectedSubnetId = id;
  state.devices = [];
  state.filter = '';
  history.replaceState(null, '', `#subnet-${id}`);
  const found = findSubnet(id);
  if (found) { state.expanded.add(found.country.id); saveExpanded(); }
  renderSidebar();
  renderMain();
  await loadDevices();
}

// ---------- Actions ----------

function subnetFormFields(s = {}) {
  return [
    { name: 'name', label: 'Subnet name', placeholder: 'e.g. Office LAN', value: s.name, required: true },
    { name: 'cidr', label: 'CIDR block', placeholder: '10.0.0.0/24', value: s.cidr, required: true, mono: true },
    { name: 'gateway', label: 'Gateway', placeholder: '10.0.0.1', value: s.gateway, required: true, mono: true, hint: 'Must be an address inside the CIDR block.' },
  ];
}

function deviceFormFields(d = {}) {
  return [
    { name: 'hostname', label: 'Hostname', placeholder: 'e.g. core-switch-01', value: d.hostname, required: true },
    { name: 'ip', label: 'IP address', placeholder: '10.0.0.10', value: d.ip, required: true, mono: true },
    { name: 'notes', label: 'Notes', placeholder: 'Location, owner, model, anything useful…', value: d.notes, type: 'textarea' },
  ];
}

const actions = {
  'close-modal': closeModal,

  'toggle-country': (id) => {
    state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
    saveExpanded();
    renderSidebar();
  },

  'add-country': () => openForm({
    title: 'Add country',
    fields: [{ name: 'name', label: 'Country name', placeholder: 'e.g. United Kingdom', required: true }],
    submitLabel: 'Add country',
    onSubmit: async (v) => {
      const c = await api('/countries', { method: 'POST', body: v });
      state.expanded.add(c.id);
      saveExpanded();
      await refresh();
      toast(`Added ${c.name}`);
    },
  }),

  'edit-country': (id) => {
    const c = state.countries.find((x) => x.id === id);
    openForm({
      title: 'Rename country',
      fields: [{ name: 'name', label: 'Country name', value: c.name, required: true }],
      onSubmit: async (v) => { await api(`/countries/${id}`, { method: 'PUT', body: v }); await refresh(); toast('Country renamed'); },
    });
  },

  'delete-country': (id) => {
    const c = state.countries.find((x) => x.id === id);
    const devices = c.subnets.reduce((n, s) => n + s.device_count, 0);
    confirmDialog({
      title: `Delete ${c.name}?`,
      message: `This permanently deletes <b>${esc(c.name)}</b>, its ${c.subnets.length} subnet(s) and ${devices} device(s).`,
      onConfirm: async () => { await api(`/countries/${id}`, { method: 'DELETE' }); state.expanded.delete(id); await refresh(); toast('Country deleted'); },
    });
  },

  'select-subnet': (id) => selectSubnet(id),

  'add-subnet': (countryId) => {
    const c = state.countries.find((x) => x.id === countryId);
    openForm({
      title: 'Add subnet',
      subtitle: `In ${c.name}`,
      fields: subnetFormFields(),
      submitLabel: 'Create subnet',
      onSubmit: async (v) => {
        const s = await api(`/countries/${countryId}/subnets`, { method: 'POST', body: v });
        await loadTree();
        await selectSubnet(s.id);
        toast(`Created ${s.name}`);
      },
    });
  },

  'edit-subnet': (id) => {
    const { subnet } = findSubnet(id);
    openForm({
      title: 'Edit subnet',
      fields: subnetFormFields(subnet),
      onSubmit: async (v) => { await api(`/subnets/${id}`, { method: 'PUT', body: v }); await refresh(); toast('Subnet updated'); },
    });
  },

  'delete-subnet': (id) => {
    const { subnet } = findSubnet(id);
    confirmDialog({
      title: `Delete ${subnet.name}?`,
      message: `This permanently deletes <b>${esc(subnet.cidr)}</b> and its ${subnet.device_count} device(s).`,
      onConfirm: async () => { await api(`/subnets/${id}`, { method: 'DELETE' }); selectNone(); await refresh(); toast('Subnet deleted'); },
    });
  },

  'add-device': () => openForm({
    title: 'Add device',
    subtitle: findSubnet(state.selectedSubnetId)?.subnet.cidr,
    fields: deviceFormFields({ ip: state.nextIp || '' }),
    submitLabel: 'Add device',
    onSubmit: async (v) => {
      const d = await api(`/subnets/${state.selectedSubnetId}/devices`, { method: 'POST', body: v });
      await refresh();
      toast(`Added ${d.hostname}`);
    },
  }),

  'edit-device': (id) => {
    const d = state.devices.find((x) => x.id === id);
    openForm({
      title: 'Edit device',
      fields: deviceFormFields(d),
      onSubmit: async (v) => { await api(`/devices/${id}`, { method: 'PUT', body: v }); await refresh(); toast('Device updated'); },
    });
  },

  'delete-device': (id) => {
    const d = state.devices.find((x) => x.id === id);
    confirmDialog({
      title: 'Delete device?',
      message: `Remove <b>${esc(d.hostname)}</b> (${esc(d.ip)}) from this subnet?`,
      onConfirm: async () => { await api(`/devices/${id}`, { method: 'DELETE' }); await refresh(); toast('Device deleted'); },
    });
  },

  'export-csv': () => { window.location.href = `/api/subnets/${state.selectedSubnetId}/export`; },

  'import-csv': () => openImport(),

  'restore-db': () => openRestore(),
};

function dropzoneHtml(accept, label) {
  return `<label class="dropzone">
    <input type="file" name="file" accept="${accept}" hidden>
    ${icons.upload}
    <div class="dz-text"><b>${label}</b> or drag it here</div>
    <div class="dz-file"></div>
  </label>`;
}

function wireDropzone(form, onChange) {
  const input = $('input[type=file]', form);
  const zone = $('.dropzone', form);
  const changed = () => {
    $('.dz-file', form).textContent = input.files[0]?.name || '';
    onChange();
  };
  input.addEventListener('change', changed);
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('drag');
    if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; changed(); }
  });
  return input;
}

function openRestore() {
  const hasData = state.countries.length > 0;
  openModal(`
    <form class="modal" novalidate>
      <header class="modal-header"><h2>Restore from backup</h2><p>Upload a <code>.db</code> file created with <b>Download backup</b>.</p></header>
      <div class="modal-body">
        ${hasData ? `<div class="warning"><b>This replaces everything.</b> All current countries, subnets and devices will be overwritten.
          A copy of the current data is saved to <code>data/backups/</code> first.</div>` : ''}
        ${dropzoneHtml('.db,.sqlite,.sqlite3', 'Choose a backup file')}
        ${hasData ? '<label class="check"><input type="checkbox" name="confirm"> I understand all current data will be replaced</label>' : ''}
        <div class="form-error" hidden></div>
      </div>
      <footer class="modal-footer">
        <button type="button" class="btn ghost" data-action="close-modal">Cancel</button>
        <button type="submit" class="btn ${hasData ? 'danger' : 'primary'}" disabled>Restore</button>
      </footer>
    </form>`, (dlg) => {
    const form = $('form', dlg);
    const submit = $('[type=submit]', form);
    const errEl = $('.form-error', form);
    const confirmBox = $('input[name=confirm]', form);
    const update = () => {
      submit.disabled = !input.files.length || (confirmBox && !confirmBox.checked);
      errEl.hidden = true;
    };
    const input = wireDropzone(form, update);
    confirmBox?.addEventListener('change', update);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      submit.disabled = true;
      try {
        const r = await api('/restore', { method: 'POST', body: new FormData(form) });
        selectNone();
        state.expanded = new Set();
        saveExpanded();
        await refresh();
        closeModal();
        toast(`Restored ${r.countries} countries, ${r.subnets} subnets, ${r.devices} devices`);
      } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
        submit.disabled = false;
      }
    });
  });
}

function openImport() {
  const { subnet } = findSubnet(state.selectedSubnetId);
  openModal(`
    <form class="modal" novalidate>
      <header class="modal-header"><h2>Import devices</h2><p>Into ${esc(subnet.name)} · ${esc(subnet.cidr)}</p></header>
      <div class="modal-body">
        ${dropzoneHtml('.csv,text/csv', 'Choose a CSV file')}
        <div class="csv-help">
          Columns: <code>hostname, ip, notes</code> — header row optional, notes optional.
          <a href="/api/csv-template">Download template</a>
        </div>
        <label class="check"><input type="checkbox" name="update"> Update hostname &amp; notes when an IP already exists</label>
        <div class="import-result" hidden></div>
        <div class="form-error" hidden></div>
      </div>
      <footer class="modal-footer">
        <button type="button" class="btn ghost" data-action="close-modal">Close</button>
        <button type="submit" class="btn primary" disabled>Import</button>
      </footer>
    </form>`, (dlg) => {
    const form = $('form', dlg);
    const submit = $('[type=submit]', form);
    const errEl = $('.form-error', form);
    const resultEl = $('.import-result', form);

    const input = wireDropzone(form, () => {
      submit.disabled = !input.files.length;
      resultEl.hidden = true;
      errEl.hidden = true;
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      submit.disabled = true;
      errEl.hidden = true;
      try {
        const r = await api(`/subnets/${subnet.id}/import`, { method: 'POST', body: new FormData(form) });
        await refresh();
        const parts = [`${r.imported} imported`];
        if (r.updated) parts.push(`${r.updated} updated`);
        if (r.skipped) parts.push(`${r.skipped} skipped`);
        if (!r.skipped) { closeModal(); toast(parts.join(', ')); return; }
        resultEl.innerHTML = `
          <div class="import-summary warn">${parts.join(', ')}</div>
          <ul class="import-errors">${r.errors.map((x) => `<li>Line ${x.line}: ${esc(x.message)}</li>`).join('')}</ul>`;
        resultEl.hidden = false;
      } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
        submit.disabled = false;
      }
    });
  });
}

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const fn = actions[el.dataset.action];
  if (!fn) return;
  e.preventDefault();
  Promise.resolve(fn(Number(el.dataset.id))).catch((err) => toast(err.message, 'error'));
});

// ---------- Start ----------

(async () => {
  try {
    await loadTree();
    const m = location.hash.match(/^#subnet-(\d+)$/);
    if (m && findSubnet(Number(m[1]))) await selectSubnet(Number(m[1]));
    else renderMain();
  } catch (err) {
    toast(err.message, 'error');
  }
})();
