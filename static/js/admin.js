const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
const autoCallNotificationStorageKey = 'espresso-last-auto-call-run-at';
const configuredRefreshIntervalMs = Number(document.body?.dataset.adminRefreshIntervalMs || '15000');
const adminRefreshIntervalMs = Number.isFinite(configuredRefreshIntervalMs)
    ? Math.min(300000, Math.max(1000, Math.trunc(configuredRefreshIntervalMs)))
    : 15000;

function parseInitialRowsFromDom() {
    return Array.from(document.querySelectorAll('#active-rows tr')).map((row) => ({
        id: Number(row.dataset.id || '0'),
        created_at: row.dataset.createdAt || '',
        type: row.dataset.type || '',
        type_id: row.dataset.typeId || '',
        status: row.dataset.status || '',
    }));
}

function buildRowsSignature(rows) {
    return rows.map((row) => `${row.id}:${row.status}:${row.type_id || ''}:${row.created_at || ''}`).join('|');
}

function buildTypeCountsSignature(typeCounts) {
    return typeCounts.map((count) => `${count.name || ''}:${count.count || 0}`).join('|');
}

let activeRowsCache = parseInitialRowsFromDom();
let typeCountsCache = [];
let activeRowsSignature = buildRowsSignature(activeRowsCache);
let typeCountsSignature = buildTypeCountsSignature(typeCountsCache);
let lastUpdatedAt = null;

function formatUpdatedAt(date) {
    return new Intl.DateTimeFormat('ja-JP', {
        timeZone: 'Asia/Tokyo',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
    }).format(date);
}

function updateLastUpdated(date = new Date()) {
    const target = document.getElementById('list-last-updated');
    if (!target) return;
    lastUpdatedAt = date;
    target.textContent = formatUpdatedAt(date);
    updateLastUpdatedWarningState();
}

function updateLastUpdatedWarningState(now = new Date()) {
    const target = document.getElementById('list-last-updated');
    if (!target) return;
    const shouldWarn = Boolean(lastUpdatedAt) && (now.getTime() - lastUpdatedAt.getTime() >= 30000);
    target.classList.toggle('list-meta-value-stale', shouldWarn);
}

function getQueryParams() {
    const params = new URLSearchParams();
    const select = document.getElementById('type-filter');
    if (select && select.value) params.set('type_id', select.value);
    const sortBy = document.getElementById('sort-by');
    if (sortBy && sortBy.value) params.set('sort_by', sortBy.value);
    const q = params.toString();
    return q ? `?${q}` : '';
}

function getActiveFilters() {
    return {
        typeId: document.getElementById('type-filter')?.value || '',
        sortBy: document.getElementById('sort-by')?.value || 'id',
    };
}

function compareValues(left, right) {
    if (left < right) return -1;
    if (left > right) return 1;
    return 0;
}

function applyClientFilters(rows) {
    const { typeId, sortBy } = getActiveFilters();
    const filtered = rows.filter((row) => {
        if (!typeId) return true;
        return String(row.type_id || '') === typeId;
    });

    filtered.sort((left, right) => {
        if (sortBy === 'status') {
            return compareValues(left.status || '', right.status || '') || compareValues(left.id, right.id);
        }
        if (sortBy === 'type') {
            return compareValues(left.type || '', right.type || '') || compareValues(left.id, right.id);
        }
        return compareValues(left.id, right.id);
    });

    return filtered;
}

function createCsrfInput() {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = '_csrf_token';
    input.value = csrfToken;
    return input;
}

function buildStatusCell(row) {
    const td = document.createElement('td');
    const badge = document.createElement('span');
    if (row.status === 'waiting') {
        badge.className = 'badge bg-warning text-dark';
        badge.textContent = '待機中';
    } else if (row.status === 'called') {
        badge.className = 'badge bg-info';
        badge.textContent = '呼出中';
    } else {
        badge.className = 'badge bg-success';
        badge.textContent = '確認完了';
    }
    td.appendChild(badge);
    return td;
}

function buildActionCell(row) {
    const td = document.createElement('td');
    if (row.status === 'waiting') {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/admin/call/${row.id}`;
        form.className = 'd-inline';
        const button = document.createElement('button');
        button.type = 'submit';
        button.className = 'btn btn-sm btn-success';
        button.textContent = '呼出';
        form.appendChild(createCsrfInput());
        form.appendChild(button);
        td.appendChild(form);
    } else if (row.status === 'called') {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/admin/finish/${row.id}`;
        form.className = 'd-inline';
        const button = document.createElement('button');
        button.type = 'submit';
        button.className = 'btn btn-sm btn-primary';
        button.textContent = '確認完了';
        form.appendChild(createCsrfInput());
        form.appendChild(button);
        td.appendChild(form);
    } else {
        const span = document.createElement('span');
        span.className = 'text-muted small';
        span.textContent = '確認完了';
        td.appendChild(span);
    }
    return td;
}

