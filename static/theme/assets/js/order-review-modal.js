/*
 * مودال دومرحله‌ای «ثبت/ویرایش امتیاز و دیدگاه» در صفحه‌ی جزئیات سفارش (templates/orders/order_full_detail.html).
 *
 * مرحله ۱: انتخاب ستاره (دکمه‌ی «ادامه» تا انتخاب حداقل یک ستاره غیرفعال است).
 * مرحله ۲: ستاره‌های قابل‌اصلاح + متن دیدگاه + نقاط قوت/ضعف (اختیاری) + تصاویر (حداکثر ۳) + ثبت با AJAX.
 *
 * حالت ایجاد: POST به reviews:create (data-create-url). حالت ویرایش: اگر کارت data-review (JSON نظر فعلی) داشته باشد،
 * مودال مستقیم روی مرحله ۲ و با مقادیر قبلی باز می‌شود و «ثبت تغییرات» به reviews:edit (edit_url) می‌رود؛ بدون ریدایرکت.
 *
 * پیش‌نویس (امتیاز، متن، نقاط قوت/ضعف، مرحله) به‌ازای هر «سفارش/کالا/حالت» در sessionStorage نگه داشته می‌شود: بستن
 * مودال با کلیک روی پس‌زمینه (یا Esc) آن را حذف نمی‌کند و بازشدن دوباره مستقیم روی همان مرحله و همان مقادیر است؛ فقط
 * فلش بازگشتِ مرحله ۲ کاربر را به مرحله ۱ می‌برد. تصاویر انتخاب‌شده فایل‌اند و در پیش‌نویس ذخیره نمی‌شوند.
 * پس از ثبت موفق پیش‌نویس پاک و کارت کالا بدون رفرش به «دیدگاه شما ثبت شد / ویرایش دیدگاه» تغییر می‌کند.
 */
