/**
 * باکس/پاپ‌آپ «اطلاع بده وقتی موجود شد» (طرح دیجی‌کالا) + یک toast سراسری ساده.
 *
 * چرا event delegation (نه addEventListener مستقیم مثل modal-trigger عمومی در app.js):
 * باکس این ویژگی با htmx (ثبت/لغو درخواست) عوض می‌شود، یعنی دکمه و پاپ‌آپش هر بار از نو
 * در DOM ساخته می‌شوند؛ listenerهای مستقیمی که فقط در DOMContentLoaded بسته می‌شوند به
 * این المان‌های تازه وصل نمی‌شوند. برای همین‌جا data-attribute های جدا (data-stock-modal-*)
 * به‌جای data-modal-* عمومی استفاده شده تا با سیستم modal سراسری app.js تداخل نکند.
 *
 * چرا پاپ‌آپ هنگام باز شدن به body منتقل می‌شود: باکس خرید صفحه‌ی محصول (#productBuybox) در
 * دسکتاپ transform/scale می‌گیرد و هر المان position:fixed داخل یک والد transform‌دار به‌جای
 * viewport نسبت به همان والد محاسبه و زیر نوار تب‌ها/فوتر گیر می‌کند؛ در body این مشکل نیست.
 */

function openStockModal(modal) {
    if (!modal) return;
    if (modal.parentElement !== document.body) document.body.appendChild(modal);
    modal.classList.remove('hidden');
}

document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-stock-modal-target]');
    if (trigger) {
        e.preventDefault();
        openStockModal(document.querySelector('[data-stock-modal-id="' + trigger.dataset.stockModalTarget + '"]'));
        return;
    }

    var closeBtn = e.target.closest('[data-stock-modal-close]');
    if (closeBtn) {
        var openModal = closeBtn.closest('.stock-alert-modal');
        if (openModal) openModal.classList.add('hidden');
        return;
    }

    if (e.target.classList && e.target.classList.contains('stock-alert-modal')) {
        e.target.classList.add('hidden');
    }
});

// بعد از هر swap ی htmx: پاپ‌آپ‌های قدیمی که به body منتقل شده بودند دیگر مالک ندارند (باکس
// جدید نسخه‌ی تازه‌ی خودش را دارد) پس حذف می‌شوند؛ اگر پاسخ خطا داشت (data-open-on-load)
// پاپ‌آپ تازه بلافاصله دوباره باز می‌شود تا کاربر پیام خطا را همان‌جا ببیند
document.addEventListener('htmx:afterSwap', function () {
    document.querySelectorAll('body > .stock-alert-modal').forEach(function (m) { m.remove(); });
    var toOpen = document.querySelector('[data-stock-modal-id][data-open-on-load]');
    if (toOpen) openStockModal(toOpen);
});

// فرم پاپ‌آپ: هر دو کانال (پیامک/ایمیل) مستقل قابل انتخاب‌اند (یکی، یا هر دو)؛ فیلد ایمیل فقط
// وقتی چک‌باکس ایمیل فعال است نشان داده می‌شود و دکمه‌ی ثبت فقط با حداقل یک انتخاب فعال است
document.addEventListener('change', function (e) {
    if (!e.target.classList || !e.target.classList.contains('stock-alert-channel-checkbox')) return;
    var form = e.target.closest('form');
    if (!form) return;

    var emailBox = form.querySelector('.stock-alert-channel-checkbox[value="email"]');
    var emailWrap = form.querySelector('.stock-alert-email-wrap');
    if (emailWrap && emailBox) emailWrap.classList.toggle('hidden', !emailBox.checked);

    var anyChecked = !!form.querySelector('.stock-alert-channel-checkbox:checked');
    var submitBtn = form.querySelector('.stock-alert-submit');
    if (submitBtn) {
        submitBtn.disabled = !anyChecked;
        submitBtn.classList.toggle('bg-gray-200', !anyChecked);
        submitBtn.classList.toggle('text-gray-400', !anyChecked);
        submitBtn.classList.toggle('cursor-not-allowed', !anyChecked);
        submitBtn.classList.toggle('bg-primary-grad', anyChecked);
        submitBtn.classList.toggle('text-white', anyChecked);
    }
});

/**
 * توست سراسری ساده (بالای صفحه)؛ محتوای پارشیال htmx بعد از ثبت/لغو اطلاع موجودی صداش
 * می‌زند. استایل‌ها inline است چون Tailwind این پروژه از پیش کامپایل شده و کلاس‌های دلخواه
 * (مثل z-[100]) ممکن است در باندل نباشند.
 */
function showToast(message, type) {
    var container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = 'position:fixed;top:5rem;left:0;right:0;z-index:2000;display:flex;' +
            'flex-direction:column;align-items:center;gap:0.5rem;pointer-events:none;padding:0 1rem;';
        document.body.appendChild(container);
    }
    var toast = document.createElement('div');
    toast.textContent = message;
    toast.style.cssText = 'pointer-events:auto;color:#fff;font-weight:700;font-size:0.875rem;' +
        'padding:0.75rem 1rem;border-radius:0.5rem;box-shadow:0 10px 25px rgba(0,0,0,.2);' +
        'transition:opacity .3s;background:' + (type === 'error' ? '#dc2626' : '#16a34a') + ';';
    container.appendChild(toast);
    setTimeout(function () {
        toast.style.opacity = '0';
        setTimeout(function () { toast.remove(); }, 300);
    }, 3500);
}
