/* تنظیمات سایت ← «قیمت برای کاربران مهمان»: فقط فیلدهای مربوط به حالت انتخاب‌شده نمایش داده می‌شوند.
   بدون این اسکریپت (یا اگر خطا بدهد) همه‌ی فیلدها دیده می‌شوند و فرم درست کار می‌کند؛ اعتبارسنجی اصلی سمت سرور است. */
(function () {
    'use strict';

    // حالت ← فیلدهایی که نمایش داده می‌شوند
    var VISIBLE = {
        hide_price: ['guest_price_hidden_message'],
        price_level: ['guest_price_level'],
        calculated_price: ['guest_price_level', 'guest_adjustment_type', 'guest_adjustment_value', 'guest_price_rounding_step']
    };
    var CONTROLLED = ['guest_price_level', 'guest_adjustment_type', 'guest_adjustment_value',
                      'guest_price_rounding_step', 'guest_price_hidden_message'];

    function apply(select) {
        var shown = VISIBLE[select.value];
        if (!shown) return;                                   // مقدار ناشناخته: همه را نشان بده
        CONTROLLED.forEach(function (name) {
            var row = document.querySelector('.form-row.field-' + name);
            if (row) row.style.display = shown.indexOf(name) === -1 ? 'none' : '';
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var select = document.getElementById('id_guest_pricing_mode');
        if (!select) return;
        apply(select);
        select.addEventListener('change', function () { apply(select); });
    });
})();
