const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
const autoCallNotificationStorageKey = 'espresso-last-auto-call-run-at';
const configuredRefreshIntervalMs = Number(document.body?.dataset.adminRefreshIntervalMs || '15000');
const adminRefreshIntervalMs = Number.isFinite(configuredRefreshIntervalMs)
    ? Math.min(300000, Math.max(1000, Math.trunc(configuredRefreshIntervalMs)))
    : 15000;

function parseInitialRowsFromDom() {
    return Array.from(document.querySelectorAll('#active-rows .admin-reservation-card')).map((row) => ({
        id: Number(row.dataset.id || '0'),
        display_no: Number(row.dataset.displayNo || row.dataset.id || '0'),
        created_at: row.dataset.createdAt || '',
        type: row.dataset.type || '',
        type_id: row.dataset.typeId || '',
        call_origin: row.dataset.callOrigin || '',
        status: row.dataset.status || '',
    }));
}

function buildRowsSignature(rows) {
    return rows.map((row) => `${row.id}:${row.display_no || ''}:${row.status}:${row.type_id || ''}:${row.call_origin || ''}:${row.created_at || ''}`).join('|');
}

function buildTypeCountsSignature(typeCounts) {
    return typeCounts.map((count) => `${count.name || ''}:${count.count || 0}`).join('|');
}

let activeRowsCache = parseInitialRowsFromDom();
let typeCountsCache = [];
let activeRowsSignature = buildRowsSignature(activeRowsCache);
let typeCountsSignature = buildTypeCountsSignature(typeCountsCache);
let lastUpdatedAt = null;
let currentPage = Number(document.body?.dataset.adminCurrentPage || '1') || 1;
let firstLoadedPage = currentPage;
let lastLoadedPage = currentPage;
let loadedPages = new Map([[currentPage, activeRowsCache]]);
let hasMoreRows = document.body?.dataset.adminHasNext === 'true';
let paginationSignature = '';
let adminRefreshInFlight = null;
let loadMoreInFlight = null;

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

function setFormDisabledState(form, disabled) {
    form.querySelectorAll('button, input, select, textarea').forEach((element) => {
        if (element.type === 'hidden') return;
        element.disabled = disabled;
    });
}

function updateAcceptingToggleState(acceptingNew) {
    const button = document.getElementById('accepting-toggle-button');
    if (!button) return;
    const form = button.closest('form');
    const nextClassName = acceptingNew ? 'btn btn-success w-100' : 'btn btn-danger w-100';
    const nextText = acceptingNew ? '受付中（停止する）' : '受付停止中（再開する）';
    const nextConfirm = acceptingNew ? '新規受付を停止しますか？' : '新規受付を再開しますか？';
    if (button.className !== nextClassName) button.className = nextClassName;
    if (button.textContent !== nextText) button.textContent = nextText;
    if (form && form.dataset.inlineConfirm !== nextConfirm) form.dataset.inlineConfirm = nextConfirm;
}

function updateAutoCallCountInput(autoCallCount) {
    const input = document.getElementById('auto-call-count-input');
    if (!input || document.activeElement === input) return;
    input.value = String(Number.isFinite(autoCallCount) ? autoCallCount : 0);
}

function getQueryParams(page = currentPage) {
    const params = new URLSearchParams(window.location.search);
    const select = document.getElementById('type-filter');
    if (select && select.value) {
        params.set('type_id', select.value);
    } else {
        params.delete('type_id');
    }
    const sortBy = document.getElementById('sort-by');
    if (sortBy && sortBy.value) params.set('sort_by', sortBy.value);
    params.set('page', String(page));
    const q = params.toString();
    return q ? `?${q}` : '';
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
    const wrapper = document.createElement('div');
    wrapper.className = 'admin-reservation-card__actions';
    if (row.status === 'waiting') {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/admin/call/${row.id}`;
        form.className = 'd-inline';
        form.dataset.ajaxPost = 'true';
        const button = document.createElement('button');
        button.type = 'submit';
        button.className = 'btn btn-sm btn-success';
        button.textContent = '呼出';
        form.appendChild(createCsrfInput());
        form.appendChild(button);
        wrapper.appendChild(form);
    } else if (row.status === 'called') {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/admin/finish/${row.id}`;
        form.className = 'd-inline';
        form.dataset.ajaxPost = 'true';
        const button = document.createElement('button');
        button.type = 'submit';
        button.className = 'btn btn-sm btn-primary';
        button.textContent = '確認完了';
        form.appendChild(createCsrfInput());
        form.appendChild(button);
        wrapper.appendChild(form);
    } else {
        const span = document.createElement('span');
        span.className = 'text-muted small';
        span.textContent = '確認完了';
        wrapper.appendChild(span);
    }
    return wrapper;
}

