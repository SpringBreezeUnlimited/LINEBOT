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

        const msgDiv = document.createElement('div');
        // データ由来の文字列は textContent を使って挿入（XSS防止）
        msgDiv.textContent = form.dataset.inlineConfirm || '';

        const btnWrap = document.createElement('div');
        btnWrap.className = 'd-flex gap-2';

        const okBtn = document.createElement('button');
        okBtn.type = 'button';
        okBtn.className = 'btn btn-sm btn-primary';
        okBtn.textContent = '実行する';
        okBtn.addEventListener('click', () => {
            form.dataset.confirmed = 'true';
            form.requestSubmit();
        });

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'btn btn-sm btn-outline-secondary';
        cancelBtn.textContent = 'キャンセル';
        cancelBtn.addEventListener('click', () => {
            form.dataset.confirmed = 'false';
            closeInlineConfirmation();
        });

        btnWrap.appendChild(okBtn);
        btnWrap.appendChild(cancelBtn);
        wrapper.appendChild(msgDiv);
        wrapper.appendChild(btnWrap);

        const parent = form.closest('.list-group-item, .card-body, .col-sm-6') || form.parentElement;
        parent.appendChild(wrapper);
        activeInlineConfirmation = wrapper;
    });
});
