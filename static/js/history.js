const historyRowsCache = Array.from(document.querySelectorAll('#history-rows .history-card')).map((row) => ({
    id: Number(row.dataset.id || '0'),
    display_no: Number(row.dataset.displayNo || row.dataset.id || '0'),
    type_id: row.dataset.typeId || '',
    type: row.dataset.type || '',
    status: row.dataset.status || '',
    call_origin: row.dataset.callOrigin || '',
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

function createHistoryCard(row) {
    const card = document.createElement('article');
    card.className = 'history-card';
    card.dataset.id = String(row.id || '');
    card.dataset.displayNo = String(row.display_no || row.id || '');
    card.dataset.typeId = row.type_id || '';
    card.dataset.type = row.type || '';
    card.dataset.status = row.status || '';
    card.dataset.callOrigin = row.call_origin || '';
    card.dataset.createdAt = row.created_at || '';
    card.dataset.serviceDuration = String(row.service_duration || '');
    card.dataset.serviceDurationLabel = row.service_duration_label || '-';

    const header = document.createElement('div');
    header.className = 'history-card__header';

    const identity = document.createElement('div');
    const label = document.createElement('div');
    label.className = 'history-card__label';
    label.textContent = '番号';
    const value = document.createElement('div');
    value.className = 'history-card__value';
    value.textContent = row.display_no || row.id || '';
    identity.appendChild(label);
    identity.appendChild(value);

    const statusWrap = document.createElement('div');
    statusWrap.className = 'history-card__status';
    statusWrap.appendChild(getHistoryStatusMarkup(row.status));

    header.appendChild(identity);
    header.appendChild(statusWrap);

    const meta = document.createElement('dl');
    meta.className = 'history-card__meta';

    const fields = [
        ['受付時刻', row.created_at || '-'],
        ['種類', row.type || '-'],
        ['呼出方法', row.call_origin === 'auto' ? '自動' : row.call_origin === 'manual' ? '手動' : '不明'],
        ['呼出から完了', row.service_duration_label || '-'],
    ];

    fields.forEach(([term, description]) => {
        const field = document.createElement('div');
        const dt = document.createElement('dt');
        dt.textContent = term;
        const dd = document.createElement('dd');
        dd.textContent = description;
        field.appendChild(dt);
        field.appendChild(dd);
        meta.appendChild(field);
    });

    card.appendChild(header);
    card.appendChild(meta);
    return card;
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
        return compareHistoryValues(left.display_no || left.id, right.display_no || right.id, sortOrder) || compareHistoryValues(left.id, right.id, 'desc');
    });

    return filtered;
}

function renderHistoryRows() {
    const cardList = document.getElementById('history-rows');
    if (!cardList) return;
    cardList.textContent = '';

    applyHistoryFilters(historyRowsCache).forEach((row) => {
        cardList.appendChild(createHistoryCard(row));
    });

    const params = getHistoryQueryParams();
    window.history.replaceState({}, '', `/admin/history?${params.toString()}`);
}

document.getElementById('history-type-filter')?.addEventListener('change', renderHistoryRows);
document.getElementById('history-sort-by')?.addEventListener('change', renderHistoryRows);
document.getElementById('history-sort-order')?.addEventListener('change', renderHistoryRows);
