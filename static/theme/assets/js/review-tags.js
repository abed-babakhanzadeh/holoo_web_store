/**
 * ویجت تگ‌های نقاط قوت/ضعف نظر محصول.
 * برخلاف ماژول عمومی «TAG INPUT» قالب (که فقط تزیینی است)، این نسخه به‌ازای هر تگ
 * یک input[type=hidden] هم‌نام (name="pros" یا name="cons") می‌سازد تا با
 * request.POST.getlist(...) در جنگو قابل خواندن باشد.
 */
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.review-tag-container').forEach(function (container) {
        const input = container.querySelector('.review-tag-input');
        if (!input) return;

        const fieldName = container.dataset.name || 'tags';
        const colorClass = container.dataset.color || 'text-gray-600';

        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && input.value.trim()) {
                e.preventDefault();
                addTag(input.value.trim());
                input.value = '';
            }
        });

        function addTag(text) {
            const tag = document.createElement('span');
            tag.className = 'inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs bg-gray-100 dark:bg-gray-700 ' + colorClass;

            const hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = fieldName;
            hidden.value = text;
            tag.appendChild(hidden);

            const label = document.createElement('span');
            label.textContent = text;
            tag.appendChild(label);

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'hover:text-red-600';
            removeBtn.textContent = '×';
            removeBtn.addEventListener('click', function () { tag.remove(); });
            tag.appendChild(removeBtn);

            container.insertBefore(tag, input);
        }
    });

    // شمارنده‌ی زنده‌ی حداقل کاراکتر متن نظر
    document.querySelectorAll('textarea[data-min-length]').forEach(function (textarea) {
        var min = parseInt(textarea.dataset.minLength, 10) || 0;
        var counter = document.getElementById(textarea.dataset.counterTarget);
        if (!counter) return;

        function update() {
            var remaining = min - textarea.value.length;
            if (remaining > 0) {
                counter.textContent = 'حداقل ' + min + ' کاراکتر نیاز است. (' + remaining + ' کاراکتر باقیمانده)';
                counter.classList.add('text-gray-400');
                counter.classList.remove('text-success');
            } else {
                counter.textContent = 'متن شما آماده ارسال است.';
                counter.classList.remove('text-gray-400');
                counter.classList.add('text-success');
            }
        }
        textarea.addEventListener('input', update);
        update();
    });
});
