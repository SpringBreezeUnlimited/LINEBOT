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
let historyPage = Number(document.body?.dataset.historyPage || '1') || 1;
let historyHasNext = document.body?.dataset.historyHasNext === 'true';
let historyLoadInFlight = null;

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
    label.textContent = 'チケット番号';
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
        [row.status === 'cancelled' ? '受付からキャンセル' : '呼出から完了', row.service_duration_label || '-'],
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

function applyHistoryFilters() {
    const params = getHistoryQueryParams();
    params.set('page', '1');
    window.location.assign(`/admin/history?${params.toString()}`);
}

async function loadMoreHistoryRows() {
    if (document.hidden || !historyHasNext || historyLoadInFlight) return historyLoadInFlight;
    historyLoadInFlight = (async () => {
        try {
            const params = getHistoryQueryParams();
            params.set('page', String(historyPage + 1));
            params.set('format', 'json');
            const response = await fetch(`/admin/history?${params.toString()}`, {
                cache: 'no-store',
                credentials: 'same-origin',
            });
            if (!response.ok) throw new Error(`Unexpected response: ${response.status}`);
            const data = await response.json();
            if (Number(data.meta?.page) !== historyPage + 1) return;

            const cardList = document.getElementById('history-rows');
            data.rows.forEach((row) => {
                historyRowsCache.push(row);
                cardList.appendChild(createHistoryCard(row));
            });
            historyPage = Number(data.meta.page);
            historyHasNext = Boolean(data.meta.has_next);
            const status = document.getElementById('history-pagination-status');
            status.textContent = `${historyRowsCache.length}件を表示${historyHasNext ? '・右端までスクロールすると次の10件を読み込みます' : ''}`;
        } catch (error) {
            console.error('Failed to load more history rows', error);
        } finally {
            historyLoadInFlight = null;
        }
    })();
    return historyLoadInFlight;
}

document.getElementById('history-type-filter')?.addEventListener('change', applyHistoryFilters);
document.getElementById('history-sort-by')?.addEventListener('change', applyHistoryFilters);
document.getElementById('history-sort-order')?.addEventListener('change', applyHistoryFilters);
document.querySelector('.history-scroll')?.addEventListener('scroll', (event) => {
    const scrollArea = event.currentTarget;
    if (scrollArea.scrollLeft + scrollArea.clientWidth >= scrollArea.scrollWidth - 8) {
        loadMoreHistoryRows();
    }
}, { passive: true });
