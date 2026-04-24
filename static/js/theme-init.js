(() => {
    let theme = '';
    try {
        theme = localStorage.getItem('espresso-theme') || '';
    } catch (e) {
        theme = '';
    }

    if (theme === 'dark' || theme === 'light') {
        document.documentElement.setAttribute('data-theme', theme);
        return;
    }

    document.documentElement.setAttribute(
        'data-theme',
        window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    );
})();
