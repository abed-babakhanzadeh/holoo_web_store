/* شمارش معکوس باکس شگفت‌انگیز: روی هر عنصر دارای data-deal-ends فعال می‌شود، هر ثانیه
   روز/ساعت/دقیقه/ثانیه‌ی باقی‌مانده تا آن تاریخ را در فرزندان [data-unit] می‌نویسد؛
   اگر تاریخ گذشته باشد، عنصر مخفی می‌شود.

   ساعت مرجع «زمان سرور» است، نه ساعت دستگاه: سرور لحظه‌ی رندر را در data-server-now می‌فرستد و ادامه‌ی زمان با
   performance.now() (ساعت یکنواخت مرورگر که با تغییر ساعت سیستم/منطقه‌ی زمانی عوض نمی‌شود) جلو می‌رود.
   پس عقب/جلو بردن ساعت دستگاه نه تایمر را دستکاری می‌کند و نه چیزی را باز می‌کند؛ این تایمر فقط نمایشی است و
   اعتبار واقعی تخفیف (نمایش قیمت، سبد، تسویه و ثبت سفارش) همیشه سمت سرور و با ساعت سرور سنجیده می‌شود. */
(function () {
    'use strict';

    function pad(n) {
        return (n < 10 ? '0' : '') + n;
    }

    /* «اکنونِ سرور» برای این عنصر: زمان سرور در لحظه‌ی رندر + مدت سپری‌شده روی ساعت یکنواخت */
    function serverNow(box) {
        var base = box._serverBase;
        if (base === undefined) {
            var parsed = Date.parse(box.dataset.serverNow || '');
            box._serverBase = base = isNaN(parsed) ? null : parsed;
            box._monotonicStart = performance.now();
        }
        if (base === null) return Date.now();      // قالبِ بدون data-server-now؛ فقط برای سازگاری
        return base + (performance.now() - box._monotonicStart);
    }

    function tick(box) {
        var target = new Date(box.dataset.dealEnds).getTime();
        var diff = target - serverNow(box);
        if (!diff || diff <= 0) {
            box.style.display = 'none';
            return false;
        }
        var days = Math.floor(diff / 86400000);
        var hours = Math.floor((diff % 86400000) / 3600000);
        var minutes = Math.floor((diff % 3600000) / 60000);
        var seconds = Math.floor((diff % 60000) / 1000);

        var daysEl = box.querySelector('[data-unit="days"]');
        var hoursEl = box.querySelector('[data-unit="hours"]');
        var minutesEl = box.querySelector('[data-unit="minutes"]');
        var secondsEl = box.querySelector('[data-unit="seconds"]');
        if (daysEl) daysEl.textContent = pad(days);
        if (hoursEl) hoursEl.textContent = pad(hours);
        if (minutesEl) minutesEl.textContent = pad(minutes);
        if (secondsEl) secondsEl.textContent = pad(seconds);
        return true;
    }

    function init(root) {
        (root || document).querySelectorAll('[data-deal-ends]').forEach(function (box) {
            if (box.dataset.countdownBound) return;
            box.dataset.countdownBound = '1';
            if (!tick(box)) return;
            var interval = setInterval(function () {
                if (!tick(box)) clearInterval(interval);
            }, 1000);
        });
    }

    document.addEventListener('DOMContentLoaded', function () { init(document); });
    document.addEventListener('htmx:afterSettle', function (e) { init(e.target); });
})();