function buildRow(row) {
    const card = document.createElement('article');
    card.className = 'admin-reservation-card';
    card.dataset.id = String(row.id ?? '');
    card.dataset.displayNo = String(row.display_no ?? row.id ?? '');
    card.dataset.createdAt = row.created_at || '';
    card.dataset.type = row.type || '';
    card.dataset.typeId = row.type_id || '';
    card.dataset.callOrigin = row.call_origin || '';
    card.dataset.status = row.status || '';

    const header = document.createElement('div');
    header.className = 'admin-reservation-card__header';

    const identity = document.createElement('div');
    const label = document.createElement('div');
    label.className = 'admin-reservation-card__label';
    label.textContent = 'チケット番号';
    const value = document.createElement('div');
    value.className = 'admin-reservation-card__value';
    value.textContent = row.display_no ?? row.id ?? '';
    identity.appendChild(label);
    identity.appendChild(value);

    const statusWrap = document.createElement('div');
    statusWrap.className = 'admin-reservation-card__status';
    statusWrap.appendChild(buildStatusCell(row).firstChild);

    header.appendChild(identity);
    header.appendChild(statusWrap);

    const meta = document.createElement('dl');
    meta.className = 'admin-reservation-card__meta';

    const fields = [
        ['受付時刻', row.created_at || '-'],
        ['種類', row.type || '-'],
    ];

    if (row.status === 'called' || row.call_origin) {
        fields.push(['呼出方法', row.call_origin === 'auto' ? '自動' : row.call_origin === 'manual' ? '手動' : '不明']);
    }

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
    card.appendChild(buildActionCell(row));
    return card;
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

function updateAdminRuntimeControls(meta = {}) {
    if (typeof meta.accepting_new === 'boolean') {
        updateAcceptingToggleState(meta.accepting_new);
    }
    if (Number.isFinite(meta.auto_call_count)) {
        updateAutoCallCountInput(meta.auto_call_count);
    }
}

function renderActiveRows() {
    const cardList = document.getElementById('active-rows');
    if (!cardList) return;
    const fragment = document.createDocumentFragment();
    activeRowsCache.forEach((row) => {
        fragment.appendChild(buildRow(row));
    });
    cardList.textContent = '';
    cardList.appendChild(fragment);
}

function renderPagination(pagination = {}) {
    const container = document.getElementById('active-pagination');
    if (!container) return;
    const totalRows = Number(pagination.total_rows || 0);
    const displayedRows = activeRowsCache.length;
    const nextSignature = `${totalRows}:${displayedRows}:${hasMoreRows}`;
    if (nextSignature === paginationSignature) return;
    paginationSignature = nextSignature;

    container.textContent = '';
    const indicator = document.createElement('span');
    indicator.className = 'history-page-indicator';
    indicator.textContent = `${totalRows}件中 ${displayedRows}件を表示`;
    container.appendChild(indicator);

    if (hasMoreRows) {
        const hint = document.createElement('span');
        hint.className = 'text-muted small';
        hint.textContent = '右端までスクロールすると次の10件を読み込みます';
        container.appendChild(hint);
    }
}

function setLoadedPage(page, rows) {
    loadedPages.set(page, rows);
    activeRowsCache = Array.from({ length: lastLoadedPage - firstLoadedPage + 1 }, (_, index) => (
        loadedPages.get(firstLoadedPage + index) || []
    )).flat();
    activeRowsSignature = buildRowsSignature(activeRowsCache);
}

async function fetchAdminDataPage(page) {
    const response = await fetch(`/admin/data${getQueryParams(page)}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Unexpected response: ${response.status}`);
    return response.json();
}

async function loadMoreRows() {
    if (document.hidden || loadMoreInFlight || !hasMoreRows) return loadMoreInFlight;

    loadMoreInFlight = (async () => {
        try {
            const nextPage = lastLoadedPage + 1;
            const data = await fetchAdminDataPage(nextPage);
            const pagination = data.meta?.pagination || {};
            if (Number(pagination.page) !== nextPage) return;
            lastLoadedPage = nextPage;
            hasMoreRows = Boolean(pagination.has_next);
            setLoadedPage(nextPage, data.rows || []);
            renderActiveRows();
            renderPagination(pagination);
            updateLastUpdated();
        } catch (error) {
            console.error('Failed to load more reservations', error);
        } finally {
            loadMoreInFlight = null;
        }
    })();
    return loadMoreInFlight;
}

function refreshAdminData() {
    if (document.hidden) return Promise.resolve();
    if (adminRefreshInFlight) return adminRefreshInFlight;
    adminRefreshInFlight = (async () => {
        try {
            const data = await fetchAdminDataPage(firstLoadedPage);
            updateAdminRuntimeControls(data.meta || {});

            const pagination = data.meta?.pagination || {};
            const returnedPage = Number(pagination.page);
            if (returnedPage && returnedPage !== firstLoadedPage) {
                firstLoadedPage = returnedPage;
                lastLoadedPage = returnedPage;
                currentPage = returnedPage;
                loadedPages = new Map();
                window.history.replaceState({}, '', `/admin${getQueryParams(returnedPage)}`);
            }
            const pagesToRefresh = Array.from(
                { length: Math.max(0, lastLoadedPage - firstLoadedPage) },
                (_, index) => firstLoadedPage + index + 1,
            );
            const additionalPages = await Promise.all(pagesToRefresh.map(fetchAdminDataPage));
            const refreshedPages = [data, ...additionalPages];
            const lastPageData = refreshedPages.at(-1);
            lastLoadedPage = Number(lastPageData.meta?.pagination?.page || firstLoadedPage);
            hasMoreRows = Boolean(lastPageData.meta?.pagination?.has_next);
            loadedPages = new Map();
            refreshedPages.forEach((pageData) => {
                const pageNumber = Number(pageData.meta?.pagination?.page);
                if (pageNumber) loadedPages.set(pageNumber, pageData.rows || []);
            });
            setLoadedPage(firstLoadedPage, loadedPages.get(firstLoadedPage) || []);
            renderActiveRows();
            renderPagination(lastPageData.meta?.pagination || pagination);

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
        } finally {
            adminRefreshInFlight = null;
        }
    })();
    return adminRefreshInFlight;
}

function applyAdminFilters() {
    currentPage = 1;
    window.location.assign(`/admin${getQueryParams(1)}`);
}

function isLoginRedirect(response) {
    try {
        return new URL(response.url, window.location.href).pathname === '/login';
    } catch (error) {
        return false;
    }
}

async function submitAdminAjaxForm(form) {
    if (form.dataset.ajaxBusy === 'true') return;
    form.dataset.ajaxBusy = 'true';
    const formData = new FormData(form);
    setFormDisabledState(form, true);

    try {
        const response = await fetch(form.action, {
            method: (form.method || 'POST').toUpperCase(),
            body: formData,
            credentials: 'same-origin',
        });
        if (isLoginRedirect(response)) {
            window.location.assign(response.url);
            return;
        }
        if (!response.ok) {
            throw new Error(`Unexpected response: ${response.status}`);
        }
        await refreshAdminData();
    } catch (error) {
        console.error('Failed to submit admin form', error);
        window.alert('更新に失敗しました。もう一度試してください。');
    } finally {
        setFormDisabledState(form, false);
        form.dataset.ajaxBusy = 'false';
    }
}

document.getElementById('type-filter')?.addEventListener('change', applyAdminFilters);
document.getElementById('sort-by')?.addEventListener('change', applyAdminFilters);
document.querySelector('.admin-scroll')?.addEventListener('scroll', (event) => {
    const scrollArea = event.currentTarget;
    if (scrollArea.scrollLeft + scrollArea.clientWidth >= scrollArea.scrollWidth - 8) {
        loadMoreRows();
    }
}, { passive: true });
document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.matches('form[data-ajax-post="true"]')) return;
    if (event.defaultPrevented) return;

    event.preventDefault();
    submitAdminAjaxForm(form);
});
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
