/* فرم «درخواست خرید چکی» (پنل کاربری): پیش‌نمایش و اعتبارسنجی سمت کاربر برای تصاویر مدارک.
   اعتبارسنجی اصلی همیشه سمت سرور است (accounts/cheque_credit_service.py)؛ این فقط تجربه‌ی کاربر را بهتر می‌کند. */
(function () {
    'use strict';
    var form = document.getElementById('credit-form');
    if (!form) { return; }
    var maxDocs = parseInt(form.dataset.maxDocs, 10) || 5;
    var maxBytes = (parseInt(form.dataset.maxMb, 10) || 5) * 1024 * 1024;
    var allowed = ['image/jpeg', 'image/png', 'image/webp'];
    var inputs = form.querySelectorAll('input[type="file"]');
    var previews = document.getElementById('credit-previews');
    var errorBox = document.getElementById('credit-docs-error');
    var submit = document.getElementById('credit-submit');
    var urls = [];

    function showError(text) {
        errorBox.textContent = text || '';
        errorBox.hidden = !text;
    }

    function refresh() {
        urls.forEach(function (u) { URL.revokeObjectURL(u); });
        urls = [];
        previews.innerHTML = '';
        var total = 0, problem = '';
        inputs.forEach(function (input) {
            Array.prototype.forEach.call(input.files || [], function (file) {
                total += 1;
                if (allowed.indexOf(file.type) === -1) { problem = 'فقط تصویر JPG، PNG یا WebP مجاز است.'; }
                else if (file.size > maxBytes) { problem = 'حجم هر تصویر حداکثر ' + (maxBytes / 1048576) + ' مگابایت است.'; }
                var url = URL.createObjectURL(file);
                urls.push(url);
                var img = document.createElement('img');
                img.src = url;
                img.alt = file.name;
                img.className = 'w-20 h-20 object-cover rounded-lg border border-gray-200';
                previews.appendChild(img);
            });
        });
        if (!problem && total > maxDocs) { problem = 'حداکثر ' + maxDocs + ' تصویر در مجموع مجاز است.'; }
        showError(problem);
        submit.disabled = !!problem;
    }

    inputs.forEach(function (input) { input.addEventListener('change', refresh); });
    form.addEventListener('submit', function (event) {
        var cheque = document.getElementById('doc_cheque_book');
        if (cheque && !(cheque.files && cheque.files.length)) {
            event.preventDefault();
            showError('تصویر دسته‌چک الزامی است.');
            return;
        }
        submit.disabled = true;                      // جلوگیری از ثبت دوباره با کلیک مکرر
    });
})();
