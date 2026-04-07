const historyRowsCache = Array.from(document.querySelectorAll('#history-rows tr')).map((row) => ({
    id: Number(row.dataset.id || '0'),
    type_id: row.dataset.typeId || '',
    type: row.dataset.type || '',
    message: row.dataset.message || '',
    status: row.dataset.status || '',
}));

function getHistoryFilters() {
    return {
        typeId: document.getElementById('history-type-filter')?.value || '',
        sortBy: document.getElementById('history-sort-by')?.value || 'id',
        sortOrder: document.getElementById('history-sort-order')?.value || 'desc',
    };
}

function getHistoryQueryParams() {
    const params = new URLSearchParams(window.location.search);
    const { typeId, sortBy, sortOrder } = getHistoryFilters();
    if (typeId) {
        params.set('type_id', typeId);
    } else {
        params.delete('type_id');
    }
    params.set('sort_by', sortBy);
    params.set('sort_order', sortOrder);
    return params;
}

function compareHistoryValues(left, right, sortOrder) {
    const multiplier = sortOrder === 'desc' ? -1 : 1;
    if (left < right) return -1 * multiplier;
    if (left > right) return 1 * multiplier;
    return 0;
}

function getHistoryStatusMarkup(status) {
    if (status === 'done') {
        return '<span class="badge bg-primary">確認完了</span>';
    }
    if (status === 'cancelled') {
        return '<span class="badge bg-secondary">キャンセル</span>';
    }
    return '<span class="badge bg-success">到着済み</span>';
}

function applyHistoryFilters(rows) {
    const { typeId, sortBy, sortOrder } = getHistoryFilters();
    const filtered = rows.filter((row) => {
        if (!typeId) return true;
        return String(row.type_id || '') === typeId;
    });

    filtered.sort((left, right) => {
        if (sortBy === 'status') {
            return compareHistoryValues(left.status || '', right.status || '', sortOrder) || compareHistoryValues(left.id, right.id, 'desc');
        }
        if (sortBy === 'type') {
            return compareHistoryValues(left.type || '', right.type || '', sortOrder) || compareHistoryValues(left.id, right.id, 'desc');
        }
        if (sortBy === 'message') {
            return compareHistoryValues(left.message || '', right.message || '', sortOrder) || compareHistoryValues(left.id, right.id, 'desc');
        }
        return compareHistoryValues(left.id, right.id, sortOrder) || compareHistoryValues(left.id, right.id, 'desc');
    });

    return filtered;
}

function renderHistoryRows() {
    const tbody = document.getElementById('history-rows');
    if (!tbody) return;
    tbody.textContent = '';

    applyHistoryFilters(historyRowsCache).forEach((row) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${row.id || ''}</td>
            <td>${row.type || '-'}</td>
            <td>${row.message || '-'}</td>
            <td>${getHistoryStatusMarkup(row.status)}</td>
        `;
        tbody.appendChild(tr);
    });

    const params = getHistoryQueryParams();
    window.history.replaceState({}, '', `/admin/history?${params.toString()}`);
}

document.getElementById('history-type-filter')?.addEventListener('change', renderHistoryRows);
document.getElementById('history-sort-by')?.addEventListener('change', renderHistoryRows);
document.getElementById('history-sort-order')?.addEventListener('change', renderHistoryRows);
