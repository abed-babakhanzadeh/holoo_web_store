/* توی فرم «اطلاع بده وقتی موجود شد» (stock_alert_box.html)، با انتخاب کانال «ایمیل» فیلد
   ایمیل نمایش داده می‌شود و با «پیامک» دوباره مخفی. با event delegation روی document چون
   این فرم با htmx جایگزین می‌شود (outerHTML) و نیازی به دوباره بستن listener بعد از هر
   swap نیست. */
(function () {
    'use strict';
    document.addEventListener('change', function (event) {
        if (!event.target.classList || !event.target.classList.contains('stock-alert-channel')) return;
        var target = document.querySelector(event.target.dataset.target);
        if (!target) return;
        target.classList.toggle('hidden', event.target.value !== 'email');
    });
})();
