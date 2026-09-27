/* آکاردئون واقعیِ فیلدست‌های «تنظیمات سایت»: فقط یکی هم‌زمان باز بماند و باز/بسته‌شدن نرم
   (انیمیشن ارتفاع) باشد - جنگو خودش هر <details> را کاملاً مستقل و بدون انیمیشن مدیریت می‌کند.
   بدون این اسکریپت (یا اگر خطا بدهد)، همان رفتار پیش‌فرض native <details> کار می‌کند و فرم
   کاملاً سالم می‌ماند؛ این فقط یک بهبود ظاهری روی رفتار درست‌کارِ مرورگر است. */
(function () {
    'use strict';

    var ANIMATION_MS = 220;

    function summaryHeight(details) {
        var summary = details.querySelector(':scope > summary');
        return summary ? summary.getBoundingClientRect().height : 0;
    }

    function clearInlineState(details) {
        details.style.height = '';
        details.style.overflow = '';
        details.style.transition = '';
    }

    function animateOpen(details) {
        details.setAttribute('open', '');
        var target = details.scrollHeight;
        details.style.overflow = 'hidden';
        details.style.height = summaryHeight(details) + 'px';
        // یک فریم صبر تا مرورگر مقدار اولیه را واقعاً اعمال کند، وگرنه transition اجرا نمی‌شود
        requestAnimationFrame(function () {
            details.style.transition = 'height ' + ANIMATION_MS + 'ms ease';
            details.style.height = target + 'px';
        });
        window.setTimeout(function () { clearInlineState(details); }, ANIMATION_MS + 30);
    }

    function animateClose(details) {
        var current = details.getBoundingClientRect().height;
        details.style.overflow = 'hidden';
        details.style.height = current + 'px';
        requestAnimationFrame(function () {
            details.style.transition = 'height ' + ANIMATION_MS + 'ms ease';
            details.style.height = summaryHeight(details) + 'px';
        });
        window.setTimeout(function () {
            details.removeAttribute('open');
            clearInlineState(details);
        }, ANIMATION_MS + 30);
    }

    document.addEventListener('DOMContentLoaded', function () {
        var allDetails = Array.prototype.slice.call(
            document.querySelectorAll('fieldset.module.collapse > details')
        );
        if (!allDetails.length) return;

        // پیش‌فرض: فقط اولین بخش باز بماند، بقیه بسته
        allDetails.forEach(function (d, index) {
            if (index !== 0) d.removeAttribute('open');
        });

        allDetails.forEach(function (details) {
            var summary = details.querySelector(':scope > summary');
            if (!summary) return;
            summary.addEventListener('click', function (e) {
                e.preventDefault();
                var isOpen = details.hasAttribute('open');
                if (isOpen) {
                    animateClose(details);
                    return;
                }
                allDetails.forEach(function (other) {
                    if (other !== details && other.hasAttribute('open')) animateClose(other);
                });
                animateOpen(details);
            });
        });
    });
})();
