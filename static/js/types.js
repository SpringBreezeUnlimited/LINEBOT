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
        
        // テンプレートもリロード（モーダル用）
        document.querySelectorAll('template[id^="modal-content-"]').forEach(el => el.remove());
        doc.querySelectorAll('template[id^="modal-content-"]').forEach(el => {
            document.body.appendChild(document.importNode(el, true));
        });

        refreshGlobalAcceptingState();

        // モーダルを閉じる
        closeModal();

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

// 「説明文」や「価格」や「種類名」のテキストボックス変更時のみ「保存」ボタンを表示・有効化する制御
document.addEventListener('input', (event) => {
    const input = event.target;
    if (input instanceof HTMLInputElement) {
        let saveBtn = null;
        if (input.classList.contains('types-card__flavor-textarea')) {
            const form = input.closest('form');
            if (form) saveBtn = form.querySelector('.types-card__flavor-save-btn');
        } else if (input.classList.contains('types-card__price-input')) {
            const form = input.closest('form');
            if (form) saveBtn = form.querySelector('.types-card__price-save-btn');
        } else if (input.classList.contains('types-card__name-input')) {
            const form = input.closest('form');
            if (form) saveBtn = form.querySelector('.types-card__name-save-btn');
        }
        if (saveBtn) {
            const isChanged = input.value !== input.defaultValue;
            saveBtn.style.display = isChanged ? 'inline-block' : 'none';
            saveBtn.disabled = !isChanged;
        }
    }
});

// =====================
// Modal (三点メニュー)
// =====================
const modal = document.getElementById('type-edit-modal');
const modalBody = document.getElementById('modal-body');
const modalTitle = document.getElementById('modal-title');
const modalClose = document.getElementById('modal-close');
const modalBackdrop = document.getElementById('modal-backdrop');

function openModal(typeId, typeName) {
    const tpl = document.getElementById('modal-content-' + typeId);
    if (!tpl || !modal || !modalBody) return;

    // テンプレートの内容をモーダルに挿入
    modalBody.innerHTML = '';
    modalBody.appendChild(document.importNode(tpl.content, true));

    if (modalTitle) {
        modalTitle.textContent = '「' + typeName + '」を編集';
    }

    modal.hidden = false;
    document.body.style.overflow = 'hidden';

    // 入力変更ハンドラを再適用（動的に挿入されたDOMのため）
    modalBody.querySelectorAll('input[class*="types-card__"]').forEach(input => {
        input.addEventListener('input', () => {
            let saveBtn = null;
            if (input.classList.contains('types-card__flavor-textarea')) {
                saveBtn = input.closest('form')?.querySelector('.types-card__flavor-save-btn');
            } else if (input.classList.contains('types-card__price-input')) {
                saveBtn = input.closest('form')?.querySelector('.types-card__price-save-btn');
            } else if (input.classList.contains('types-card__name-input')) {
                saveBtn = input.closest('form')?.querySelector('.types-card__name-save-btn');
            }
            if (saveBtn) {
                const isChanged = input.value !== input.defaultValue;
                saveBtn.style.display = isChanged ? 'inline-block' : 'none';
                saveBtn.disabled = !isChanged;
            }
        });
    });
}

function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    document.body.style.overflow = '';
    if (modalBody) modalBody.innerHTML = '';
}

// 三点ボタンのクリックをイベント委譲で捕捉
document.addEventListener('click', (event) => {
    const btn = event.target.closest('.type-item-card__menu-btn');
    if (btn) {
        const typeId = btn.dataset.typeId;
        // カード内の種類名を取得
        const card = btn.closest('.type-item-card');
        const typeName = card?.querySelector('.type-item-card__name')?.textContent?.trim() || '種類';
        openModal(typeId, typeName);
    }
});

if (modalClose) {
    modalClose.addEventListener('click', closeModal);
}

if (modalBackdrop) {
    modalBackdrop.addEventListener('click', closeModal);
}

// Escキーでモーダルを閉じる
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && modal && !modal.hidden) {
        closeModal();
    }
});
