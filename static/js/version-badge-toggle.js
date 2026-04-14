const versionBadge = document.getElementById('version-badge');
const versionToggle = document.querySelector('[data-version-toggle]');
const versionStorageKey = 'espresso-version-badge-visible';

function setVersionBadgeVisibility(isVisible) {
    if (!versionBadge || !versionToggle) return;
    versionBadge.hidden = !isVisible;
    versionToggle.setAttribute('aria-expanded', String(isVisible));
    versionToggle.textContent = isVisible ? 'バージョン非表示' : 'バージョン表示';
    versionToggle.classList.toggle('version-toggle-hidden', !isVisible);
}

function loadVersionBadgePreference() {
    try {
        return localStorage.getItem(versionStorageKey) !== 'hidden';
    } catch (e) {
        return true;
    }
}

function saveVersionBadgePreference(isVisible) {
    try {
        localStorage.setItem(versionStorageKey, isVisible ? 'visible' : 'hidden');
    } catch (e) {
        // no-op
    }
}

if (versionBadge && versionToggle) {
    setVersionBadgeVisibility(loadVersionBadgePreference());
    versionToggle.addEventListener('click', () => {
        const nextVisible = versionBadge.hidden;
        setVersionBadgeVisibility(nextVisible);
        saveVersionBadgePreference(nextVisible);
    });
}
