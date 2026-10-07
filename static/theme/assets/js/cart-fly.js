/*
 * افکت «پرواز محصول به سبد خرید» + پنجره‌ی کوچک سبد.
 *
 * با هر افزودن موفق (پاسخ ۲۰۰ از cart:add_to_cart) عکس محصول از قابش بیرون می‌آید، یک سبد از پایین (در موبایل: از بالا)
 * می‌رسد، عکس داخلش می‌افتد و سبد به آیکون سبد خرید (دسکتاپ: هدر؛ موبایل: منوی پایین) پرواز می‌کند و محو می‌شود؛ بعد پنجره‌ی
 * سبد چسبیده به آیکون چند ثانیه باز می‌ماند (تصویر کالاهای سبد، تعداد روی عکس، جمع کل و دکمه‌ها).
 *
 * فقط وقتی کار می‌کند که <body data-cart-fly="1"> باشد (تنظیمات سایت ← افکت سبد خرید). دکمه‌های افزودن با
 * data-cart-fly-add علامت خورده‌اند؛ دکمه‌های داخل آفکانواس سبد علامت ندارند و انیمیشن نمی‌گیرند. داده‌ی پنجره از
 * #cart-fly-data (cart/partials/cart_fly_data.html) می‌آید که با هر پاسخ nav_cart تازه می‌شود.
 * خطا در این فایل هرگز نباید مسیر افزودن به سبد را بشکند؛ همه‌چیز در try/catch است.
 */
