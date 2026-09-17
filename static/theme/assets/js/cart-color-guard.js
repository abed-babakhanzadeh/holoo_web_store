/* دکمه‌ی «افزودن به سبد» برای محصول رنگ‌بندی‌شده دیگر تا انتخاب رنگ، غیرفعال/کم‌رنگ نمی‌ماند
   (تجربه‌ی کاربری بد بود). به‌جایش همیشه کلیک‌پذیر است؛ اگر هنوز رنگی انتخاب نشده، این تابع
   درخواست واقعی افزودن را متوقف می‌کند و با یک قاب چشمک‌زن توجه کاربر را به دایره‌های رنگ
   جلب می‌کند. باید از onclick درون خودِ HTML صدا زده شود (نه addEventListener بعد از لود)
   تا تضمین شود قبل از listener کلیک خودِ htmx روی همان دکمه اجرا می‌شود. */
(function () {
    'use strict';

    window.holooGuardColorSelection = function (event, btn) {
        if (btn.dataset.hasColors !== 'true' || btn.dataset.selectedColor) {
            return true; // نیازی به انتخاب رنگ نیست یا قبلاً انتخاب شده؛ htmx خودش درخواست را بفرستد
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        window.holooFlashColorSelector(btn.dataset.colorTarget);
        return false;
    };

    window.holooFlashColorSelector = function (selector) {
        var target = selector && document.querySelector(selector);
        if (!target) return;
        target.classList.remove('color-selector-flash'); // ری‌استارت انیمیشن اگر قبلاً در جریان بود
        void target.offsetWidth; // force reflow تا حذف/افزودن دوباره‌ی کلاس واقعاً انیمیشن را از نو شروع کند
        target.classList.add('color-selector-flash');
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        window.setTimeout(function () { target.classList.remove('color-selector-flash'); }, 1600);
    };
})();
