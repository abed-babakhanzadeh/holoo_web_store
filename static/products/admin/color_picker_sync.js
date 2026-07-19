(function () {
    var HEX_RE = /^#[0-9A-Fa-f]{6}$/;

    function pair(el) {
        var wrap = el.closest('.color-picker-wrap');
        if (!wrap) return null;
        return {
            wrap: wrap,
            text: wrap.querySelector('.color-hex-input'),
            picker: wrap.querySelector('.color-hex-picker')
        };
    }

    // هماهنگی زنده: چه کاربر رنگ را با انتخابگر بصری عوض کند، چه کد را تایپ کند
    document.addEventListener('input', function (e) {
        var p = pair(e.target);
        if (!p || !p.text || !p.picker) return;

        if (e.target === p.picker) {
            p.text.value = p.picker.value;
        } else if (e.target === p.text && HEX_RE.test(p.text.value)) {
            p.picker.value = p.text.value;
        }
    });

    // شبکه‌ی ایمنی: درست قبل از ارسال فرم، اگر فیلد متنی به هر دلیلی خالی/نامعتبر مانده بود،
    // مقدار انتخابگر رنگ (که همیشه یک کد معتبر ۷ کاراکتری دارد) در آن نشانده می‌شود
    document.addEventListener('submit', function () {
        document.querySelectorAll('.color-picker-wrap').forEach(function (wrap) {
            var text = wrap.querySelector('.color-hex-input');
            var picker = wrap.querySelector('.color-hex-picker');
            if (text && picker && !HEX_RE.test(text.value)) {
                text.value = picker.value;
            }
        });
    }, true);
})();
