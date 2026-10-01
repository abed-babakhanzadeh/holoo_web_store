/* صفحه‌ی «تنظیمات سایت»: ۱) تب‌های گروه‌بندی بخش‌ها  ۲) آکاردئون واقعیِ بخش‌های هر گروه.

   ۱) هر fieldset با کلاس sgroup-<کلید> به یک گروه تعلق دارد (products/admin.py::SITE_SETTINGS_GROUPS) و فهرست
      گروه‌ها (کلید + عنوان) به‌صورت JSON در <script id="sitesettings-groups"> می‌آید. این‌جا بالای فرم یک نوار تب
      ساخته می‌شود و هر بار فقط بخش‌های یک گروه دیده می‌شود. تب فعال با #hash و sessionStorage حفظ می‌شود
      (پس از «ذخیره» دوباره همان تب باز است)، و اگر فرم خطای اعتبارسنجی داشته باشد، تبِ دارای خطا باز می‌شود و
      روی تب‌هایی که خطا دارند نشان قرمز با تعداد خطا می‌نشیند.
   ۲) داخل هر گروه فقط یک بخش هم‌زمان باز می‌ماند و باز/بسته‌شدن نرم (انیمیشن ارتفاع) است - جنگو خودش هر
      <details> را کاملاً مستقل و بدون انیمیشن مدیریت می‌کند.

   بدون این اسکریپت (یا اگر خطا بدهد) همه‌ی بخش‌ها مثل قبل زیر هم دیده می‌شوند و native <details> کار می‌کند؛ این فقط
   یک بهبود روی رفتار درست‌کارِ مرورگر است و داده‌ای از فرم حذف نمی‌شود (فیلدهای تب‌های پنهان هم ارسال می‌شوند). */
