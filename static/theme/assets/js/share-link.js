/*
 * دکمه‌ی «کپی لینک» مودال اشتراک‌گذاری محصول (templates/products/product_detail.html).
 *
 * بازخورد: دکمه ۲٫۵ ثانیه «کپی شد ✓» و سبز می‌شود و بعد به حالت اولیه برمی‌گردد؛ هم‌زمان توست سراسری سایت (showToast در
 * stock-alert.js) پیام می‌دهد. کپی: navigator.clipboard.writeText فقط در صفحه‌ی امن (HTTPS / localhost)؛ در غیر این صورت یا اگر
 * رد شد (مجوز/مرورگر قدیمی) فال‌بک سنتی document.execCommand('copy') با یک textarea موقت. اگر هر دو شکست بخورند، متن لینک
 * انتخاب می‌شود و پیام خطا می‌آید تا کاربر با Ctrl+C دستی کپی کند.
 */
(function () {
    'use strict';

    var RESET_MS = 2500;
    var LABEL_IDLE = 'کپی لینک';
    var LABEL_DONE = 'کپی شد ✓';
    var LABEL_FAIL = 'کپی نشد';
    var timers = new WeakMap();

    function toast(message, type) {
        if (typeof window.showToast === 'function') window.showToast(message, type);
    }

    function legacyCopy(text) {
        var area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none;';
        document.body.appendChild(area);
        area.select();
        if (area.setSelectionRange) area.setSelectionRange(0, text.length);
        var ok = false;
        try { ok = !!document.execCommand('copy'); } catch (e) { ok = false; }
        document.body.removeChild(area);
        return ok;
    }

    function copyText(text, onDone, onFail) {
        var fallback = function () { (legacyCopy(text) ? onDone : onFail)(); };
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(onDone, fallback);
        } else {
            fallback();
        }
    }

    function setState(btn, state) {
        var label = state === 'done' ? LABEL_DONE : state === 'fail' ? LABEL_FAIL : LABEL_IDLE;
        btn.textContent = label;
        btn.classList.toggle('is-copied', state === 'done');
        btn.classList.toggle('is-failed', state === 'fail');
        var previous = timers.get(btn);
        if (previous) clearTimeout(previous);
        if (state !== 'idle') {
            timers.set(btn, setTimeout(function () { setState(btn, 'idle'); }, RESET_MS));
        }
    }

    window.copyShareLink = function (btn) {
        var input = document.getElementById('shareUrlInput');
        btn = btn || document.getElementById('shareCopyBtn');
        if (!input || !btn) return;
        copyText(input.value, function () {
            setState(btn, 'done');
            toast('لینک محصول با موفقیت در کلیپ‌بورد کپی شد', 'success');
        }, function () {
            setState(btn, 'fail');
            input.focus();
            input.select();                         // کاربر با Ctrl+C دستی کپی کند
            toast('کپی خودکار انجام نشد؛ لینک انتخاب شد، با Ctrl+C کپی کنید', 'error');
        });
    };
})();
