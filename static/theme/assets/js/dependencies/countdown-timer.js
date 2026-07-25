/* شمارش معکوس باکس شگفت‌انگیز: روی هر عنصر دارای data-deal-ends فعال می‌شود، هر ثانیه
   روز/ساعت/دقیقه/ثانیه‌ی باقی‌مانده تا آن تاریخ را در فرزندان [data-unit] می‌نویسد؛
   اگر تاریخ گذشته باشد، عنصر مخفی می‌شود. */
(function () {
    'use strict';

    function pad(n) {
        return (n < 10 ? '0' : '') + n;
    }

    function tick(box) {
        var target = new Date(box.dataset.dealEnds).getTime();
        var diff = target - Date.now();
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