(function () {
    'use strict';

    var modal = document.getElementById('reviewModal');
    if (!modal) return;

    var MAX_IMAGES = 3;
    var MAX_POINTS = 5;
    var MIN_BODY = parseInt(modal.dataset.minBody, 10) || 4;
    var LABELS = ['', 'خیلی بد', 'بد', 'معمولی', 'خوب', 'عالی'];
    var NETWORK_ERROR_MESSAGE = 'ارتباط با سرور برقرار نشد. اتصال اینترنت خود را بررسی کنید.';

    var form = modal.querySelector('#rmForm');
    var starButtons = Array.prototype.slice.call(modal.querySelectorAll('[data-rm-star]'));
    var stepEls = Array.prototype.slice.call(modal.querySelectorAll('[data-rm-step]'));
    var stepNo = modal.querySelector('[data-rm-stepno]');
    var titleEl = modal.querySelector('[data-rm-title]');
    var backBtn = modal.querySelector('[data-rm-back]');
    var closeBtn = modal.querySelector('[data-rm-close]');
    var continueBtn = modal.querySelector('[data-rm-continue]');
    var submitBtn = modal.querySelector('[data-rm-submit]');
    var editNotice = modal.querySelector('[data-rm-edit-notice]');
    var ratingLabel = modal.querySelector('[data-rm-rating-label]');
    var bodyEl = modal.querySelector('[data-rm-body]');
    var errorBox = modal.querySelector('[data-rm-error]');
    var fileInput = modal.querySelector('[data-rm-file]');
    var thumbsEl = modal.querySelector('[data-rm-thumbs]');
    var addBtn = modal.querySelector('[data-rm-add]');
    var productImg = modal.querySelector('[data-rm-product-img]');
    var productName = modal.querySelector('[data-rm-product-name]');
    var dialog = modal.querySelector('.rm-dialog');
    var pointBoxes = {};
    Array.prototype.forEach.call(modal.querySelectorAll('[data-rm-points]'), function (box) {
        pointBoxes[box.dataset.rmPoints] = {
            input: box.querySelector('[data-rm-point-input]'),
            add: box.querySelector('[data-rm-point-add]'),
            list: box.querySelector('[data-rm-point-list]'),
        };
    });

    // state: {container, orderId, productId, mode, review, rating, step, body, pros, cons, files, existing, removed}
    var state = null;
    var lastTrigger = null;
    var submitting = false;

    function readReview(container) {
        try { return container.dataset.review ? JSON.parse(container.dataset.review) : null; } catch (e) { return null; }
    }

    // ---------- پیش‌نویس ----------
    function draftKey(s) { return 'orderReviewDraft:' + s.orderId + ':' + s.productId + ':' + s.mode; }

    function loadDraft(s) {
        try {
            var raw = window.sessionStorage.getItem(draftKey(s));
            return raw ? JSON.parse(raw) : null;
        } catch (e) { return null; }
    }

    function saveDraft() {
        if (!state) return;
        try {
            var empty = !state.rating && !state.body && !state.pros.length && !state.cons.length;
            if (empty) { window.sessionStorage.removeItem(draftKey(state)); return; }
            window.sessionStorage.setItem(draftKey(state), JSON.stringify({
                rating: state.rating, step: state.step, body: state.body,
                pros: state.pros, cons: state.cons, removed: state.removed,
            }));
        } catch (e) { /* حالت خصوصی/ممنوع بودن storage: مودال بدون پیش‌نویس هم کار می‌کند */ }
    }

    function clearDrafts(s) {
        ['create', 'edit'].forEach(function (mode) {
            try { window.sessionStorage.removeItem('orderReviewDraft:' + s.orderId + ':' + s.productId + ':' + mode); } catch (e) { /* ignore */ }
        });
    }

    // ---------- نمایش ----------
    function keptExisting() {
        return state.existing.filter(function (img) { return state.removed.indexOf(img.id) === -1; });
    }

    function render() {
        starButtons.forEach(function (btn, i) {
            var on = i + 1 <= state.rating;
            btn.classList.toggle('is-on', on);
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        ratingLabel.textContent = state.rating ? LABELS[state.rating] : 'روی ستاره‌ها بزنید';
        continueBtn.disabled = !state.rating;
        stepEls.forEach(function (el) { el.hidden = parseInt(el.dataset.rmStep, 10) !== state.step; });
        stepNo.hidden = state.step !== 2;
        backBtn.hidden = state.step !== 2;
        closeBtn.hidden = state.step === 2;       // مرحله ۲: فقط فلش بازگشت (طبق طراحی دیجی‌کالا، بدون ضربدر)
        var editing = state.mode === 'edit';
        titleEl.textContent = editing ? 'ویرایش امتیاز و دیدگاه' : 'ثبت امتیاز و دیدگاه';
        submitBtn.textContent = editing ? 'ثبت تغییرات' : 'ثبت امتیاز و دیدگاه';
        editNotice.hidden = !editing;
    }

    function showError(message) {
        errorBox.textContent = message;
        errorBox.hidden = false;
    }

    function hideError() { errorBox.hidden = true; }

    function renderPoints(kind) {
        var box = pointBoxes[kind];
        box.list.innerHTML = '';
        state[kind].forEach(function (text, index) {
            var li = document.createElement('li');
            li.className = 'rm-chip';
            var span = document.createElement('span');
            span.textContent = text;
            var remove = document.createElement('button');
            remove.type = 'button';
            remove.setAttribute('aria-label', 'حذف');
            remove.textContent = '×';
            remove.addEventListener('click', function () {
                state[kind].splice(index, 1);
                renderPoints(kind);
                saveDraft();
            });
            li.appendChild(span);
            li.appendChild(remove);
            box.list.appendChild(li);
        });
    }

    function addPoint(kind) {
        var box = pointBoxes[kind];
        var text = box.input.value.trim();
        if (!text) return true;
        if (state[kind].length >= MAX_POINTS) { showError('حداکثر ' + MAX_POINTS + ' مورد برای هر بخش می‌توانید بنویسید.'); return false; }
        state[kind].push(text);
        box.input.value = '';
        hideError();
        renderPoints(kind);
        saveDraft();
        return true;
    }

    function clearThumbs() {
        Array.prototype.slice.call(thumbsEl.querySelectorAll('[data-rm-thumb]')).forEach(function (el) {
            var img = el.querySelector('img');
            if (img && img.dataset.blob) URL.revokeObjectURL(img.src);
            el.remove();
        });
    }

    function makeThumb(src, isBlob, label, onRemove) {
        var wrap = document.createElement('div');
        wrap.className = 'rm-thumb';
        wrap.setAttribute('data-rm-thumb', '');
        var img = document.createElement('img');
        img.src = src;
        if (isBlob) img.dataset.blob = '1';
        img.alt = label;
        var remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'rm-thumb-remove';
        remove.setAttribute('aria-label', 'حذف تصویر');
        remove.textContent = '×';
        remove.addEventListener('click', onRemove);
        wrap.appendChild(img);
        wrap.appendChild(remove);
        thumbsEl.insertBefore(wrap, addBtn);
    }

    function renderThumbs() {
        clearThumbs();
        keptExisting().forEach(function (img, index) {
            makeThumb(img.url, false, 'تصویر ثبت‌شده ' + (index + 1), function () {
                state.removed.push(img.id);
                renderThumbs();
                saveDraft();
            });
        });
        state.files.forEach(function (file, index) {
            makeThumb(URL.createObjectURL(file), true, 'پیش‌نمایش تصویر ' + (index + 1), function () {
                state.files.splice(index, 1);
                renderThumbs();
            });
        });
        addBtn.hidden = keptExisting().length + state.files.length >= MAX_IMAGES;
    }

    // ---------- باز و بسته ----------
    function open(container, presetRating, trigger) {
        var review = readReview(container);
        state = {
            container: container,
            orderId: container.dataset.orderId,
            productId: container.dataset.productId,
            mode: review ? 'edit' : 'create',
            review: review,
            rating: 0, step: 1, body: '', pros: [], cons: [], files: [],
            existing: review ? (review.images || []) : [], removed: [],
        };
        if (review) {
            // ویرایش: مستقیم مرحله ۲ با مقادیر ثبت‌شده
            state.rating = review.rating || 0;
            state.body = review.body || '';
            state.pros = (review.pros || []).slice();
            state.cons = (review.cons || []).slice();
            state.step = 2;
        }
        var draft = loadDraft(state);
        if (draft) {
            state.rating = draft.rating || 0;
            state.body = draft.body || '';
            state.pros = draft.pros || [];
            state.cons = draft.cons || [];
            state.removed = draft.removed || [];
            // بازگشت به مرحله ۲ فقط وقتی امتیاز دارد؛ مرحله‌ای که کاربر آخر روی آن بود حفظ می‌شود
            state.step = draft.step === 2 && state.rating ? 2 : 1;
        }
        if (presetRating && !review) state.rating = presetRating;

        productImg.src = container.dataset.productImage || '';
        productImg.alt = container.dataset.productName || '';
        productName.textContent = container.dataset.productName || '';
        bodyEl.value = state.body;
        pointBoxes.pros.input.value = '';
        pointBoxes.cons.input.value = '';
        hideError();
        renderPoints('pros');
        renderPoints('cons');
        renderThumbs();
        render();

        lastTrigger = trigger || null;
        modal.hidden = false;
        document.documentElement.classList.add('rm-lock');
        // کمی صبر تا transition باز شدن اجرا شود (setTimeout، نه rAF: در تب پس‌زمینه rAF اجرا نمی‌شود و مودال نامرئی می‌ماند)
        window.setTimeout(function () { modal.classList.add('is-open'); }, 16);
        window.setTimeout(function () { (state.step === 2 ? bodyEl : dialog).focus(); }, 30);
    }

    function close() {
        if (!state || submitting) return;
        saveDraft();
        modal.classList.remove('is-open');
        document.documentElement.classList.remove('rm-lock');
        window.setTimeout(function () {
            modal.hidden = true;
            state.files.length = 0;
            clearThumbs();
            if (lastTrigger && document.contains(lastTrigger)) lastTrigger.focus();
            state = null;
        }, 180);
    }

    function goStep(step) {
        state.step = step;
        hideError();
        saveDraft();
        render();
        if (step === 2) bodyEl.focus();
    }

    function setRating(value) {
        state.rating = value;
        hideError();
        saveDraft();
        render();
    }

    // ---------- رویدادها ----------
    document.addEventListener('click', function (e) {
        var star = e.target.closest('[data-review-star]');
        var trigger = e.target.closest('[data-review-trigger]');
        var el = star || trigger;
        if (!el) return;
        var container = el.closest('[data-review-item]');
        if (!container) return;
        e.preventDefault();
        open(container, star ? parseInt(star.dataset.reviewStar, 10) : 0, el);
    });

    starButtons.forEach(function (btn, i) {
        btn.addEventListener('click', function () { setRating(i + 1); });
        btn.addEventListener('keydown', function (e) {
            // RTL: فلش چپ = امتیاز بیشتر
            if (e.key === 'ArrowLeft' && state.rating < 5) { setRating(state.rating + 1); starButtons[state.rating - 1].focus(); e.preventDefault(); }
            if (e.key === 'ArrowRight' && state.rating > 1) { setRating(state.rating - 1); starButtons[state.rating - 1].focus(); e.preventDefault(); }
        });
    });

    continueBtn.addEventListener('click', function () { if (state.rating) goStep(2); });
    backBtn.addEventListener('click', function () { goStep(1); });
    closeBtn.addEventListener('click', close);

    modal.addEventListener('mousedown', function (e) {
        // فقط کلیک روی خودِ پس‌زمینه (نه کشیدن ماوس از داخل دیالوگ به بیرون) مودال را می‌بندد
        if (e.target === modal) close();
    });

    document.addEventListener('keydown', function (e) {
        if (modal.hidden) return;
        if (e.key === 'Escape') { close(); return; }
        if (e.key === 'Tab') {
            var focusable = Array.prototype.slice.call(dialog.querySelectorAll('button:not([disabled]), textarea, input[type=text], a[href]'))
                .filter(function (el) { return !el.hidden && el.offsetParent !== null; });
            if (!focusable.length) return;
            var first = focusable[0], last = focusable[focusable.length - 1];
            if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
            else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
        }
    });

    bodyEl.addEventListener('input', function () {
        state.body = bodyEl.value;
        hideError();
        saveDraft();
    });

    Object.keys(pointBoxes).forEach(function (kind) {
        var box = pointBoxes[kind];
        box.add.addEventListener('click', function () { addPoint(kind); box.input.focus(); });
        box.input.addEventListener('keydown', function (e) {
            // Enter نقطه را اضافه می‌کند و فرم را ارسال نمی‌کند
            if (e.key === 'Enter') { e.preventDefault(); addPoint(kind); }
        });
    });

    addBtn.addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function () {
        var picked = Array.prototype.slice.call(fileInput.files).filter(function (f) { return /^image\//.test(f.type); });
        fileInput.value = '';
        var room = MAX_IMAGES - keptExisting().length - state.files.length;
        if (picked.length > room) showError('حداکثر ' + MAX_IMAGES + ' تصویر می‌توانید پیوست کنید.');
        state.files = state.files.concat(picked.slice(0, Math.max(room, 0)));
        renderThumbs();
    });

    // ---------- ثبت ----------
    function applySuccess(container, data, wasEdit) {
        var rating = data.rating || 0;
        container.querySelectorAll('.od-star').forEach(function (svg, i) {
            svg.classList.toggle('is-on', i + 1 <= rating);
        });
        container.querySelectorAll('[data-review-star]').forEach(function (btn) {
            btn.removeAttribute('data-review-star');
            btn.disabled = true;
            btn.tabIndex = -1;
        });
        var hint = container.querySelector('.od-rate-hint');
        if (hint) hint.textContent = wasEdit
            ? 'تغییرات شما ثبت شد و پس از تأیید نمایش داده می‌شود'
            : 'دیدگاه شما ثبت شد و پس از تأیید نمایش داده می‌شود';
        var link = container.querySelector('[data-review-trigger]');
        if (link) {
            link.textContent = 'ویرایش دیدگاه';
            if (data.edit_url) link.href = data.edit_url;
        }
        // دفعه‌ی بعد مودال در حالت ویرایش و با همین مقادیر باز شود
        var snapshot = {};
        ['review_id', 'rating', 'status', 'title', 'body', 'pros', 'cons', 'images', 'edit_url'].forEach(function (k) { snapshot[k] = data[k]; });
        container.dataset.review = JSON.stringify(snapshot);
        container.classList.add('is-reviewed');
    }

    function flushPendingPoints() {
        return addPoint('pros') && addPoint('cons');
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (submitting || !state) return;
        hideError();
        if (!flushPendingPoints()) return;
        if (!state.rating) { showError('لطفاً یک امتیاز بین ۱ تا ۵ ستاره انتخاب کنید.'); return; }
        if (state.body.trim().length < MIN_BODY) { showError('متن دیدگاه باید حداقل ' + MIN_BODY + ' کاراکتر باشد.'); bodyEl.focus(); return; }

        var editing = state.mode === 'edit';
        var data = new FormData();
        data.append('csrfmiddlewaretoken', form.querySelector('[name=csrfmiddlewaretoken]').value);
        data.append('rating', String(state.rating));
        data.append('body', state.body.trim());
        // سرور ویرایش عنوان را هم بازنویسی می‌کند؛ عنوان قبلی را (که مودال ویرایش نمی‌کند) نگه می‌داریم
        if (editing) data.append('title', (state.review && state.review.title) || '');
        state.pros.forEach(function (t) { data.append('pros', t); });
        state.cons.forEach(function (t) { data.append('cons', t); });
        state.files.forEach(function (f) { data.append('images', f); });
        if (editing) state.removed.forEach(function (id) { data.append('remove_image', String(id)); });

        var current = state;
        var url = editing ? current.review.edit_url : current.container.dataset.createUrl;
        submitting = true;
        submitBtn.disabled = true;
        submitBtn.classList.add('is-busy');
        fetch(url, {
            method: 'POST', body: data, headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin',
        })
            .then(function (res) { return res.json().then(function (json) { return { json: json }; }); })
            .then(function (result) {
                var json = result.json;
                submitting = false;
                submitBtn.classList.remove('is-busy');
                submitBtn.disabled = false;
                if (json.ok) {
                    clearDrafts(current);
                    applySuccess(current.container, json, editing);
                    state.body = ''; state.rating = 0; state.pros = []; state.cons = [];   // close() دوباره پیش‌نویس ننویسد
                    close();
                } else if (json.error === 'already_reviewed') {
                    clearDrafts(current);
                    window.location.reload();     // قبلاً نظر داده؛ کارت را از سرور تازه بخوان
                } else {
                    showError(json.message || 'خطایی رخ داد. لطفاً دوباره تلاش کنید.');
                }
            })
            .catch(function () {
                submitting = false;
                submitBtn.classList.remove('is-busy');
                submitBtn.disabled = false;
                showError(NETWORK_ERROR_MESSAGE);
            });
    });
})();
