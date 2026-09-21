/* پنل کدهای تخفیف کاربر: کپی کد، مودال قوانین و مودال نتیجه‌ی «دریافت کد».
   همه‌چیز با event delegation است تا برای محتوایی که HTMX بعداً وارد صفحه می‌کند (نتیجه‌ی دریافت) هم کار کند. */
(function () {
    'use strict';

    var toast = null;
    function showToast(text) {
        toast = toast || document.getElementById('copy-toast');
        if (!toast) return;
        toast.textContent = text;
        toast.classList.remove('hidden');
        setTimeout(function () { toast.classList.add('hidden'); }, 1800);
    }

    function legacyCopy(code, done) {                 // صفحه‌ی ناامن (http) یا مرورگر بدون clipboard API
        var area = document.createElement('textarea');
        area.value = code;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        try { document.execCommand('copy'); done(); } catch (e) { showToast('کپی نشد؛ کد را دستی کپی کنید'); }
        document.body.removeChild(area);
    }

    function copyCode(code) {
        var done = function () { showToast('کد «' + code + '» کپی شد'); };
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(code).then(done, function () { legacyCopy(code, done); });
        } else {
            legacyCopy(code, done);
        }
    }

    function openModal(id) {
        var modal = document.getElementById(id);
        if (modal) modal.classList.remove('hidden');
    }

    function closeModals() {
        document.querySelectorAll('.js-modal').forEach(function (m) { m.classList.add('hidden'); });
    }

    document.addEventListener('click', function (event) {
        var copy = event.target.closest('[data-copy]');
        if (copy) { copyCode(copy.dataset.copy); return; }

        var rules = event.target.closest('[data-rules-open]');
        if (rules) {
            var source = document.getElementById(rules.dataset.rulesOpen);
            if (!source) return;
            document.getElementById('rules-modal-title').textContent = source.dataset.title || 'قوانین';
            document.getElementById('rules-modal-body').innerHTML = source.innerHTML;
            openModal('rules-modal');
            return;
        }
        if (event.target.closest('[data-modal-close]')) closeModals();
    });

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') closeModals();
    });

    // نتیجه‌ی دریافت کد (HTMX) در مودال نمایش داده می‌شود
    document.body.addEventListener('htmx:afterSwap', function (event) {
        if (event.detail.target && event.detail.target.id === 'claim-modal-body') openModal('claim-modal');
    });
})();
