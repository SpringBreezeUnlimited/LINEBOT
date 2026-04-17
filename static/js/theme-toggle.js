function getPreferredTheme() {
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
        button.textContent = isDark ? 'OS連動中（ダーク）' : 'OS連動中（ライト）';
        button.disabled = true;
    });
}

function initializeThemeToggle() {
    const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    applyTheme(getPreferredTheme());
    if (!media) return;
    if (typeof media.addEventListener === 'function') {
        media.addEventListener('change', (event) => {
            applyTheme(event.matches ? 'dark' : 'light');
        });
    } else if (typeof media.addListener === 'function') {
        media.addListener((event) => {
            applyTheme(event.matches ? 'dark' : 'light');
        });
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeThemeToggle);
} else {
    initializeThemeToggle();
}
