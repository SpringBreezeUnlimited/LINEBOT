const configuredLoginLogsRefreshIntervalMs = Number(document.body?.dataset.adminRefreshIntervalMs || '15000');
const loginLogsRefreshIntervalMs = Number.isFinite(configuredLoginLogsRefreshIntervalMs)
    ? Math.min(300000, Math.max(1000, Math.trunc(configuredLoginLogsRefreshIntervalMs)))
    : 15000;

function buildLoginResultBadge(loginResult) {
    const span = document.createElement('span');
    span.className = 'badge';
    if (loginResult === 'failure') {
        span.classList.add('bg-danger');
        span.textContent = '失敗';
        return span;
    }
    span.classList.add('bg-success');
    span.textContent = '成功';
    return span;
}

function buildAdminRoleBadge(adminRole) {
    const span = document.createElement('span');
    span.className = 'badge';
    if (adminRole === 'audit_admin') {
        span.classList.add('bg-primary');
        span.textContent = '監査管理者';
        return span;
    }
    if (adminRole === 'admin') {
        span.classList.add('bg-secondary');
        span.textContent = '通常管理者';
        return span;
    }
    span.classList.add('bg-secondary');
    span.textContent = '不明';
    return span;
}

function createLoginLogRow(row) {
    const tr = document.createElement('tr');

    const cells = [
        String(row.id ?? ''),
        null,
        null,
        row.admin_login_id || '-',
        row.ip_address || '-',
        row.user_agent || '-',
        row.logged_in_at || '-',
    ];

    cells.forEach((value, index) => {
        const td = document.createElement('td');
        if (index === 1) {
            td.appendChild(buildLoginResultBadge(row.login_result));
        } else if (index === 2) {
            td.appendChild(buildAdminRoleBadge(row.admin_role));
        } else {
            td.textContent = value;
        }
        tr.appendChild(td);
    });

    return tr;
}

function renderLoginLogRows(rows) {
    const tbody = document.getElementById('login-log-rows');
    if (!tbody) return;

    tbody.textContent = '';
    if (!Array.isArray(rows) || rows.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 7;
        td.className = 'text-center';
        td.textContent = 'ログイン履歴はまだありません。';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    rows.forEach((row) => {
        tbody.appendChild(createLoginLogRow(row));
    });
}

async function refreshLoginLogs() {
    if (document.hidden) return;
    try {
        const response = await fetch('/admin/login-logs/data', {
            cache: 'no-store',
            credentials: 'same-origin',
        });
        const url = new URL(response.url);
        if (url.pathname === '/login') {
            window.location.assign(response.url);
            return;
        }
        if (!response.ok) return;
        const data = await response.json();
        renderLoginLogRows(Array.isArray(data?.rows) ? data.rows : []);
    } catch (error) {
        // 自動更新は失敗しても画面操作を止めない
    }
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        refreshLoginLogs();
    }
});

refreshLoginLogs();
setInterval(() => {
    refreshLoginLogs();
}, loginLogsRefreshIntervalMs);
