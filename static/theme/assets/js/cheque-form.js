/*
 * فرم «ثبت اطلاعات چک»: اعتبارسنجی سمت کاربر (فقط راحتی؛ اعتبار اصلی، تشخیص نوع واقعی فایل و حذف EXIF سمت سرور است)، پیش‌نمایش
 * تصاویر انتخابی با امکان حذف هر کدام، تبدیل ارقام فارسی شناسه‌ی صیادی به لاتین، و جلوگیری از ارسال دوباره‌ی ناخواسته.
 * بدون کتابخانه؛ همه‌ی متن‌های پویا با textContent درج می‌شوند.
 */
(function () {
    'use strict';

    var form = document.getElementById('cheque-form');
    if (!form) { return; }

    var MAX_IMAGES = parseInt(form.getAttribute('data-max-images'), 10) || 5;
    var MAX_BYTES = (parseInt(form.getAttribute('data-max-mb'), 10) || 5) * 1024 * 1024;
    var TYPES = ['image/jpeg', 'image/png', 'image/webp'];

    var sayadi = document.getElementById('id_sayadi_id');
    var fileInput = document.getElementById('id_images');
    var previews = document.getElementById('chq-previews');
    var submit = document.getElementById('chq-submit');
    var chosen = [];                                         // فایل‌های انتخاب‌شده (قابل حذف تکی)

    function toLatin(text) {
        return String(text || '').replace(/[۰-۹]/g, function (c) { return c.charCodeAt(0) - 0x06F0; })
            .replace(/[٠-٩]/g, function (c) { return c.charCodeAt(0) - 0x0660; });
    }

    function showError(id, message) {
        var box = document.getElementById(id);
        if (!box) { return; }
        box.textContent = message || '';
        box.hidden = !message;
    }

    function syncInput() {
        // فایل‌های باقی‌مانده را دوباره در همان input می‌گذاریم تا با فرم ارسال شوند
        try {
            var transfer = new DataTransfer();
            chosen.forEach(function (file) { transfer.items.add(file); });
            fileInput.files = transfer.files;
        } catch (error) { /* مرورگر قدیمی: همان انتخاب اصلی ارسال می‌شود */ }
    }

    function renderPreviews() {
        previews.textContent = '';
        chosen.forEach(function (file, index) {
            var box = document.createElement('div');
            box.className = 'chq-preview';
            var img = document.createElement('img');
            img.alt = 'پیش‌نمایش تصویر چک';
            img.src = URL.createObjectURL(file);
            img.onload = function () { URL.revokeObjectURL(img.src); };
            var remove = document.createElement('button');
            remove.type = 'button';
            remove.textContent = '×';
            remove.setAttribute('aria-label', 'حذف تصویر');
            remove.addEventListener('click', function () { chosen.splice(index, 1); syncInput(); renderPreviews(); });
            box.appendChild(img);
            box.appendChild(remove);
            previews.appendChild(box);
        });
    }

    // صفحه‌ی اصلاح: تصاویر موجودِ حذف‌نشده هم در سقف ۵ تصویر شمرده می‌شوند
    function keptCount() {
        return form.querySelectorAll('input[name="remove_images"]:not(:checked)').length;
    }

    fileInput.addEventListener('change', function () {
        var problem = '';
        Array.prototype.forEach.call(fileInput.files, function (file) {
            if (chosen.length + keptCount() >= MAX_IMAGES) { problem = 'حداکثر ' + MAX_IMAGES + ' تصویر برای هر چک مجاز است.'; return; }
            if (TYPES.indexOf(file.type) === -1) { problem = 'فقط تصویر JPG، PNG یا WebP مجاز است.'; return; }
            if (file.size > MAX_BYTES) { problem = 'حجم هر تصویر حداکثر ' + (MAX_BYTES / 1048576) + ' مگابایت است.'; return; }
            chosen.push(file);
        });
        showError('err_images', problem);
        syncInput();
        renderPreviews();
    });

    sayadi.addEventListener('input', function () {
        var digits = toLatin(sayadi.value).replace(/[^0-9]/g, '').slice(0, 16);
        if (sayadi.value !== digits) { sayadi.value = digits; }
        if (digits.length === 16) { showError('err_sayadi_id', ''); }       // خطای قبلی با تکمیل شدن ۱۶ رقم پاک می‌شود
    });

    form.addEventListener('submit', function (event) {
        var ok = true;
        var id = toLatin(sayadi.value).replace(/\D/g, '');
        if (id.length !== 16) { showError('err_sayadi_id', 'شناسه‌ی صیادی باید دقیقاً ۱۶ رقم باشد.'); ok = false; } else { showError('err_sayadi_id', ''); }
        if (!chosen.length && !keptCount() && !(fileInput.files && fileInput.files.length)) { showError('err_images', 'حداقل یک تصویر از چک لازم است.'); ok = false; }
        if (!ok) { event.preventDefault(); return; }
        submit.disabled = true;                              // کلیک دوباره‌ی ناخواسته، چک تکراری نسازد
        submit.textContent = 'در حال ثبت…';
    });
})();
