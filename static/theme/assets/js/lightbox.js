/**
 * لایت‌باکس عمومی برای نمایش تصاویر (گالری محصول، تصاویر نظرات و ...).
 * روی هر عنصری که data-lightbox-src دارد کلیک شود، تصویر در یک پاپ‌آپ با
 * قابلیت زوم (اسکرول/دبل‌کلیک) و دکمه دانلود/بستن باز می‌شود.
 * استایل‌ها عمداً کلاس‌های CSS دستی (نه یوتیلیتی‌های تیلویند) هستند، چون این
 * مارک‌آپ داخل رشته‌ی جاوااسکریپت ساخته می‌شود و اسکنر خودکار تیلویند آن را نمی‌بیند
 * (تعریف کلاس‌ها در FA/src/input.css، بخش انتهایی فایل).
 */
(function () {
    var overlay, imgEl, downloadLink;
    var scale = 1, translateX = 0, translateY = 0;
    var dragging = false, dragStartX = 0, dragStartY = 0;

    function buildOverlay() {
        if (overlay) return;
        overlay = document.createElement('div');
        overlay.id = 'lightbox-overlay';
        overlay.className = 'hidden';
        overlay.innerHTML =
            '<button type="button" data-lightbox-close class="lightbox-icon-btn" aria-label="بستن">×</button>' +
            '<a data-lightbox-download download target="_blank" rel="noopener" class="lightbox-icon-btn" aria-label="دانلود تصویر">' +
                '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" style="width:1.25rem;height:1.25rem;"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" /></svg>' +
            '</a>' +
            '<div class="lightbox-frame">' +
                '<img data-lightbox-img alt="" class="lightbox-img" draggable="false">' +
            '</div>';
        document.body.appendChild(overlay);
        imgEl = overlay.querySelector('[data-lightbox-img]');
        downloadLink = overlay.querySelector('[data-lightbox-download]');

        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) close();
        });
        overlay.querySelector('[data-lightbox-close]').addEventListener('click', close);

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

        document.addEventListener('keydown', function (e) {
            if (overlay.classList.contains('hidden')) return;
            if (e.key === 'Escape') close();
            if (e.key === '+') setScale(scale + 0.25);
            if (e.key === '-') setScale(scale - 0.25);
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

    function open(src, alt) {
        buildOverlay();
        scale = 1; translateX = 0; translateY = 0;
        imgEl.src = src;
        imgEl.alt = alt || '';
        imgEl.style.transform = 'none';
        downloadLink.href = src;
        overlay.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
    }

    function close() {
        if (!overlay) return;
        overlay.classList.add('hidden');
        document.body.style.overflow = '';
    }

    document.addEventListener('click', function (e) {
        var trigger = e.target.closest('[data-lightbox-src]');
        if (!trigger) return;
        e.preventDefault();
        open(trigger.getAttribute('data-lightbox-src'), trigger.getAttribute('data-lightbox-alt'));
    });
})();
