/**
 * لایت‌باکس عمومی برای نمایش تصاویر (گالری محصول، تصاویر نظرات و ...).
 * روی هر عنصری که data-lightbox-src دارد کلیک شود، تصویر در یک پاپ‌آپ با
 * قابلیت زوم (اسکرول/دبل‌کلیک) و دکمه دانلود/بستن باز می‌شود.
 *
 * حالت گالری: اگر عنصر کلیک‌شده data-lightbox-group هم داشته باشد، تمام عناصر هم‌گروه
 * (به همان ترتیبی که در DOM هستند - همان ترتیب اسلایدهای گالری محصول) به‌عنوان یک مجموعه
 * قابل‌مرور جمع می‌شوند: دو دکمه‌ی ناوبری (قبلی/بعدی)، کشیدن با انگشت (swipe) در موبایل، و
 * کلیدهای arrow صفحه‌کلید. عناصر بدون data-lightbox-group (مثل تصاویر نظرات) مثل قبل
 * تک‌تصویری می‌مانند (دکمه‌های ناوبری خودکار پنهان‌اند).
 *
 * استایل‌ها عمداً کلاس‌های CSS دستی (نه یوتیلیتی‌های تیلویند) هستند، چون این
 * مارک‌آپ داخل رشته‌ی جاوااسکریپت ساخته می‌شود و اسکنر خودکار تیلویند آن را نمی‌بیند
 * (تعریف کلاس‌ها در static/theme/assets/css/app.css، بخش لایت‌باکس).
 */
