const globalAcceptingBadge = document.getElementById('global-accepting-badge');
const typesRefreshIntervalMs = 15000;

function updateGlobalAcceptingBadge(acceptingNew) {
    if (!globalAcceptingBadge) return;
    globalAcceptingBadge.className = acceptingNew ? 'badge bg-success' : 'badge bg-danger';
    globalAcceptingBadge.textContent = acceptingNew ? '受付中' : '停止中';
    globalAcceptingBadge.dataset.acceptingNew = acceptingNew ? 'true' : 'false';
}

async function refreshGlobalAcceptingState() {
    if (document.hidden) return;
    try {
        const response = await fetch('/admin/data', {
            cache: 'no-store',
            credentials: 'same-origin',
        });
        if (!response.ok) return;
        const data = await response.json();
        if (typeof data?.meta?.accepting_new === 'boolean') {
            updateGlobalAcceptingBadge(data.meta.accepting_new);
        }
    } catch (error) {
        // no-op
    }
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        refreshGlobalAcceptingState();
    }
});

refreshGlobalAcceptingState();
setInterval(() => {
    refreshGlobalAcceptingState();
}, typesRefreshIntervalMs);
