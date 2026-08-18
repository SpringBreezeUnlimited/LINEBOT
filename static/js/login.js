(() => {
    const form = document.getElementById('login-form');
    const submitButton = document.getElementById('login-submit');
    if (!form || !submitButton) return;

    let submitRequested = false;

    submitButton.addEventListener('click', (event) => {
        if (!form.reportValidity()) {
            event.preventDefault();
            return;
        }
        submitRequested = true;
    });

    form.addEventListener('submit', (event) => {
        if (!submitRequested) {
            event.preventDefault();
            return;
        }
        submitRequested = false;
    });

    form.addEventListener('input', () => {
        submitRequested = false;
    });
})();
