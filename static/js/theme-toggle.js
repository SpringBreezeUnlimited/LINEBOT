const themeStorageKey = 'espresso-theme';

function readStoredTheme() {
    try {
        const value = localStorage.getItem(themeStorageKey);
        if (value === 'dark' || value === 'light') {
            return value;
        }
    } catch (e) {
        return '';
    }
    return '';
}

function writeStoredTheme(value) {
    try {
        localStorage.setItem(themeStorageKey, value);
    } catch (e) {
        // no-op
    }
}

function getPreferredTheme() {
    const stored = readStoredTheme();
    if (stored) {
        return stored;
    }
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        return 'dark';
    }
    return 'light';
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
        const isDark = theme === 'dark';
        button.setAttribute('aria-pressed', isDark ? 'true' : 'false');
        const iconPath = isDark ? '/static/img/luna.svg' : '/static/img/helios.svg';
        const label = isDark ? 'ダーク' : 'ライト';
        button.innerHTML = `<img src="${iconPath}" alt="" aria-hidden="true" class="theme-mode-icon"><span class="theme-mode-label">${label}</span>`;
        button.disabled = false;
    });
}

function initializeThemeToggle() {
    const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    applyTheme(getPreferredTheme());
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
        button.addEventListener('click', () => {
            const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
            const nextTheme = current === 'dark' ? 'light' : 'dark';
            applyTheme(nextTheme);
            writeStoredTheme(nextTheme);
        });
    });
    if (!media) return;
    const syncWithOs = (event) => {
        if (readStoredTheme()) {
            return;
        }
        applyTheme(event.matches ? 'dark' : 'light');
    };
    if (typeof media.addEventListener === 'function') {
        media.addEventListener('change', syncWithOs);
    } else if (typeof media.addListener === 'function') {
        media.addListener(syncWithOs);
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeThemeToggle);
} else {
    initializeThemeToggle();
}
