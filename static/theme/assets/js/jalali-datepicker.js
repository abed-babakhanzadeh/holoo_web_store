/* انتخابگر تاریخ شمسی (بدون وابستگی خارجی) — روی هر input دارای data-jalali-datepicker فعال می‌شود.
   مقدار نهایی به‌صورت رشته‌ی «YYYY/MM/DD» شمسی در همان input ذخیره می‌شود. */
(function () {
    'use strict';

    var WEEKDAY_INDEX = {Sat: 0, Sun: 1, Mon: 2, Tue: 3, Wed: 4, Thu: 5, Fri: 6};
    var WEEKDAY_LABELS = ['ش', 'ی', 'د', 'س', 'چ', 'پ', 'ج'];
    var MONTH_NAMES = ['فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور', 'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند'];

    var jalaliFormatter = new Intl.DateTimeFormat('en-US-u-ca-persian-nu-latn', {
        year: 'numeric', month: 'numeric', day: 'numeric', weekday: 'short'
    });

    function toJalali(date) {
        var parts = {};
        jalaliFormatter.formatToParts(date).forEach(function (p) { parts[p.type] = p.value; });
        return {
            year: parseInt(parts.year, 10),
            month: parseInt(parts.month, 10),
            day: parseInt(parts.day, 10),
            weekday: WEEKDAY_INDEX[parts.weekday]
        };
    }

    function pad2(n) { return (n < 10 ? '0' : '') + n; }

    // تاریخ میلادیِ روزِ اول یک ماه شمسی را با جست‌وجوی محدود اطراف یک حدس اولیه پیدا می‌کند
    // (محاسبات تقویم شمسی/کبیسه را به Intl/ICU مرورگر واگذار می‌کند، بدون نیاز به پیاده‌سازی دستی)
    function firstOfJalaliMonth(jy, jm) {
        var seed = new Date(Date.UTC(jy + 621, 2, 21));
        seed.setUTCDate(seed.getUTCDate() + (jm - 1) * 30);
        for (var offset = -20; offset <= 20; offset++) {
            var d = new Date(seed.getTime());
            d.setUTCDate(d.getUTCDate() + offset);
            var j = toJalali(d);
            if (j.year === jy && j.month === jm && j.day === 1) return d;
        }
        return seed;
    }

    function daysInJalaliMonth(jy, jm) {
        var nextJy = jm === 12 ? jy + 1 : jy;
        var nextJm = jm === 12 ? 1 : jm + 1;
        var first = firstOfJalaliMonth(jy, jm);
        var next = firstOfJalaliMonth(nextJy, nextJm);
        return Math.round((next.getTime() - first.getTime()) / 86400000);
    }

    function ensureStyles() {
        if (document.getElementById('jdp-styles')) return;
        var style = document.createElement('style');
        style.id = 'jdp-styles';
        style.textContent =
            '.jdp-panel{position:absolute;top:calc(100% + 6px);inset-inline-start:0;z-index:50;min-width:280px;' +
            'background:#fff;border:1px solid #e5e7eb;border-radius:12px;box-shadow:0 10px 25px -5px rgba(0,0,0,.1),0 8px 10px -6px rgba(0,0,0,.1);padding:12px;font-size:13px;direction:rtl}' +
            '.dark .jdp-panel{background:#1f2937;border-color:#374151}' +
            '.jdp-header{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:10px}' +
            '.jdp-nav{width:28px;height:28px;border-radius:8px;border:1px solid #e5e7eb;background:#fff;color:#4b5563;font-size:16px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0}' +
            '.jdp-nav:hover{background:#f9fafb;border-color:var(--color-primary)}' +
            '.dark .jdp-nav{background:#111827;border-color:#374151;color:#d1d5db}' +
            '.dark .jdp-nav:hover{border-color:var(--color-primary)}' +
            '.jdp-selects{display:flex;gap:6px;flex:1}' +
            '.jdp-select{flex:1;min-width:0;padding:5px 6px;border:1px solid #e5e7eb;border-radius:8px;background:#fff;color:#1f2937;font-size:12.5px}' +
            '.dark .jdp-select{background:#111827;border-color:#374151;color:#fff}' +
            '.jdp-weekdays{display:grid;grid-template-columns:repeat(7,1fr);text-align:center;color:#9ca3af;font-size:11px;margin-bottom:4px}' +
            '.jdp-days{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}' +
            '.jdp-cell{height:30px;display:flex;align-items:center;justify-content:center}' +
            '.jdp-day{border:none;background:transparent;border-radius:8px;color:#374151;cursor:pointer;font-size:12.5px}' +
            '.jdp-day:hover{background:#f3f4f6}' +
            '.dark .jdp-day{color:#e5e7eb}' +
            '.dark .jdp-day:hover{background:#374151}' +
            '.jdp-day-today{font-weight:700;color:var(--color-primary)}' +
            '.jdp-day-selected{background:var(--color-primary)!important;color:#fff!important;font-weight:700}' +
            '.jdp-footer{display:flex;align-items:center;justify-content:space-between;margin-top:10px;padding-top:8px;border-top:1px solid #f3f4f6}' +
            '.dark .jdp-footer{border-color:#374151}' +
            '.jdp-link{background:none;border:none;color:var(--color-primary);font-size:12px;font-weight:700;cursor:pointer;padding:2px}' +
            '.jdp-link-muted{color:#9ca3af;font-weight:400}';
        document.head.appendChild(style);
    }

    function bind(input) {
        input.dataset.jdpBound = '1';
        var today = toJalali(new Date());
        var maxYear = parseInt(input.dataset.maxYear, 10) || today.year;
        var minYear = parseInt(input.dataset.minYear, 10) || (today.year - 100);

        function parseValue() {
            var m = /^(\d{4})\/(\d{1,2})\/(\d{1,2})$/.exec((input.value || '').trim());
            if (m) return {year: +m[1], month: +m[2], day: +m[3]};
            return null;
        }

        var selected = parseValue();
        var state = {year: selected ? selected.year : today.year, month: selected ? selected.month : today.month};

        var panel = document.createElement('div');
        panel.className = 'jdp-panel';
        panel.style.display = 'none';
        input.insertAdjacentElement('afterend', panel);

        function render() {
            var numDays = daysInJalaliMonth(state.year, state.month);
            var first = firstOfJalaliMonth(state.year, state.month);
            var startWeekday = toJalali(first).weekday;

            var yearOptions = '';
            for (var y = maxYear; y >= minYear; y--) {
                yearOptions += '<option value="' + y + '"' + (y === state.year ? ' selected' : '') + '>' + y + '</option>';
            }
            var monthOptions = MONTH_NAMES.map(function (name, idx) {
                var v = idx + 1;
                return '<option value="' + v + '"' + (v === state.month ? ' selected' : '') + '>' + name + '</option>';
            }).join('');

            var cells = '';
            for (var i = 0; i < startWeekday; i++) cells += '<span class="jdp-cell"></span>';
            for (var d = 1; d <= numDays; d++) {
                var isSelected = selected && selected.year === state.year && selected.month === state.month && selected.day === d;
                var isToday = today.year === state.year && today.month === state.month && today.day === d;
                var cls = 'jdp-day';
                if (isSelected) cls += ' jdp-day-selected';
                else if (isToday) cls += ' jdp-day-today';
                cells += '<span class="jdp-cell"><button type="button" class="' + cls + '" data-day="' + d + '">' + d + '</button></span>';
            }

            panel.innerHTML =
                '<div class="jdp-header">' +
                    '<button type="button" class="jdp-nav" data-nav="next" aria-label="ماه بعد">&#8249;</button>' +
                    '<div class="jdp-selects">' +
                        '<select class="jdp-select" data-select="month">' + monthOptions + '</select>' +
                        '<select class="jdp-select" data-select="year">' + yearOptions + '</select>' +
                    '</div>' +
                    '<button type="button" class="jdp-nav" data-nav="prev" aria-label="ماه قبل">&#8250;</button>' +
                '</div>' +
                '<div class="jdp-weekdays">' + WEEKDAY_LABELS.map(function (l) { return '<span>' + l + '</span>'; }).join('') + '</div>' +
                '<div class="jdp-days">' + cells + '</div>' +
                '<div class="jdp-footer">' +
                    '<button type="button" class="jdp-link" data-action="today">امروز</button>' +
                    '<button type="button" class="jdp-link jdp-link-muted" data-action="clear">پاک کردن</button>' +
                '</div>';
        }

        function open() {
            render();
            panel.style.display = 'block';
            document.addEventListener('click', outsideClick, true);
        }

        function close() {
            panel.style.display = 'none';
            document.removeEventListener('click', outsideClick, true);
        }

        function outsideClick(e) {
            if (!panel.contains(e.target) && e.target !== input) close();
        }

        function setValue(y, m, d) {
            selected = {year: y, month: m, day: d};
            input.value = y + '/' + pad2(m) + '/' + pad2(d);
            input.dispatchEvent(new Event('change', {bubbles: true}));
        }

        input.addEventListener('click', function (e) {
            e.stopPropagation();
            if (panel.style.display === 'none') open(); else close();
        });

        panel.addEventListener('click', function (e) {
            e.stopPropagation();

            var dayBtn = e.target.closest('[data-day]');
            if (dayBtn) {
                setValue(state.year, state.month, parseInt(dayBtn.dataset.day, 10));
                close();
                return;
            }

            var navBtn = e.target.closest('[data-nav]');
            if (navBtn) {
                var dir = navBtn.dataset.nav === 'next' ? 1 : -1;
                state.month += dir;
                if (state.month > 12) { state.month = 1; state.year++; }
                if (state.month < 1) { state.month = 12; state.year--; }
                state.year = Math.min(maxYear, Math.max(minYear, state.year));
                render();
                return;
            }

            var action = e.target.closest('[data-action]');
            if (action) {
                if (action.dataset.action === 'today') {
                    state.year = today.year;
                    state.month = today.month;
                    setValue(today.year, today.month, today.day);
                    close();
                } else if (action.dataset.action === 'clear') {
                    selected = null;
                    input.value = '';
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    close();
                }
            }
        });

        panel.addEventListener('change', function (e) {
            var sel = e.target.closest('[data-select]');
            if (!sel) return;
            if (sel.dataset.select === 'month') state.month = parseInt(sel.value, 10);
            if (sel.dataset.select === 'year') state.year = parseInt(sel.value, 10);
            render();
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && panel.style.display !== 'none') close();
        });
    }

    function init(root) {
        ensureStyles();
        (root || document).querySelectorAll('[data-jalali-datepicker]').forEach(function (input) {
            if (!input.dataset.jdpBound) bind(input);
        });
    }

    document.addEventListener('DOMContentLoaded', function () { init(document); });
    document.addEventListener('htmx:afterSettle', function (e) { init(e.target); });
})();