(function () {
    'use strict';

    var CART_ICON = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.6" stroke="currentColor">' +
        '<path stroke-linecap="round" stroke-linejoin="round" d="M2.25 3h1.386c.51 0 .955.343 1.087.835l.383 1.437M7.5 14.25a3 3 0 0 0-3 3h15.75m-12.75-3h11.218c1.121-2.3 2.1-4.684 2.924-7.138a60.114 60.114 0 0 0-16.536-1.84M7.5 14.25 5.106 5.272M6 20.25a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0Zm12.75 0a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0Z"/></svg>';

    var POPUP_MS = 4200;           // مدت باز ماندن پنجره
    var snapshots = new WeakMap(); // XHR -> اطلاعات دکمه (پیش از اینکه htmx دکمه را با HTML تازه عوض کند)
    var flights = 0;               // پروازهای فعال؛ پروازِ هم‌زمان فقط «عکس» می‌پرد (کلیک‌های پیاپی روی +)
    var layer = null;
    var popup = null;
    var popupTimer = null;
    var popupHover = false;
    var iconHover = false;        // ماوس روی آیکون سبد (حالت هاور)
    var popupMode = 'fly';        // 'fly' = بعد از افزودن (بسته‌شدن خودکار)، 'hover' = با ماوس روی آیکون
    var hoverOpenTimer = null;
    var hoverCloseTimer = null;
    var pendingHighlight = null;

    function enabled() {
        return document.body && document.body.getAttribute('data-cart-fly') === '1';
    }

    // «کاهش حرکتِ» سیستم‌عامل فقط وقتی رعایت می‌شود که در تنظیمات سایت روشن شده باشد (نگاه کنید توضیح فیلد
    // SiteSettings.cart_fly_respect_reduced_motion)
    function reduced() {
        if (document.body.getAttribute('data-cart-fly-respect-motion') !== '1') { return false; }
        return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    }

    function rand(min, max) { return min + Math.random() * (max - min); }
    function pick(list) { return list[Math.floor(Math.random() * list.length)]; }
    function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
    function visible(el) {
        if (!el) { return false; }
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }

    /* ---------- هدف: آیکون سبدِ دیدنی (دسکتاپ: هدر، موبایل: منوی پایین) ---------- */
    function cartTarget() {
        var ids = ['nav-cart-badge', 'nav-cart-badge-mobile'];
        for (var i = 0; i < ids.length; i++) {
            var badge = document.getElementById(ids[i]);
            if (badge && badge.parentElement && visible(badge.parentElement)) {
                var holder = badge.parentElement;
                var icon = holder.querySelector('svg') || holder;
                return {holder: holder, icon: icon, badge: badge, mobile: ids[i] === 'nav-cart-badge-mobile'};
            }
        }
        return null;
    }

    function center(rect) {
        return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
    }

    function ensureLayer() {
        if (!layer || !layer.isConnected) {
            layer = document.createElement('div');
            layer.className = 'cf-layer';
            layer.setAttribute('aria-hidden', 'true');
            document.body.appendChild(layer);
        }
        return layer;
    }

    function dispose() {
        if (layer && flights === 0 && layer.parentNode) {
            layer.parentNode.removeChild(layer);
            layer = null;
        }
    }

    /* ---------- گرفتن اطلاعات دکمه، قبل از تعویض DOM ---------- */
    function snapshot(button) {
        var scope = button.closest('article');
        var img = scope ? scope.querySelector('img') : null;
        if (!visible(img)) { img = document.querySelector('.product-zoom-img'); }
        var rect = (visible(img) ? img : button).getBoundingClientRect();
        return {
            rect: {left: rect.left, top: rect.top, width: rect.width, height: rect.height},
            src: button.getAttribute('data-fly-image') || (img && img.getAttribute('src')) || '',
            name: button.getAttribute('data-fly-name') || '',
            productId: button.getAttribute('data-fly-product') || ''
        };
    }

    /* ---------- پنجره‌ی کوچک سبد ---------- */
    function closePopup(immediate) {
        clearTimeout(popupTimer);
        popupTimer = null;
        if (!popup) { return; }
        var el = popup;
        popup = null;
        popupHover = false;
        if (immediate) {
            if (el.parentNode) { el.parentNode.removeChild(el); }
            return;
        }
        el.classList.add('cf-closing');
        setTimeout(function () { if (el.parentNode) { el.parentNode.removeChild(el); } }, 230);
    }

    function armPopupTimer(ms) {
        clearTimeout(popupTimer);
        popupTimer = setTimeout(function () {
            if (popupHover || iconHover) { armPopupTimer(1500); } else { closePopup(false); }
        }, ms);
    }

    // بسته‌شدن پنجره‌ی هاور: کمی مهلت تا ماوس از آیکون به پنجره برسد
    function scheduleHoverClose() {
        clearTimeout(hoverCloseTimer);
        hoverCloseTimer = setTimeout(function () {
            if (!iconHover && !popupHover) { closePopup(false); }
        }, 280);
    }

    function fillPopup() {
        var data = document.getElementById('cart-fly-data');
        if (!popup || !data) { return false; }
        if (!data.firstElementChild) {
            if (popupMode !== 'hover') { return false; }     // بعد از افزودن، سبد خالی معنا ندارد (داده هنوز نرسیده)
            popup.innerHTML = '<div class="cfp-empty"><span class="cfp-empty-icon">' + CART_ICON + '</span>' +
                '<p>سبد خرید شما خالی است.</p></div>';
            return true;
        }
        popup.innerHTML = data.innerHTML;
        if (pendingHighlight) {
            var item = popup.querySelector('.cfp-item[data-product-id="' + pendingHighlight + '"]');
            if (item) { item.classList.add('cf-new'); }
        }
        return true;
    }

    function positionPopup(target) {
        if (!popup || !target) { return; }
        var rect = target.icon.getBoundingClientRect();
        var c = center(rect);
        var vw = window.innerWidth;
        var vh = window.innerHeight;
        var width = popup.offsetWidth || 312;
        var left = clamp(c.x - width / 2, 8, vw - width - 8);
        popup.style.left = left + 'px';
        popup.style.setProperty('--cf-arrow-x', clamp(c.x - left, 22, width - 22) + 'px');
        popup.style.setProperty('--cf-origin-x', clamp(c.x - left, 0, width) + 'px');
        if (target.mobile) {
            // آیکون در منوی پایین است: پنجره بالای آن باز می‌شود
            popup.setAttribute('data-edge', 'bottom');
            popup.style.top = '';
            popup.style.bottom = (vh - rect.top + 14) + 'px';
        } else {
            popup.setAttribute('data-edge', 'top');
            popup.style.bottom = '';
            popup.style.top = (rect.bottom + 14) + 'px';
        }
        // اگر ارتفاعش از صفحه بیرون می‌زد، بیشینه‌ی ارتفاع و اسکرول
        popup.style.maxHeight = (vh - 24 - (target.mobile ? vh - rect.top : rect.bottom)) > 160 ? '' : (vh - 24) + 'px';
    }

    function openPopup(target, productId, mode) {
        popupMode = mode || 'fly';
        pendingHighlight = productId || null;
        closePopup(true);
        var el = document.createElement('div');
        el.className = 'cf-popup';
        el.setAttribute('role', 'dialog');
        el.setAttribute('aria-label', 'سبد خرید شما');
        el.addEventListener('mouseenter', function () { popupHover = true; clearTimeout(hoverCloseTimer); });
        el.addEventListener('mouseleave', function () {
            popupHover = false;
            if (popupMode === 'hover') { scheduleHoverClose(); } else { armPopupTimer(1400); }
        });
        el.addEventListener('click', function (event) {
            var action = event.target.closest('[data-action="open-cart"]');
            if (action) {
                closePopup(true);
                if (typeof window.toggleOffcanvas === 'function') { window.toggleOffcanvas('offcanvas-left'); }
            }
        });
        document.body.appendChild(el);
        popup = el;
        if (!fillPopup()) {
            // داده‌ی سبد هنوز نرسیده (پاسخ nav_cart در راه است)؛ تا رسیدنش پنجره‌ی خالی نشان نمی‌دهیم
            popup = null;
            el.parentNode.removeChild(el);
            popupWaiting = {target: target, productId: productId};
            setTimeout(function () { if (popupWaiting && popupWaiting.target === target) { popupWaiting = null; } }, 2500);
            return;
        }
        positionPopup(target);
        if (popupMode === 'fly') { armPopupTimer(POPUP_MS); }     // پنجره‌ی هاور با رفتن ماوس بسته می‌شود نه با تایمر
    }

    var popupWaiting = null;
    var lastTarget = null;

    /* ---------- انیمیشن ---------- */
    function animate(el, keyframes, options) {
        // Web Animations API؛ اگر نبود (مرورگر قدیمی) بلافاصله تمام‌شده فرض می‌شود
        if (!el.animate) { return Promise.resolve(); }
        var a = el.animate(keyframes, Object.assign({fill: 'forwards'}, options));
        return a.finished.catch(function () {});
    }

    function makeThumb(snap, size) {
        var c = center(snap.rect);
        var thumb = document.createElement('div');
        thumb.className = 'cf-thumb';
        thumb.style.width = size + 'px';
        thumb.style.height = size + 'px';
        thumb.style.left = (c.x - size / 2) + 'px';
        thumb.style.top = (c.y - size / 2) + 'px';
        var img = document.createElement('img');
        img.src = snap.src;
        img.alt = '';
        thumb.appendChild(img);
        ensureLayer().appendChild(thumb);
        return {el: thumb, x: c.x, y: c.y};
    }

    function bumpTarget(target) {
        [target.icon, target.badge].forEach(function (el) {
            if (!el) { return; }
            el.classList.remove('cf-bump');
            void el.offsetWidth;
            el.classList.add('cf-bump');
            setTimeout(function () { el.classList.remove('cf-bump'); }, 600);
        });
    }

    // پروازِ ساده: فقط عکس، مستقیم به آیکون (وقتی پروازِ کاملِ قبلی هنوز در جریان است)
    function quickFly(snap, target) {
        var size = clamp(Math.min(snap.rect.width, snap.rect.height) * 0.6, 40, 80);
        var thumb = makeThumb(snap, size);
        var t = center(target.icon.getBoundingClientRect());
        var dx = t.x - thumb.x;
        var dy = t.y - thumb.y;
        flights++;
        return animate(thumb.el, [
            {transform: 'translate(0,0) scale(1) rotate(0deg)', opacity: 1},
            {transform: 'translate(' + (dx * 0.5) + 'px,' + (dy * 0.5 - 60) + 'px) scale(.7) rotate(' + rand(-25, 25) + 'deg)', opacity: 1, offset: 0.55},
            {transform: 'translate(' + dx + 'px,' + dy + 'px) scale(.2) rotate(0deg)', opacity: 0.2}
        ], {duration: 520, easing: 'cubic-bezier(.45,.05,.55,.95)'}).then(function () {
            thumb.el.remove();
            flights--;
            bumpTarget(target);
            dispose();
        });
    }

    var POP_OUTS = [
        // «لیفت»: عکس کمی بالا می‌آید
        function (s) { return [{transform: 'translateY(0) scale(1) rotate(0deg)'}, {transform: 'translateY(-16px) scale(1.18) rotate(-4deg)'}, {transform: 'translateY(-6px) scale(1.1) rotate(2deg)'}]; },
        // «چرخش»: یک دور می‌چرخد
        function (s) { return [{transform: 'scale(1) rotate(0deg)'}, {transform: 'scale(1.25) rotate(200deg)'}, {transform: 'scale(1.12) rotate(360deg)'}]; },
        // «لرزش»: تکان خورده بیرون می‌آید
        function (s) { return [{transform: 'scale(1) rotate(0deg)'}, {transform: 'scale(1.16) rotate(-12deg)', offset: 0.35}, {transform: 'scale(1.16) rotate(9deg)', offset: 0.7}, {transform: 'scale(1.1) rotate(0deg)'}]; }
    ];

    function fullFly(snap, target) {
        var vw = window.innerWidth;
        var vh = window.innerHeight;
        var size = clamp(Math.min(snap.rect.width, snap.rect.height) * 0.7, 60, 130);
        var thumb = makeThumb(snap, size);
        var mobile = target.mobile;
        var basketSize = mobile ? 56 : 64;

        // سبد زیر عکس می‌ایستد (در صفحه می‌ماند)
        var bx = clamp(thumb.x, basketSize / 2 + 8, vw - basketSize / 2 - 8);
        var by = clamp(thumb.y + size / 2 + basketSize / 2 + 18, basketSize / 2 + 8, vh - basketSize / 2 - (mobile ? 90 : 24));
        var basket = document.createElement('div');
        basket.className = 'cf-basket';
        basket.style.width = basket.style.height = basketSize + 'px';
        basket.style.left = (bx - basketSize / 2) + 'px';
        basket.style.top = (by - basketSize / 2) + 'px';
        basket.innerHTML = CART_ICON;
        basket.style.opacity = '0';
        ensureLayer().appendChild(basket);

        flights++;
        var side = pick([-1, 0, 1]);                                    // سبد از وسط یا از یکی از دو گوشه می‌آید
        var startY = mobile ? -(by + basketSize + 40) : (vh - by + basketSize + 40);
        var startX = side * Math.min(vw * 0.35, 220);
        var popOut = pick(POP_OUTS)(snap);

        // ۱) عکس از قابش بیرون می‌آید
        return animate(thumb.el, popOut, {duration: Math.round(rand(300, 420)), easing: 'cubic-bezier(.2,.8,.3,1)'})
            // ۲) سبد از پایین/بالا (با کمی برگشت) می‌رسد
            .then(function () {
                basket.style.opacity = '1';
                return animate(basket, [
                    {transform: 'translate(' + startX + 'px,' + startY + 'px) rotate(' + (side * -18) + 'deg) scale(.8)'},
                    {transform: 'translate(0,0) rotate(0deg) scale(1)'}
                ], {duration: Math.round(rand(480, 620)), easing: 'cubic-bezier(.34,1.45,.64,1)'});
            })
            // ۳) عکس داخل سبد می‌افتد و کوچک می‌شود
            .then(function () {
                var dx = bx - thumb.x;
                var dy = (by - 6) - thumb.y;
                return animate(thumb.el, [
                    {transform: 'translate(0,0) scale(1.1) rotate(0deg)', opacity: 1},
                    {transform: 'translate(' + (dx * 0.6) + 'px,' + (dy * 0.45 - 26) + 'px) scale(.7) rotate(' + rand(-30, 30) + 'deg)', opacity: 1, offset: 0.55},
                    {transform: 'translate(' + dx + 'px,' + dy + 'px) scale(' + (basketSize * 0.42 / size) + ') rotate(0deg)', opacity: 0.9}
                ], {duration: 430, easing: 'cubic-bezier(.5,0,.75,.4)'});
            })
            // ۴) سبد با محصول داخلش یک تکان می‌خورد
            .then(function () {
                thumb.el.remove();      // عکس داخل سبد جا شد (fill:forwards انیمیشن، opacity دستی را نادیده می‌گیرد)
                return animate(basket, [
                    {transform: 'translate(0,0) scale(1)'},
                    {transform: 'translate(0,0) scale(1.18,.86)'},
                    {transform: 'translate(0,-6px) scale(.94,1.1)'},
                    {transform: 'translate(0,0) scale(1)'}
                ], {duration: 300, easing: 'ease-out'});
            })
            // ۵) سبد به آیکون سبد پرواز می‌کند (دسکتاپ: بالا، موبایل: پایین) و محو می‌شود
            .then(function () {
                var t = center(target.icon.getBoundingClientRect());
                var dx = t.x - bx;
                var dy = t.y - by;
                var finalScale = clamp((target.icon.getBoundingClientRect().width * 0.85) / basketSize, 0.3, 0.8);
                var arc = mobile ? 40 : -40;     // وسط مسیر کمی به سمتِ «بیرون» می‌رود تا مسیر خمیده باشد
                var bend = pick([-1, 1]) * Math.min(120, Math.abs(dx) * 0.3 + 40);
                return animate(basket, [
                    {transform: 'translate(0,0) scale(1) rotate(0deg)', opacity: 1},
                    {transform: 'translate(' + (dx * 0.5 + bend) + 'px,' + (dy * 0.5 + arc) + 'px) scale(.8) rotate(' + (bend > 0 ? 12 : -12) + 'deg)', opacity: 1, offset: 0.55},
                    {transform: 'translate(' + dx + 'px,' + dy + 'px) scale(' + finalScale + ') rotate(0deg)', opacity: 0.95, offset: 0.92},
                    {transform: 'translate(' + dx + 'px,' + dy + 'px) scale(' + (finalScale * 0.6) + ') rotate(0deg)', opacity: 0}
                ], {duration: Math.round(rand(680, 820)), easing: 'cubic-bezier(.55,.05,.35,1)'});
            })
            .then(function () {
                basket.remove();
                thumb.el.remove();
                flights--;
                bumpTarget(target);
                dispose();
            });
    }

    function play(snap) {
        var target = cartTarget();
        if (!target) { return; }
        lastTarget = target;
        if (reduced() || !snap.src) {
            openPopup(target, snap.productId, 'fly');
            return;
        }
        var run = flights > 0 ? quickFly(snap, target) : fullFly(snap, target);
        run.then(function () {
            // هدف ممکن است حین پرواز (تغییر اندازه/اسکرول) جابه‌جا شده باشد
            var again = cartTarget() || target;
            lastTarget = again;
            openPopup(again, snap.productId, 'fly');
        }).catch(function () { flights = Math.max(0, flights - 1); });
    }

    /* ---------- اتصال به htmx ---------- */
    document.addEventListener('htmx:beforeRequest', function (event) {
        try {
            var elt = event.detail && event.detail.elt;
            if (elt && elt.hasAttribute && elt.hasAttribute('data-cart-fly-add') && enabled() && event.detail.xhr) {
                snapshots.set(event.detail.xhr, snapshot(elt));
            }
        } catch (error) { /* افکت نباید افزودن به سبد را بشکند */ }
    });

    document.addEventListener('htmx:afterRequest', function (event) {
        try {
            var xhr = event.detail && event.detail.xhr;
            var snap = xhr && snapshots.get(xhr);
            if (!snap) { return; }
            snapshots.delete(xhr);
            // فقط افزودن واقعی: ۲۰۰. پاسخ ۲۰۴ یعنی کاربر تأییدنشده است (توست خطا)، پس انیمیشنی نمی‌خواهد
            if (!event.detail.successful || xhr.status !== 200) { return; }
            play(snap);
        } catch (error) { /* ignore */ }
    });

    // داده‌ی تازه‌ی سبد رسید (OOB)؛ اگر پنجره باز است یا منتظر داده بود، پُر/تازه می‌شود
    document.addEventListener('htmx:oobAfterSwap', function (event) {
        try {
            var target = event.detail && event.detail.target;
            if (!target || target.id !== 'cart-fly-data') { return; }
            if (popup) {
                fillPopup();
                if (lastTarget) { positionPopup(lastTarget); }
            } else if (popupWaiting) {
                var waiting = popupWaiting;
                popupWaiting = null;
                openPopup(waiting.target, waiting.productId, 'fly');
            }
        } catch (error) { /* ignore */ }
    });

    /* ---------- هاور روی آیکون سبد (فقط دسکتاپ با ماوس) ---------- */
    function hoverEnabled() {
        if (!document.body || document.body.getAttribute('data-cart-hover') !== '1') { return false; }
        return !!(window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches);
    }

    function bindHover() {
        var badge = document.getElementById('nav-cart-badge');       // آیکون سبد هدر دسکتاپ (در موبایل مخفی است)
        var holder = badge && badge.parentElement;
        if (!holder || holder.getAttribute('data-cart-hover-bound')) { return; }
        holder.setAttribute('data-cart-hover-bound', '1');
        holder.addEventListener('mouseenter', function () {
            iconHover = true;
            if (!hoverEnabled() || flights > 0) { return; }
            clearTimeout(hoverCloseTimer);
            clearTimeout(hoverOpenTimer);
            hoverOpenTimer = setTimeout(function () {
                var target = cartTarget();
                if (!target || target.mobile || !iconHover) { return; }
                var drawer = document.getElementById('offcanvas-left');
                if (drawer && !drawer.classList.contains('invisible')) { return; }   // کشوی سبد باز است
                if (popup) { return; }                                                 // از قبل باز است (مثلاً بعد از افزودن)
                lastTarget = target;
                openPopup(target, null, 'hover');
            }, 130);
        });
        holder.addEventListener('mouseleave', function () {
            iconHover = false;
            clearTimeout(hoverOpenTimer);
            if (popup && popupMode === 'hover') { scheduleHoverClose(); }
        });
    }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', bindHover); } else { bindHover(); }

    document.addEventListener('click', function (event) {
        if (popup && !popup.contains(event.target)) { closePopup(false); }
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') { closePopup(false); }
    });
    window.addEventListener('resize', function () { closePopup(true); });
})();
