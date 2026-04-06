let activeInlineConfirmation = null;

function closeInlineConfirmation() {
    if (activeInlineConfirmation) {
        activeInlineConfirmation.remove();
        activeInlineConfirmation = null;
    }
}

document.querySelectorAll('form[data-inline-confirm]').forEach((form) => {
    form.dataset.confirmed = 'false';
    form.addEventListener('submit', (event) => {
        if (form.dataset.confirmed === 'true') {
            form.dataset.confirmed = 'false';
            closeInlineConfirmation();
            return;
        }

        event.preventDefault();
        closeInlineConfirmation();

        const wrapper = document.createElement('div');
        wrapper.className = 'alert alert-warning mt-3 mb-0 d-flex flex-column flex-sm-row align-items-sm-center justify-content-between gap-2';
        wrapper.innerHTML = `
            <div>${form.dataset.inlineConfirm}</div>
            <div class="d-flex gap-2">
                <button type="button" class="btn btn-sm btn-primary">実行する</button>
                <button type="button" class="btn btn-sm btn-outline-secondary">キャンセル</button>
            </div>
        `;

        wrapper.querySelector('.btn-primary').addEventListener('click', () => {
            form.dataset.confirmed = 'true';
            form.requestSubmit();
        });
        wrapper.querySelector('.btn-outline-secondary').addEventListener('click', () => {
            form.dataset.confirmed = 'false';
            closeInlineConfirmation();
        });

        const parent = form.closest('.list-group-item, .card-body, .col-sm-6') || form.parentElement;
        parent.appendChild(wrapper);
        activeInlineConfirmation = wrapper;
    });
});
