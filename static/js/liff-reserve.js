(() => {
    "use strict";

    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
    const liffId = document.body?.dataset.liffId || '';

    const statusBanner = document.getElementById('liff-status');
    const reservationStatus = document.getElementById('reservation-status');
    const reservationMeta = document.getElementById('reservation-meta');
    const typeSelect = document.getElementById('type-select');
    const reserveButton = document.getElementById('reserve-button');
    const refreshButton = document.getElementById('refresh-button');
    const cancelButton = document.getElementById('cancel-button');
    const notice = document.getElementById('liff-notice');

    let idToken = '';
    let acceptingNew = true;
    let currentReservation = null;
    let isBusy = false;
    let hasTypes = false;

    function setBanner(message, level = 'info') {
        if (!statusBanner) return;
        statusBanner.textContent = message;
        statusBanner.className = `status-banner alert-${level}`;
    }

    function showNotice(message, level = 'info') {
        if (!notice) return;
        notice.textContent = message;
        notice.className = `notice alert-${level}`;
        notice.hidden = false;
    }

    function clearNotice() {
        if (!notice) return;
        notice.hidden = true;
    }

    function setBusy(state) {
        isBusy = state;
        updateButtons();
    }

    function updateButtons() {
        const hasReservation = Boolean(currentReservation);
        const hasTypeSelection = Boolean(typeSelect && typeSelect.value);
        if (reserveButton) {
            reserveButton.disabled = isBusy || hasReservation || !acceptingNew || !hasTypes || !hasTypeSelection;
        }
        if (cancelButton) {
            cancelButton.disabled = isBusy || !hasReservation;
        }
        if (refreshButton) {
            refreshButton.disabled = isBusy;
        }
        if (typeSelect) {
            typeSelect.disabled = isBusy || !hasTypes;
        }
    }

    async function apiFetch(path, options = {}) {
        const headers = new Headers(options.headers || {});
        if (csrfToken) {
            headers.set('X-CSRF-Token', csrfToken);
        }
        if (idToken) {
            headers.set('Authorization', `Bearer ${idToken}`);
        }
        headers.set('Accept', 'application/json');
        if (options.body && !headers.has('Content-Type')) {
            headers.set('Content-Type', 'application/json');
        }

        const response = await fetch(path, {
            credentials: 'same-origin',
            ...options,
            headers,
        });

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const message = data.error || '通信に失敗しました。しばらくしてから再度お試しください。';
            throw new Error(message);
        }
        return data;
    }

    function renderTypes(types) {
        if (!typeSelect) return;
        typeSelect.textContent = '';
        hasTypes = Array.isArray(types) && types.length > 0;
        if (!hasTypes) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '受付可能な種類がありません';
            typeSelect.appendChild(option);
            updateButtons();
            return;
        }
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = '種類を選択してください';
        typeSelect.appendChild(placeholder);
        types.forEach((type) => {
            const option = document.createElement('option');
            option.value = String(type.id || '');
            option.textContent = type.name || '-';
            typeSelect.appendChild(option);
        });
        updateButtons();
    }

    function renderReservation(reservation) {
        currentReservation = reservation || null;
        if (!reservationStatus) return;
        if (!reservation) {
            reservationStatus.textContent = '予約はありません。';
            if (reservationMeta) reservationMeta.textContent = '';
            updateButtons();
            return;
        }

        const typeLabel = reservation.type_name || '-';
        if (reservation.status === 'waiting') {
            reservationStatus.textContent = `番号: ${reservation.id} / 種類: ${typeLabel} / 待機中`;
            if (reservationMeta) {
                const waiting = reservation.waiting_people_ahead ?? 0;
                const estimated = reservation.estimated_minutes ?? 0;
                reservationMeta.textContent = `あなたの前: ${waiting}人 / 目安: ${estimated}分`;
            }
        } else if (reservation.status === 'called') {
            reservationStatus.textContent = `【呼出中】番号: ${reservation.id} / 種類: ${typeLabel}`;
            if (reservationMeta) reservationMeta.textContent = '会場へお越しください。';
        } else {
            reservationStatus.textContent = `番号: ${reservation.id} / 状態: ${reservation.status}`;
            if (reservationMeta) reservationMeta.textContent = '';
        }
        updateButtons();
    }

    function applyAcceptingState(value) {
        acceptingNew = Boolean(value);
        if (!acceptingNew) {
            setBanner('現在、新規の予約受付は停止中です。', 'warning');
        } else {
            setBanner('新規予約を受け付けています。', 'info');
        }
        updateButtons();
    }

    async function loadTypes() {
        const data = await apiFetch('/liff/api/types');
        applyAcceptingState(data.accepting_new);
        renderTypes(data.types || []);
    }

    async function loadSummary() {
        const data = await apiFetch('/liff/api/summary');
        applyAcceptingState(data.accepting_new);
        renderReservation(data.reservation);
    }

    async function createReservation() {
        if (!typeSelect || !typeSelect.value) {
            showNotice('予約の種類を選択してください。', 'warning');
            return;
        }
        clearNotice();
        setBusy(true);
        try {
            const data = await apiFetch('/liff/api/reservations', {
                method: 'POST',
                body: JSON.stringify({ type_id: typeSelect.value }),
            });
            applyAcceptingState(data.accepting_new);
            renderReservation(data.reservation);
            showNotice(data.message || '予約を受け付けました。', 'success');
        } catch (error) {
            showNotice(error.message, 'danger');
        } finally {
            setBusy(false);
        }
    }

    async function cancelReservation() {
        clearNotice();
        setBusy(true);
        try {
            const data = await apiFetch('/liff/api/cancel', { method: 'POST' });
            applyAcceptingState(data.accepting_new);
            renderReservation(null);
            showNotice(data.message || '予約をキャンセルしました。', 'success');
        } catch (error) {
            showNotice(error.message, 'danger');
        } finally {
            setBusy(false);
        }
    }

    async function initialize() {
        if (!liffId) {
            setBanner('LIFF_ID が未設定です。管理者へお問い合わせください。', 'danger');
            return;
        }
        if (!window.liff) {
            setBanner('LIFF SDK の読み込みに失敗しました。', 'danger');
            return;
        }
        try {
            await window.liff.init({ liffId });
        } catch (error) {
            console.error(error);
            setBanner('LIFF の初期化に失敗しました。', 'danger');
            return;
        }

        if (!window.liff.isLoggedIn()) {
            window.liff.login({ redirectUri: window.location.href });
            return;
        }

        idToken = window.liff.getIDToken() || '';
        if (!idToken) {
            setBanner('ログイン情報の取得に失敗しました。', 'danger');
            return;
        }

        try {
            await loadTypes();
            await loadSummary();
            updateButtons();
        } catch (error) {
            console.error(error);
            setBanner(error.message || '通信に失敗しました。', 'danger');
        }
    }

    if (typeSelect) {
        typeSelect.addEventListener('change', () => updateButtons());
    }
    if (reserveButton) {
        reserveButton.addEventListener('click', createReservation);
    }
    if (cancelButton) {
        cancelButton.addEventListener('click', cancelReservation);
    }
    if (refreshButton) {
        refreshButton.addEventListener('click', loadSummary);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }
})();