(function () {
    var overlay, imgEl, downloadLink, prevBtn, nextBtn, counterEl;
    var scale = 1, translateX = 0, translateY = 0;
    var dragging = false, dragStartX = 0, dragStartY = 0;
    var images = [];   // [{src, alt}, ...] - گروه فعلی
    var currentIndex = 0;
    var touchStartX = null, touchStartY = null;

    function buildOverlay() {
        if (overlay) return;
        overlay = document.createElement('div');
        overlay.id = 'lightbox-overlay';
        overlay.className = 'hidden';
        overlay.innerHTML =
            '<button type="button" data-lightbox-close class="lightbox-icon-btn lightbox-close-btn" aria-label="بستن">×</button>' +
            '<a data-lightbox-download download target="_blank" rel="noopener" class="lightbox-icon-btn lightbox-download-btn" aria-label="دانلود تصویر">' +
                '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" style="width:1.25rem;height:1.25rem;"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" /></svg>' +
            '</a>' +
            '<button type="button" data-lightbox-prev class="lightbox-icon-btn lightbox-nav-btn lightbox-prev-btn" aria-label="تصویر قبلی">' +
                '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" style="width:1.5rem;height:1.5rem;"><path stroke-linecap="round" stroke-linejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" /></svg>' +
            '</button>' +
            '<button type="button" data-lightbox-next class="lightbox-icon-btn lightbox-nav-btn lightbox-next-btn" aria-label="تصویر بعدی">' +
                '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" style="width:1.5rem;height:1.5rem;"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" /></svg>' +
            '</button>' +
            '<div class="lightbox-frame">' +
                '<img data-lightbox-img alt="" class="lightbox-img" draggable="false">' +
            '</div>' +
            '<div class="lightbox-counter" data-lightbox-counter dir="ltr"></div>';
        document.body.appendChild(overlay);
        imgEl = overlay.querySelector('[data-lightbox-img]');
        downloadLink = overlay.querySelector('[data-lightbox-download]');
        prevBtn = overlay.querySelector('[data-lightbox-prev]');
        nextBtn = overlay.querySelector('[data-lightbox-next]');
        counterEl = overlay.querySelector('[data-lightbox-counter]');

        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) close();
        });
        overlay.querySelector('[data-lightbox-close]').addEventListener('click', close);
        prevBtn.addEventListener('click', function (e) { e.stopPropagation(); showIndex(currentIndex - 1); });
        nextBtn.addEventListener('click', function (e) { e.stopPropagation(); showIndex(currentIndex + 1); });

        var frame = overlay.querySelector('.lightbox-frame');
        frame.addEventListener('wheel', function (e) {
            e.preventDefault();
            var delta = e.deltaY < 0 ? 0.25 : -0.25;
            setScale(scale + delta);
        }, { passive: false });

        frame.addEventListener('dblclick', function () {
            setScale(scale > 1 ? 1 : 2.2);
        });

        frame.addEventListener('mousedown', function (e) {
            if (scale <= 1) return;
            dragging = true;
            dragStartX = e.clientX - translateX;
            dragStartY = e.clientY - translateY;
        });
        window.addEventListener('mousemove', function (e) {
            if (!dragging) return;
            translateX = e.clientX - dragStartX;
            translateY = e.clientY - dragStartY;
            applyTransform();
        });
        window.addEventListener('mouseup', function () { dragging = false; });

        // کشیدن با انگشت (موبایل): فقط وقتی زوم نشده - اگر زوم‌شده، کشیدن برای Pan تصویر
        // استفاده می‌شود، نه جابه‌جایی بین تصاویر گالری
        frame.addEventListener('touchstart', function (e) {
            if (scale > 1 || e.touches.length !== 1) return;
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
        }, { passive: true });
        frame.addEventListener('touchend', function (e) {
            if (touchStartX === null || scale > 1) return;
            var t = e.changedTouches[0];
            var dx = t.clientX - touchStartX;
            var dy = t.clientY - touchStartY;
            touchStartX = null;
            if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy) * 1.5) {
                if (dx < 0) showIndex(currentIndex + 1);
                else showIndex(currentIndex - 1);
            }
        }, { passive: true });

        document.addEventListener('keydown', function (e) {
            if (overlay.classList.contains('hidden')) return;
            if (e.key === 'Escape') close();
            if (e.key === '+') setScale(scale + 0.25);
            if (e.key === '-') setScale(scale - 0.25);
            if (e.key === 'ArrowLeft') showIndex(currentIndex + 1);
            if (e.key === 'ArrowRight') showIndex(currentIndex - 1);
        });
    }

    function setScale(next) {
        scale = Math.min(Math.max(next, 1), 3.5);
        if (scale === 1) { translateX = 0; translateY = 0; }
        applyTransform();
        var frame = overlay.querySelector('.lightbox-frame');
        frame.style.cursor = scale > 1 ? 'grab' : 'zoom-in';
    }

    function applyTransform() {
        imgEl.style.transform = 'translate(' + translateX + 'px,' + translateY + 'px) scale(' + scale + ')';
    }

    function updateNav() {
        var multi = images.length > 1;
        prevBtn.style.display = multi ? '' : 'none';
        nextBtn.style.display = multi ? '' : 'none';
        counterEl.style.display = multi ? '' : 'none';
        if (multi) counterEl.textContent = (currentIndex + 1) + ' / ' + images.length;
    }

    function showIndex(index) {
        if (!images.length) return;
        currentIndex = ((index % images.length) + images.length) % images.length;
        var item = images[currentIndex];
        scale = 1; translateX = 0; translateY = 0;
        imgEl.style.transform = 'none';
        imgEl.src = item.src;
        imgEl.alt = item.alt || '';
        downloadLink.href = item.src;
        updateNav();
    }

    function collectGroup(trigger) {
        var group = trigger.getAttribute('data-lightbox-group');
        if (!group) {
            return {
                items: [{
                    src: trigger.getAttribute('data-lightbox-src'),
                    alt: trigger.getAttribute('data-lightbox-alt'),
                }],
                index: 0,
            };
        }
        var nodes = Array.prototype.slice.call(document.querySelectorAll('[data-lightbox-group="' + group + '"]'));
        var items = nodes.map(function (node) {
            return { src: node.getAttribute('data-lightbox-src'), alt: node.getAttribute('data-lightbox-alt') };
        });
        return { items: items, index: nodes.indexOf(trigger) };
    }

    function open(trigger) {
        buildOverlay();
        var group = collectGroup(trigger);
        images = group.items;
        overlay.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
        showIndex(group.index < 0 ? 0 : group.index);
    }

    function close() {
        if (!overlay) return;
        overlay.classList.add('hidden');
        document.body.style.overflow = '';
        images = [];
    }

    document.addEventListener('click', function (e) {
        var trigger = e.target.closest('[data-lightbox-src]');
        if (!trigger) return;
        e.preventDefault();
        open(trigger);
    });
})();