function buildRow(row) {
    const tr = document.createElement('tr');
    const tdId = document.createElement('td');
    tdId.textContent = row.id ?? '';
    const tdCreatedAt = document.createElement('td');
    tdCreatedAt.textContent = row.created_at || '-';
    const tdType = document.createElement('td');
    tdType.textContent = row.type || '-';
    tr.appendChild(tdId);
    tr.appendChild(tdCreatedAt);
    tr.appendChild(tdType);
    tr.appendChild(buildStatusCell(row));
    tr.appendChild(buildActionCell(row));
    return tr;
}

function renderTypeCounts(counts = typeCountsCache) {
    const container = document.getElementById('type-counts');
    if (!container) return;
    container.textContent = '';
    if (!counts || counts.length === 0) {
        const badge = document.createElement('span');
        badge.className = 'badge bg-secondary';
        badge.textContent = '未設定: 0';
        container.appendChild(badge);
        return;
    }
    counts.forEach((count) => {
        const badge = document.createElement('span');
        badge.className = 'badge bg-secondary';
        const name = count.name || '未設定';
        badge.textContent = `${name}: ${count.count}`;
        container.appendChild(badge);
    });
}

function updateAutoCallSummary(summary) {
    const target = document.getElementById('last-auto-call-message');
    if (!target || !summary) return;
    target.textContent = summary.message || 'まだ自動呼出は実行されていません。';
}

function showAutoCallNotification(summary) {
    const target = document.getElementById('call-notification');
    if (!target || !summary || !summary.run_at) return;
    let lastSeen = '';
    try {
        lastSeen = localStorage.getItem(autoCallNotificationStorageKey) || '';
    } catch (e) {
        lastSeen = '';
    }
    if (lastSeen === summary.run_at) return;
    target.hidden = false;
    target.textContent = `自動呼出を実行しました: ${summary.run_at} / ${summary.sent_count || 0}人を呼出`;
    try {
        localStorage.setItem(autoCallNotificationStorageKey, summary.run_at);
    } catch (e) {
        // no-op
    }
}

function renderActiveRows() {
    const tbody = document.getElementById('active-rows');
    if (!tbody) return;
    tbody.textContent = '';
    applyClientFilters(activeRowsCache).forEach((row) => {
        tbody.appendChild(buildRow(row));
    });
    window.history.replaceState({}, '', '/admin' + getQueryParams());
}

async function refreshAdminData() {
    if (document.hidden) return;
    try {
        const res = await fetch('/admin/data', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        const nextRows = data.rows || [];
        const nextRowsSignature = buildRowsSignature(nextRows);
        if (nextRowsSignature !== activeRowsSignature) {
            activeRowsCache = nextRows;
            activeRowsSignature = nextRowsSignature;
            renderActiveRows();
        }

        const nextTypeCounts = data.meta?.type_counts || [];
        const nextTypeCountsSignature = buildTypeCountsSignature(nextTypeCounts);
        if (nextTypeCountsSignature !== typeCountsSignature) {
            typeCountsCache = nextTypeCounts;
            typeCountsSignature = nextTypeCountsSignature;
            renderTypeCounts(nextTypeCounts);
        }

        updateAutoCallSummary(data.meta?.last_auto_call);
        showAutoCallNotification(data.meta?.latest_auto_call);
        updateLastUpdated();
    } catch (e) {
        // no-op
    }
}

function applyAdminFilters() {
    renderActiveRows();
}

document.getElementById('type-filter')?.addEventListener('change', applyAdminFilters);
document.getElementById('sort-by')?.addEventListener('change', applyAdminFilters);
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        refreshAdminData();
    }
});

updateLastUpdated();
setInterval(() => {
    updateLastUpdatedWarningState();
}, 1000);
setInterval(() => {
    refreshAdminData();
}, adminRefreshIntervalMs);

if ('requestIdleCallback' in window) {
    window.requestIdleCallback(() => {
        refreshAdminData();
    }, { timeout: 1000 });
} else {
    setTimeout(() => {
        refreshAdminData();
    }, 0);
}
