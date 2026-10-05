/**
 * محافظ سراسری htmx (بعد از htmx.min.js لود می‌شود).
 *
 * ۱) پاسخِ «سند کامل HTML» هرگز داخل یک المان swap نمی‌شود.
 *    وقتی درخواست htmx (مثلاً «افزودن به سبد» یا قلب) ریدایرکت می‌شود (کاربر وارد نشده/تأییدنشده، نشست منقضی، ...)،
 *    مرورگر ریدایرکت را دنبال می‌کند و کل صفحه‌ی مقصد (هدر، محتوا، فوتر و اسکریپت‌ها) داخل همان دکمه می‌نشست: «صفحه در صفحه»،
 *    و چون اسکریپت‌های سراسری دوباره اجرا می‌شدند خطای «Identifier 'swiper' has already been declared» می‌آمد.
 *    حالا اگر پاسخ ریدایرکت شده بود به آدرس مقصد می‌رویم (ناوبری عادی)، وگرنه چیزی عوض نمی‌شود.
 *    استثنا: عنصری که خودش hx-select دارد (مثل مرتب‌سازی نظرات) عمداً سند کامل می‌گیرد و فقط قطعه‌ای از آن را برمی‌دارد.
 *
 * ۲) توست ساده برای پیام‌هایی که سرور با هدر HX-Trigger می‌فرستد: {"holooToast": {"message": "...", "type": "error"}}.
 *    (سرور برای کاربرِ واردشده‌ی تأییدنشده به‌جای ریدایرکت فقط همین را برمی‌گرداند و دکمه عوض نمی‌شود.)
 */
(function () {
    var FULL_DOCUMENT = /^\s*(?:<!doctype\s+html|<html[\s>])/i;

    document.addEventListener('htmx:beforeSwap', function (evt) {
        var detail = evt.detail || {};
        var xhr = detail.xhr;
        if (!xhr || typeof xhr.responseText !== 'string') return;
        if (detail.elt && detail.elt.closest && detail.elt.closest('[hx-select]')) return;
        if (!FULL_DOCUMENT.test(xhr.responseText)) return;

        detail.shouldSwap = false;
        var url = xhr.responseURL;
        if (url && url !== window.location.href) window.location.assign(url);
    });

    var COLORS = {success: '#15803d', error: '#b91c1c', info: '#1d4ed8'};

    function showToast(message, type) {
        if (!message || !document.body) return;
        var toast = document.createElement('div');
        toast.setAttribute('role', 'status');
        toast.textContent = message;
        toast.style.cssText = 'position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:100000;max-width:90vw;' +
            'padding:10px 18px;border-radius:10px;color:#fff;font-size:14px;font-weight:700;box-shadow:0 6px 20px rgba(0,0,0,.25);' +
            'background:' + (COLORS[type] || COLORS.info);
        document.body.appendChild(toast);
        setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 4000);
    }

    document.addEventListener('holooToast', function (evt) {
        var detail = evt.detail || {};
        showToast(detail.message, detail.type);
    });
})();
