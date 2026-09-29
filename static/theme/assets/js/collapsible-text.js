/* کلاپس متن طولانی («ادامه مطلب»/«مشاهده بیشتر»، کلاس .read-more-wrap - برای توضیحات کالا و
   متن نظر/پاسخ در reviews/partials/review_node.html) و لیست نقاط قوت/ضعف نظرات (.clamp-list).
   قبلاً این منطق فقط به‌صورت اینلاین در product_detail.html وجود داشت؛ چون همین قرارداد کلاس‌ها
   در پنل «دیدگاه‌های من» (templates/reviews/my_reviews.html) هم لازم است، به یک فایل مستقل
   منتقل شد تا هر صفحه‌ای که این کلاس‌ها را دارد (یا هر پاسخ HTMX که review_node.html را مستقیم
   رندر می‌کند) از یک منبع واحد همین رفتار را داشته باشد. */
(function () {
    'use strict';

    // «ادامه مطلب» / «بستن»: باز و بسته شدن سقف متن (رفت و برگشت)
    document.querySelectorAll('.read-more-wrap').forEach(function (wrap) {
        const body = wrap.querySelector('.read-more-body');
        if (body) wrap.dataset.collapsedHeight = body.style.maxHeight || '260px';
    });

    // با event delegation روی document (نه بایند مستقیم روی هر دکمه) تا برای متن‌هایی که بعداً
    // با HTMX اضافه می‌شوند (مثلاً بدنه‌ی پاسخ تازه‌ی یک نظر) هم بدون بایند دوباره کار کند.
    // data-collapsed-label اختیاری: برچسب حالت جمع‌شده هر استفاده را جدا تعیین می‌کند (پیش‌فرض
    // «ادامه مطلب» برای توضیحات کالا؛ نظرات «مشاهده بیشتر» می‌فرستند).
    document.addEventListener('click', function (e) {
        const btn = e.target.closest('.read-more-btn');
        if (!btn) return;
        const wrap = btn.closest('.read-more-wrap');
        const body = wrap.querySelector('.read-more-body');
        const fade = wrap.querySelector('.read-more-fade');
        const expanded = wrap.dataset.expanded === 'true';
        const collapsedLabel = btn.dataset.collapsedLabel || 'ادامه مطلب';
        if (expanded) {
            body.style.maxHeight = wrap.dataset.collapsedHeight;
            if (fade) fade.style.display = '';
            btn.textContent = collapsedLabel;
            wrap.dataset.expanded = 'false';
        } else {
            body.style.maxHeight = 'none';
            if (fade) fade.style.display = 'none';
            btn.textContent = 'بستن';
            wrap.dataset.expanded = 'true';
        }
    });

    // بررسی سرریز شدن متن: چون تب‌های غیرفعال با display:none ارتفاع صفر دارند،
    // این بررسی هم موقع لود صفحه و هم بعد از هر تغییر تب دوباره اجرا می‌شود
    // (با setTimeout تا مطمئن شویم کلاس hidden قبل از اندازه‌گیری برداشته شده)
    function refreshReadMore() {
        document.querySelectorAll('.read-more-wrap').forEach(function (wrap) {
            const body = wrap.querySelector('.read-more-body');
            if (!body) return;
            wrap.dataset.collapsedHeight = wrap.dataset.collapsedHeight || body.style.maxHeight || '260px';
            if (wrap.dataset.expanded === 'true') return; // دست‌کاری‌شده توسط کاربر را نادیده بگیر
            const btn = wrap.querySelector('.read-more-btn');
            const fade = wrap.querySelector('.read-more-fade');
            if (!btn) return;
            const overflowing = body.scrollHeight > body.clientHeight + 4;
            btn.style.display = overflowing ? '' : 'none';
            if (fade) fade.style.display = overflowing ? '' : 'none';
        });
    }

    document.querySelectorAll('.tab-button').forEach(function (tabBtn) {
        tabBtn.addEventListener('click', function () {
            setTimeout(refreshReadMore, 0);
        });
    });

    window.addEventListener('load', refreshReadMore);
    // پاسخ تازه‌ی یک نظر با HTMX (hx-swap="beforeend") اضافه می‌شود؛ بدون این، سرریزبودنِ
    // متنِ همان پاسخِ تازه هرگز بررسی نمی‌شد و دکمه‌ی «مشاهده بیشتر» همیشه پنهان می‌ماند
    document.body.addEventListener('htmx:afterSettle', refreshReadMore);

    // نقاط قوت/ضعف نظرات: اگر بیش از حد مجاز (data-clamp-limit، پیش‌فرض ۳) مورد باشد، مازاد
    // پنهان و یک دکمه‌ی مینیمال مجزا (مستقل از دکمه‌ی متن نظر) نمایش داده می‌شود.
    function refreshClampLists() {
        document.querySelectorAll('.clamp-list').forEach(function (list) {
            if (list.dataset.clampChecked === 'true') return;
            const items = list.querySelectorAll('.clamp-item');
            const limit = parseInt(list.dataset.clampLimit, 10) || 3;
            if (items.length <= limit) return;
            list.dataset.clampChecked = 'true';
            items.forEach(function (item, index) {
                if (index >= limit) item.classList.add('hidden');
            });
            const btn = document.querySelector('.clamp-list-toggle[data-target="' + list.id + '"]');
            if (btn) btn.classList.remove('hidden');
        });
    }

    document.addEventListener('click', function (e) {
        const btn = e.target.closest('.clamp-list-toggle');
        if (!btn) return;
        const list = document.getElementById(btn.dataset.target);
        if (!list) return;
        const limit = parseInt(list.dataset.clampLimit, 10) || 3;
        const expanded = btn.dataset.expanded === 'true';
        list.querySelectorAll('.clamp-item').forEach(function (item, index) {
            if (index >= limit) item.classList.toggle('hidden', expanded);
        });
        btn.textContent = expanded ? 'مشاهده بیشتر' : 'بستن';
        btn.dataset.expanded = expanded ? 'false' : 'true';
    });

    refreshClampLists();
    document.body.addEventListener('htmx:afterSettle', refreshClampLists);
})();
