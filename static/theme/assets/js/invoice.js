/*
 * سوییچ چیدمان چاپ فاکتور (templates/invoices/layout.html): دو حالت ساده و شفاف.
 *
 *   حالت پیوسته (یکپارچه) - پیش‌فرض: همان سند اصلی سرور با یک سربرگ، جدول کامل اقلام و یک فوتر. هیچ کدی ردیف‌ها را تکه نمی‌کند.
 *   برگه‌های مستقل (گسسته): فقط وقتی کاربر خودش روی دکمه‌اش کلیک کند. اقلام به‌طور متوازن بین چند «.invoice-sheet» تقسیم می‌شود
 *       (حداکثر data-rows-per-sheet ردیف در هر برگه، پیش‌فرض ۸؛ مثلاً ۱۳ قلم = ۷ + ۶ نه سه برگه‌ی نیمه‌خالی)؛ هر برگه سربرگ کامل، جدول،
 *       «جمع این برگه» و فوتر خودش را دارد و در چاپ با break-after: page از بقیه جداست. تا data-rows-per-sheet قلم تقسیم نمی‌شود.
 *
 * هیچ رفتار خودکاری (beforeprint / matchMedia) وجود ندارد: چیدمان فقط با کلیک کاربر عوض می‌شود. سرور فقط یک فاکتور کامل می‌فرستد و
 * این اسکریپت هیچ درخواستی به سرور نمی‌زند. شماره ردیف‌ها در همه‌ی برگه‌ها پیوسته می‌ماند (ردیف ۶ در برگه‌ی دوم هم ۶ است).
 */
(function () {
    'use strict';

    var root = document.getElementById('invoiceRoot');
    if (!root) return;

    var PER_SHEET = parseInt(root.dataset.rowsPerSheet, 10) || 8;
    var pristine = root.innerHTML;                       // فاکتور کامل رندرشده‌ی سرور؛ مرجع بازگشت به حالت پیوسته
    var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-inv-layout]'));

    function formatNumber(value) {
        return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }

    // اندازه‌ی دسته‌ها به‌صورت متوازن: n برگه‌ی لازم، ردیف‌ها تا حد امکان مساوی (اولین برگه‌ها در صورت باقیمانده یکی بیشتر)
    // پدینگ سطرها (فقط روی کاغذ عمودی بزرگ، نگاه کنید invoice.css): هرچه اقلام هر برگه کمتر، سطرها بازتر تا برگه خالی/مچاله نماند.
    // برای همه‌ی برگه‌ها یکسان و بر مبنای بزرگ‌ترین دسته است تا ظاهر برگه‌ها هم‌شکل بماند.
    function rowPadding(maxRows) {
        if (maxRows <= 3) return 14;
        if (maxRows <= 4) return 12;
        if (maxRows <= 5) return 10;
        if (maxRows <= 6) return 8;
        if (maxRows <= 7) return 6;
        return 4;
    }

    function balancedSizes(total, perSheet) {
        var sheets = Math.ceil(total / perSheet);
        var base = Math.floor(total / sheets);
        var extra = total % sheets;
        var sizes = [];
        for (var i = 0; i < sheets; i++) sizes.push(base + (i < extra ? 1 : 0));
        return sizes;
    }

    function buildSheets() {
        var holder = document.createElement('div');
        holder.innerHTML = pristine;
        var base = holder.querySelector('[data-sheet]');
        var rows = Array.prototype.slice.call(base.querySelectorAll('[data-row]'));
        if (rows.length <= PER_SHEET) return null;       // تا PER_SHEET قلم یک برگه بیشتر لازم نیست

        var sizes = balancedSizes(rows.length, PER_SHEET);
        var pad = rowPadding(Math.max.apply(null, sizes));
        var chunks = [];
        var cursor = 0;
        sizes.forEach(function (size) { chunks.push(rows.slice(cursor, cursor + size)); cursor += size; });

        return chunks.map(function (chunk, index) {
            var isLast = index === chunks.length - 1;
            var sheet = base.cloneNode(true);
            sheet.style.setProperty('--inv-row-pad', pad + 'px');
            var tbody = sheet.querySelector('[data-inv-table] tbody');
            var keep = chunk.map(function (row) { return row.getAttribute('data-row'); });
            Array.prototype.slice.call(tbody.querySelectorAll('[data-row]')).forEach(function (row) {
                if (keep.indexOf(row.getAttribute('data-row')) === -1) row.remove();
            });

            // جمع مجزای همین برگه (از data-value خودِ ردیف‌ها؛ مبلغ‌ها همان اعداد رندرشده‌ی سرورند)
            var totalRow = tbody.querySelector('[data-total-row]');
            var subtotal = totalRow.cloneNode(true);
            var sums = {};
            chunk.forEach(function (row) {
                Array.prototype.slice.call(row.querySelectorAll('[data-sum]')).forEach(function (cell) {
                    var key = cell.getAttribute('data-sum');
                    sums[key] = (sums[key] || 0) + (parseFloat(cell.getAttribute('data-value')) || 0);
                });
            });
            Array.prototype.slice.call(subtotal.querySelectorAll('[data-sum-key]')).forEach(function (cell) {
                var key = cell.getAttribute('data-sum-key');
                cell.textContent = key in sums ? formatNumber(sums[key]) : '';
            });
            var label = subtotal.querySelector('[data-total-label]');
            if (label) label.textContent = 'جمع این برگه';
            subtotal.removeAttribute('data-total-row');
            subtotal.classList.add('inv-subtotal-row');

            if (isLast) {
                // آخرین برگه: جمع مجزای برگه + جمع کل فاکتور (ردیف اصلی سرور) + خلاصه‌ی مبالغ
                var grand = totalRow.querySelector('[data-total-label]');
                if (grand) grand.textContent = 'جمع کل فاکتور';
                totalRow.parentNode.insertBefore(subtotal, totalRow);
            } else {
                totalRow.parentNode.replaceChild(subtotal, totalRow);
                var summary = sheet.querySelector('[data-inv-summary]');
                if (summary) summary.remove();           // خلاصه‌ی نهایی فقط روی آخرین برگه
            }

            var pageNo = sheet.querySelector('[data-inv-pageno]');
            if (pageNo) {
                pageNo.hidden = false;
                pageNo.textContent = 'برگه ' + (index + 1) + ' از ' + chunks.length;
            }
            return sheet;
        });
    }

    function apply(mode) {
        var sheets = mode === 'paged' ? buildSheets() : null;
        root.innerHTML = '';
        if (sheets) {
            sheets.forEach(function (sheet) { root.appendChild(sheet); });
            root.classList.add('is-paged');
        } else {
            root.innerHTML = pristine;                   // حالت پیوسته: دقیقاً همان HTML سرور
            root.classList.remove('is-paged');
        }
        root.setAttribute('data-sheets', String(root.querySelectorAll('.invoice-sheet').length));
        buttons.forEach(function (btn) {
            btn.setAttribute('aria-pressed', btn.getAttribute('data-inv-layout') === mode ? 'true' : 'false');
        });
    }

    buttons.forEach(function (btn) {
        btn.addEventListener('click', function () { apply(btn.getAttribute('data-inv-layout')); });
    });
    var printBtn = document.querySelector('[data-inv-print]');
    if (printBtn) printBtn.addEventListener('click', function () { window.print(); });
})();
