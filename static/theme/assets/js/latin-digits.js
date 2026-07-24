/* روی هر input[data-latin-digits]، ارقام فارسی/عربی را همان لحظه‌ی تایپ به رقم لاتین تبدیل می‌کند
   (شماره موبایل، کد تایید، کد امنیتی) تا با صفحه‌کلید فارسی هم فرم بدون خطا ارسال شود. */
(function () {
    'use strict';

    var DIGIT_MAP = {
        '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4', '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
        '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9'
    };
    var DIGIT_RE = /[۰-۹٠-٩]/g;

    function toLatinDigits(value) {
        return value.replace(DIGIT_RE, function (ch) { return DIGIT_MAP[ch] || ch; });
    }

    function bind(input) {
        input.dataset.latinDigitsBound = '1';
        input.addEventListener('input', function () {
            var converted = toLatinDigits(input.value);
            if (converted !== input.value) {
                var pos = input.selectionStart;
                input.value = converted;
                if (pos !== null) input.setSelectionRange(pos, pos);
            }
        });
    }

    function init(root) {
        (root || document).querySelectorAll('[data-latin-digits]').forEach(function (input) {
            if (!input.dataset.latinDigitsBound) bind(input);
        });
    }

    document.addEventListener('DOMContentLoaded', function () { init(document); });
    document.addEventListener('htmx:afterSettle', function (e) { init(e.target); });
})();
