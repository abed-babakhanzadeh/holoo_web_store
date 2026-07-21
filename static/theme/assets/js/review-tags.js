/**
 * ویجت‌های فرم نظر: تگ نقاط قوت/ضعف، شمارنده کاراکتر، آپلود تصویر سفارشی،
 * و ارسال فرم با AJAX (تا در صورت خطای اعتبارسنجی، صفحه رفرش/رد نشود و
 * هیچ‌کدام از مقادیر واردشده از بین نرود).
 */
document.addEventListener('DOMContentLoaded', function () {
    // ------------------------------------------------------------------
    // تگ‌های نقاط قوت/ضعف: به‌ازای هر تگ یک input[type=hidden] هم‌نام می‌سازد
    // تا با request.POST.getlist(...) در جنگو قابل خواندن باشد.
    // ------------------------------------------------------------------
    document.querySelectorAll('.review-tag-container').forEach(function (container) {
        const input = container.querySelector('.review-tag-input');
        if (!input) return;

        const fieldName = container.dataset.name || 'tags';
        const colorClass = container.dataset.color || 'text-gray-600';

        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && input.value.trim()) {
                e.preventDefault();
                addTag(input.value.trim());
                input.value = '';
            }
        });

        function addTag(text) {
            const tag = document.createElement('span');
            tag.className = 'inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs bg-gray-100 dark:bg-gray-700 ' + colorClass;

            const hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = fieldName;
            hidden.value = text;
            tag.appendChild(hidden);

            const label = document.createElement('span');
            label.textContent = text;
            tag.appendChild(label);

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'hover:text-red-600';
            removeBtn.textContent = '×';
            removeBtn.addEventListener('click', function () { tag.remove(); });
            tag.appendChild(removeBtn);

            container.insertBefore(tag, input);
        }
    });

    // ------------------------------------------------------------------
    // شمارنده‌ی زنده‌ی حداقل کاراکتر متن نظر
    // ------------------------------------------------------------------
    document.querySelectorAll('textarea[data-min-length]').forEach(function (textarea) {
        var min = parseInt(textarea.dataset.minLength, 10) || 0;
        var counter = document.getElementById(textarea.dataset.counterTarget);
        if (!counter) return;

        function update() {
            var remaining = min - textarea.value.length;
            if (remaining > 0) {
                counter.textContent = 'حداقل ' + min + ' کاراکتر نیاز است. (' + remaining + ' کاراکتر باقیمانده)';
                counter.classList.add('text-gray-400');
                counter.classList.remove('text-success');
            } else {
                counter.textContent = 'متن شما آماده ارسال است.';
                counter.classList.remove('text-gray-400');
                counter.classList.add('text-success');
            }
        }
        textarea.addEventListener('input', update);
        update();
    });

    // ------------------------------------------------------------------
    // ویجت سفارشی آپلود تصویر: به‌جای متن انگلیسیِ اینپوت فایل پیش‌فرض،
    // دکمه فارسی + پیش‌نمایش تصویرهای انتخاب‌شده با امکان حذف تکی.
    // مارک‌آپ (دکمه/پیش‌نمایش/قالب بج) از قبل در HTML نوشته شده تا کلاس‌های
    // تیلویند در اسکن ساخت CSS دیده شوند؛ این اسکریپت فقط رفتار را وصل می‌کند.
    // ------------------------------------------------------------------
    document.querySelectorAll('.review-image-widget').forEach(function (widget) {
        const input = widget.querySelector('.review-image-input');
        const btn = widget.querySelector('.review-image-add-btn');
        const previewRow = widget.querySelector('.review-image-preview');
        const thumbTemplate = widget.querySelector('.review-image-thumb-template');
        if (!input || !btn || !previewRow || !thumbTemplate) return;

        const maxFiles = parseInt(widget.dataset.max || '3', 10);
        let selectedFiles = [];

        btn.addEventListener('click', function () { input.click(); });

        input.addEventListener('change', function () {
            const incoming = Array.from(input.files || []);
            selectedFiles = selectedFiles.concat(incoming).slice(0, maxFiles);
            syncInput();
            renderPreviews();
        });

        function syncInput() {
            const dt = new DataTransfer();
            selectedFiles.forEach(function (f) { dt.items.add(f); });
            input.files = dt.files;
        }

        function renderPreviews() {
            previewRow.innerHTML = '';
            selectedFiles.forEach(function (file, idx) {
                const node = thumbTemplate.content.cloneNode(true);
                const img = node.querySelector('img');
                img.src = URL.createObjectURL(file);
                node.querySelector('.review-image-remove').addEventListener('click', function () {
                    selectedFiles.splice(idx, 1);
                    syncInput();
                    renderPreviews();
                });
                previewRow.appendChild(node);
            });
            btn.style.display = selectedFiles.length >= maxFiles ? 'none' : '';
        }
    });

    // ------------------------------------------------------------------
    // ارسال فرم نظر (ثبت/ویرایش) با AJAX: اگر اعتبارسنجی (ستاره/طول متن)
    // در سمت کاربر رد شود یا سرور خطا برگرداند، هیچ رفرش/جابه‌جایی‌ای رخ
    // نمی‌دهد و تمام مقادیر واردشده (متن، تگ‌ها، تصاویر انتخاب‌شده) باقی می‌مانند.
    // ------------------------------------------------------------------
    document.querySelectorAll('.review-ajax-form').forEach(function (form) {
        const warningBox = form.querySelector('.review-form-warning');
        const minLength = parseInt((form.querySelector('textarea[data-min-length]') || {}).dataset ? form.querySelector('textarea[data-min-length]').dataset.minLength : '4', 10) || 4;

        function showWarning(message) {
            if (!warningBox) return;
            warningBox.textContent = message;
            warningBox.classList.remove('hidden');
            warningBox.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }

        function hideWarning() {
            if (warningBox) warningBox.classList.add('hidden');
        }

        function clientValidate() {
            const rating = form.querySelector('input[name="rating"]:checked');
            if (!rating) {
                showWarning('لطفاً یک امتیاز بین ۱ تا ۵ ستاره انتخاب کنید.');
                return false;
            }
            const body = form.querySelector('textarea[name="body"]');
            if (body && body.value.trim().length < minLength) {
                showWarning('متن نظر باید حداقل ' + minLength + ' کاراکتر باشد.');
                return false;
            }
            return true;
        }

        form.addEventListener('submit', function (e) {
            e.preventDefault();
            hideWarning();
            if (!clientValidate()) return;

            const submitBtn = form.querySelector('button[type="submit"]');
            if (submitBtn) submitBtn.disabled = true;

            fetch(form.action, {
                method: 'POST',
                body: new FormData(form),
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            })
                .then(function (res) { return res.json().then(function (data) { return { status: res.status, data: data }; }); })
                .then(function (result) {
                    if (result.data.ok) {
                        window.location.href = result.data.redirect;
                    } else {
                        showWarning(result.data.message || 'خطایی رخ داد. لطفاً دوباره تلاش کنید.');
                        if (submitBtn) submitBtn.disabled = false;
                    }
                })
                .catch(function () {
                    showWarning('ارتباط با سرور برقرار نشد. اتصال اینترنت خود را بررسی کنید.');
                    if (submitBtn) submitBtn.disabled = false;
                });
        });
    });
});
