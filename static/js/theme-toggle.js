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
    if (stored) return stored;
    const fromDom = document.documentElement.getAttribute('data-theme');
    if (fromDom === 'dark' || fromDom === 'light') {
        return fromDom;
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
        button.textContent = isDark ? 'ライトモード' : 'ダークモード';
    });
}

function initializeThemeToggle() {
    const currentTheme = getPreferredTheme();
    applyTheme(currentTheme);

    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
        button.addEventListener('click', () => {
            const nextTheme = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
            applyTheme(nextTheme);
            writeStoredTheme(nextTheme);
        });
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeThemeToggle);
} else {
    initializeThemeToggle();
}
