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

async function submitAjaxForm(form) {
    if (form.dataset.ajaxBusy === 'true') return;
    form.dataset.ajaxBusy = 'true';
    const formData = new FormData(form);

    form.querySelectorAll('button, input, select, textarea').forEach(el => {
        if (el.type !== 'hidden') el.disabled = true;
    });

    try {
        const response = await fetch(form.action, {
            method: (form.method || 'POST').toUpperCase(),
            body: formData,
            credentials: 'same-origin',
        });
        
        const url = new URL(response.url);
        if (url.pathname === '/login') {
            window.location.assign(response.url);
            return;
        }

        const html = await response.text();
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');

        const currentContainer = document.getElementById('types-container');
        const newContainer = doc.getElementById('types-container');
        if (currentContainer && newContainer) {
            currentContainer.innerHTML = newContainer.innerHTML;
        }
        
        refreshGlobalAcceptingState();

    } catch (error) {
        console.error('Failed to submit form', error);
        window.alert('更新に失敗しました。もう一度試してください。');
    } finally {
        if (document.body.contains(form)) {
            form.querySelectorAll('button, input, select, textarea').forEach(el => el.disabled = false);
            form.dataset.ajaxBusy = 'false';
        }
    }
}

document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.matches('form[data-ajax-post="true"]')) return;
    if (event.defaultPrevented) return;

    event.preventDefault();
    submitAjaxForm(form);
});

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        refreshGlobalAcceptingState();
    }
});

refreshGlobalAcceptingState();
setInterval(() => {
    refreshGlobalAcceptingState();
}, typesRefreshIntervalMs);
