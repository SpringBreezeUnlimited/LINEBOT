const historyRowsCache = Array.from(document.querySelectorAll('#history-rows tr')).map((row) => ({
    id: Number(row.dataset.id || '0'),
    type_id: row.dataset.typeId || '',
    type: row.dataset.type || '',
    status: row.dataset.status || '',
    created_at: row.dataset.createdAt || '',
    service_duration: Number(row.dataset.serviceDuration || '0'),
    service_duration_label: row.dataset.serviceDurationLabel || '-',
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
    const span = document.createElement('span');
    span.className = 'badge';
    if (status === 'done') {
        span.classList.add('bg-primary');
        span.textContent = '確認完了';
        return span;
    }
    if (status === 'cancelled') {
        span.classList.add('bg-secondary');
        span.textContent = 'キャンセル';
        return span;
    }
    span.classList.add('bg-success');
    span.textContent = '呼出中';
    return span;
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
        if (sortBy === 'created_at') {
            return compareHistoryValues(left.created_at || '', right.created_at || '', sortOrder) || compareHistoryValues(left.id, right.id, 'desc');
        }
        if (sortBy === 'type') {
            return compareHistoryValues(left.type || '', right.type || '', sortOrder) || compareHistoryValues(left.id, right.id, 'desc');
        }
        if (sortBy === 'service_duration') {
            return compareHistoryValues(left.service_duration || 0, right.service_duration || 0, sortOrder) || compareHistoryValues(left.id, right.id, 'desc');
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

        const tdId = document.createElement('td');
        tdId.textContent = row.id || '';

        const tdCreated = document.createElement('td');
        tdCreated.textContent = row.created_at || '-';

        const tdType = document.createElement('td');
        tdType.textContent = row.type || '-';

        const tdStatus = document.createElement('td');
        const statusElem = getHistoryStatusMarkup(row.status);
        tdStatus.appendChild(statusElem);

        const tdDuration = document.createElement('td');
        tdDuration.textContent = row.service_duration_label || '-';

        tr.appendChild(tdId);
        tr.appendChild(tdCreated);
        tr.appendChild(tdType);
        tr.appendChild(tdStatus);
        tr.appendChild(tdDuration);

        tbody.appendChild(tr);
    });

    const params = getHistoryQueryParams();
    window.history.replaceState({}, '', `/admin/history?${params.toString()}`);
    updateHistoryPaginationLinks(params);
}

function updateHistoryPaginationLinks(params) {
    const links = document.querySelectorAll('a[data-history-page]');
    if (!links.length) return;
    links.forEach((link) => {
        const page = Number.parseInt(link.dataset.historyPage || '', 10);
        if (!Number.isFinite(page)) return;
        const nextParams = new URLSearchParams(params.toString());
        nextParams.set('page', String(page));
        link.href = `/admin/history?${nextParams.toString()}`;
    });
}

document.getElementById('history-type-filter')?.addEventListener('change', renderHistoryRows);
document.getElementById('history-sort-by')?.addEventListener('change', renderHistoryRows);
document.getElementById('history-sort-order')?.addEventListener('change', renderHistoryRows);
