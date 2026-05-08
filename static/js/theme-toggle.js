const themeStorageKey = 'espresso-theme';
const themeToggleButtons = Array.from(document.querySelectorAll('[data-theme-toggle]'));

function createThemeToggleContent(button, iconPath, label) {
    const img = document.createElement('img');
    img.src = iconPath;
    img.alt = '';
    img.setAttribute('aria-hidden', 'true');
    img.className = 'theme-mode-icon';

    const text = document.createElement('span');
    text.className = 'theme-mode-label';
    text.textContent = label;

    button.replaceChildren(img, text);
}

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
    themeToggleButtons.forEach((button) => {
        const isDark = theme === 'dark';
        button.setAttribute('aria-pressed', isDark ? 'true' : 'false');
        const darkIconPath = button.dataset.themeToggleDarkIcon || '/static/img/luna.svg';
        const lightIconPath = button.dataset.themeToggleLightIcon || '/static/img/helios.svg';
        const iconPath = isDark ? darkIconPath : lightIconPath;
        const label = isDark ? 'ダーク' : 'ライト';
        createThemeToggleContent(button, iconPath, label);
        button.disabled = false;
    });
}

function initializeThemeToggle() {
    const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    applyTheme(getPreferredTheme());
    themeToggleButtons.forEach((button) => {
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