(function () {
    'use strict';

    var ANIMATION_MS = 220;
    var STORAGE_KEY = 'siteSettingsActiveTab';
    var HASH_PREFIX = '#tab=';
    var GROUP_CLASS_RE = /(?:^|\s)sgroup-([a-z0-9_]+)(?:\s|$)/;

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

    function readGroups() {
        var node = document.getElementById('sitesettings-groups');
        if (!node) return [];
        try {
            return JSON.parse(node.textContent) || [];
        } catch (e) {
            return [];
        }
    }

    function groupOf(fieldset) {
        var match = GROUP_CLASS_RE.exec(fieldset.className);
        return match ? match[1] : null;
    }

    function storageGet() {
        try { return window.sessionStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
    }

    function storageSet(value) {
        try { window.sessionStorage.setItem(STORAGE_KEY, value); } catch (e) { /* حالت خصوصی: مهم نیست */ }
    }

    document.addEventListener('DOMContentLoaded', function () {
        var fieldsets = Array.prototype.slice.call(document.querySelectorAll('fieldset.module.collapse'));
        var allDetails = fieldsets
            .map(function (fs) { return fs.querySelector(':scope > details'); })
            .filter(Boolean);
        if (!allDetails.length) return;

        // --- آکاردئون: داخل یک گروه فقط یکی باز بماند ---
        allDetails.forEach(function (details) {
            var summary = details.querySelector(':scope > summary');
            if (!summary) return;
            summary.addEventListener('click', function (e) {
                e.preventDefault();
                if (details.hasAttribute('open')) {
                    animateClose(details);
                    return;
                }
                var fieldset = details.parentElement;
                var key = groupOf(fieldset);
                fieldsets.forEach(function (other) {
                    var otherDetails = other.querySelector(':scope > details');
                    if (otherDetails && otherDetails !== details && groupOf(other) === key
                        && otherDetails.hasAttribute('open')) {
                        animateClose(otherDetails);
                    }
                });
                animateOpen(details);
            });
        });

        var groups = readGroups().filter(function (g) {
            return fieldsets.some(function (fs) { return groupOf(fs) === g.key; });
        });

        function openOnly(details) {
            allDetails.forEach(function (d) {
                if (d !== details) d.removeAttribute('open');
            });
            if (details) details.setAttribute('open', '');
        }

        // بدون گروه (JSON نیامده): رفتار قدیمی - فقط اولین بخش باز
        if (!groups.length) {
            openOnly(allDetails[0]);
            return;
        }

        // --- نوار تب ---
        var bar = document.createElement('div');
        bar.className = 'sg-tabs';
        bar.setAttribute('role', 'tablist');
        bar.setAttribute('aria-label', 'گروه‌های تنظیمات سایت');
        var tabs = {};
        groups.forEach(function (group) {
            var errorCount = 0;
            fieldsets.forEach(function (fs) {
                if (groupOf(fs) === group.key) errorCount += fs.querySelectorAll('ul.errorlist li').length;
            });
            var tab = document.createElement('button');
            tab.type = 'button';
            tab.className = 'sg-tab';
            tab.id = 'sg-tab-' + group.key;
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-selected', 'false');
            tab.setAttribute('tabindex', '-1');
            tab.textContent = group.label;
            if (errorCount) {
                var badge = document.createElement('span');
                badge.className = 'sg-badge';
                badge.textContent = String(errorCount);
                badge.setAttribute('title', 'تعداد خطا در این گروه');
                tab.appendChild(badge);
            }
            tab.addEventListener('click', function () { activate(group.key, true); });
            tab.addEventListener('keydown', function (e) {
                var order = groups.map(function (g) { return g.key; });
                var index = order.indexOf(group.key);
                var step = e.key === 'ArrowLeft' ? 1 : (e.key === 'ArrowRight' ? -1 : 0);
                // جهت صفحه راست‌به‌چپ است: «چپ» یعنی تب بعدی
                if (document.documentElement.dir !== 'rtl') step = -step;
                if (!step) return;
                e.preventDefault();
                var next = order[(index + step + order.length) % order.length];
                activate(next, true);
                tabs[next].focus();
            });
            tabs[group.key] = tab;
            bar.appendChild(tab);
        });
        fieldsets[0].parentNode.insertBefore(bar, fieldsets[0]);

        function activate(key, openFirst) {
            groups.forEach(function (g) {
                var selected = g.key === key;
                tabs[g.key].setAttribute('aria-selected', selected ? 'true' : 'false');
                tabs[g.key].setAttribute('tabindex', selected ? '0' : '-1');
            });
            var first = null;
            fieldsets.forEach(function (fs) {
                var visible = groupOf(fs) === key;
                fs.classList.toggle('sg-hidden', !visible);
                if (visible && !first) first = fs.querySelector(':scope > details');
            });
            if (openFirst) openOnly(first);
            storageSet(key);
            try {
                window.history.replaceState(null, '', window.location.pathname + window.location.search + HASH_PREFIX + key);
            } catch (e) { /* مهم نیست: sessionStorage هم تب فعال را نگه می‌دارد */ }
        }

        // نمایش یک عنصر مشخص (مثلاً فیلد نامعتبر): تبِ خودش را فعال و بخش خودش را باز می‌کند
        function reveal(element) {
            var fieldset = element.closest('fieldset.module.collapse');
            if (!fieldset) return;
            var key = groupOf(fieldset);
            var details = fieldset.querySelector(':scope > details');
            if (key && tabs[key]) activate(key, false);
            if (details) openOnly(details);
        }

        // تب اولیه: بخش دارای خطا > #hash > sessionStorage > اولین گروه
        var errored = document.querySelector('fieldset.module.collapse ul.errorlist');
        var keys = groups.map(function (g) { return g.key; });
        var fromHash = window.location.hash.indexOf(HASH_PREFIX) === 0 ? window.location.hash.slice(HASH_PREFIX.length) : null;
        var initial = [fromHash, storageGet()].filter(function (k) { return k && keys.indexOf(k) !== -1; })[0] || keys[0];
        if (errored) {
            reveal(errored);
        } else {
            activate(initial, true);
        }

        // فیلد نامعتبر (HTML5 required/min/max) داخل تبِ پنهان یا بخش بسته قابل فوکوس نیست و مرورگر بی‌صدا
        // ارسال را متوقف می‌کند؛ پس قبل از نمایش پیام، همان‌جا را آشکار می‌کنیم
        document.addEventListener('invalid', function (e) {
            var fieldset = e.target.closest ? e.target.closest('fieldset.module.collapse') : null;
            if (!fieldset) return;
            // بخشِ دارای خطا را جنگو بدون <details> (همیشه باز) رندر می‌کند؛ پس details ممکن است نباشد
            var details = fieldset.querySelector(':scope > details');
            if (fieldset.classList.contains('sg-hidden') || (details && !details.hasAttribute('open'))) {
                reveal(e.target);
            }
        }, true);
    });
})();
