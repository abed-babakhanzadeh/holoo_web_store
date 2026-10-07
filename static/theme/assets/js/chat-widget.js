/*
 * ویجت گفتگوی آنلاین: دکمه‌ی شناور با انیمیشن‌ها، حباب، قابل بستن، و پنجره‌ی سه‌زبانه (گفتگوی آنلاین / پیام آفلاین / چت هوشمند).
 * همه‌ی متن‌ها و تنظیمات از GET /chat/config/ می‌آید (پنل ادمین). فاز ۲: ثبت پیام آفلاین، مشاهده‌ی پاسخ کارشناس در همین ویجت
 * (پولینگ سبک با after=<seq>: پنجره‌ی باز هر چند ثانیه، پنجره‌ی بسته فقط وقتی گفتگو دارد و فاصله‌اش صفر نباشد، تب مخفی متوقف)،
 * تعداد خوانده‌نشده روی دکمه و علامت «خوانده شد».
 * فاز ۳: گفتگوی زنده (فرم زبانه‌ی زنده وقتی ساعت کاری و کارشناس آنلاین است؛ وضعیت دسترسی هنگام باز کردن پنجره تازه می‌شود)،
 * یک نمای گفتگوی مشترک بین دو زبانه، نشانگر «در حال نوشتن» (هر ۳ ثانیه یک‌بار، فقط Redis) و عقب‌نشینی نمایی پولینگ هنگام خطا.
 * فاز ۴: پیوست (تصویر/PDF طبق تنظیمات؛ اعتبارسنجی و پاک‌سازی اصلی سمت سرور است) و حالت «مسدود» (فرم و کادر پیام غیرفعال).
 *
 * بدون کتابخانه و CDN. همه‌ی متن‌های پویا با textContent درج می‌شوند (ضد XSS)؛ فقط SVG آواتارهای استاتیکِ خودِ سایت
 * با innerHTML می‌آید (همان‌مبدأ و ثابت).
 */
(function () {
    'use strict';

    var root = document.getElementById('chat-root');
    if (!root) { return; }

    var LS_DISMISS = 'chat_launcher_dismissed_until';
    var SS_BUBBLE = 'chat_bubble_seen';
    var MOBILE = window.matchMedia ? window.matchMedia('(max-width: 1023.98px)') : {matches: false};

    var cfg = null;
    var panel = null;
    var launcherBtn = null;
    var bubbleEl = null;
    var state = {open: false, tab: null, bubbleTimer: null, attnTimer: null,
                 conv: null, blocked: false, lastSeq: 0, pollTimer: null, lastActivity: Date.now(), polling: false, failures: 0,
                 lastTypingSent: 0, lastStateAt: 0, ui: {forms: {}}};

    /* ---------- ابزارها ---------- */
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = text; }
        return node;
    }

    function store(kind) {
        try { return window[kind]; } catch (error) { return null; }
    }

    function read(kind, key) {
        try { var s = store(kind); return s ? s.getItem(key) : null; } catch (error) { return null; }
    }

    function write(kind, key, value) {
        try { var s = store(kind); if (s) { s.setItem(key, value); } } catch (error) { /* حالت خصوصی/بلاک: بی‌خطر */ }
    }

    function toLatin(text) {
        return String(text || '').replace(/[۰-۹]/g, function (c) { return c.charCodeAt(0) - 0x06F0; })
            .replace(/[٠-٩]/g, function (c) { return c.charCodeAt(0) - 0x0660; });
    }

    var ICONS = {
        chat: '<path stroke-linecap="round" stroke-linejoin="round" d="M8 10h8M8 14h5m-8.5 6 3-3H17a3 3 0 0 0 3-3V7a3 3 0 0 0-3-3H7a3 3 0 0 0-3 3v10c0 .8.4 1.5 1 2Z"/>',
        mail: '<path stroke-linecap="round" stroke-linejoin="round" d="M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Zm0 0 9 6 9-6"/>',
        spark: '<path stroke-linecap="round" stroke-linejoin="round" d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3Zm7 11 .8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14Z"/>',
        close: '<path stroke-linecap="round" stroke-linejoin="round" d="M6 6l12 12M18 6 6 18"/>',
        send: '<path stroke-linecap="round" stroke-linejoin="round" d="M5 12l14-7-5 14-2.5-5.5L5 12Z"/>',
        attach: '<path stroke-linecap="round" stroke-linejoin="round" d="m21 11.5-8.6 8.6a5 5 0 0 1-7.1-7.1l8.9-8.9a3.3 3.3 0 0 1 4.7 4.7l-8.9 8.9a1.7 1.7 0 0 1-2.4-2.4l8.2-8.2"/>',
        clock: '<path stroke-linecap="round" stroke-linejoin="round" d="M12 7v5l3 2m6-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"/>',
        headset: '<path stroke-linecap="round" stroke-linejoin="round" d="M4 14v-2a8 8 0 0 1 16 0v2M4 14h2a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-4Zm16 0h-2a1 1 0 0 0-1 1v3a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1v-4Zm-3 6c0 1.1-1.8 2-4 2"/>'
    };

    function icon(name) {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '1.8');
        svg.setAttribute('aria-hidden', 'true');
        svg.innerHTML = ICONS[name] || '';
        return svg;
    }

    /* رنگ متن روی رنگ اصلی: سفید، مگر رنگ بسیار روشن باشد */
    function luminance(hex) {
        var m = /^#([0-9a-f]{6})$/i.exec(hex || '');
        if (!m) { return 0.3; }
        var parts = [0, 2, 4].map(function (i) {
            var v = parseInt(m[1].substr(i, 2), 16) / 255;
            return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2];
    }

    function darken(hex, factor) {
        var m = /^#([0-9a-f]{6})$/i.exec(hex || '');
        if (!m) { return hex; }
        var out = [0, 2, 4].map(function (i) {
            var v = Math.round(parseInt(m[1].substr(i, 2), 16) * (1 - factor));
            return ('0' + v.toString(16)).slice(-2);
        });
        return '#' + out.join('');
    }

    /* ---------- شبکه (JSON، CSRF از کوکی) ---------- */
    function csrfToken() {
        var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    function api(method, path, body) {
        var options = {method: method, credentials: 'same-origin', headers: {'Accept': 'application/json'}};
        if (method !== 'GET') {
            options.headers['X-CSRFToken'] = csrfToken();
            if (window.FormData && body instanceof FormData) {
                options.body = body;                          // multipart (پیوست): Content-Type را مرورگر با boundary می‌گذارد
            } else {
                options.headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(body || {});
            }
        }
        return fetch(path, options).then(function (response) {
            return response.json().catch(function () { return {ok: false, message: 'پاسخ نامعتبر از سرور.'}; })
                .then(function (data) { data.status = response.status; return data; });
        });
    }

    function newId() {
        if (window.crypto && window.crypto.randomUUID) { return window.crypto.randomUUID(); }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            var r = Math.random() * 16 | 0;
            return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }

    function fmtTime(iso) {
        if (!iso) { return ''; }
        var d = new Date(iso);
        try { return new Intl.DateTimeFormat('fa-IR', {hour: '2-digit', minute: '2-digit'}).format(d); } catch (error) { return ''; }
    }

    /* ---------- آواتار (SVG استاتیک inline تا CSS بتواند حرکتش بدهد؛ اختصاصی با <img>) ---------- */
    function fillAvatar(host, avatar) {
        if (avatar.kind === 'custom') {
            var img = el('img');
            img.src = avatar.url;
            img.alt = '';
            img.decoding = 'async';
            host.appendChild(img);
            return Promise.resolve();
        }
        return fetch(avatar.url, {credentials: 'same-origin'}).then(function (r) {
            if (!r.ok) { throw new Error('avatar'); }
            return r.text();
        }).then(function (markup) {
            if (markup.indexOf('<svg') === -1) { throw new Error('avatar'); }
            host.innerHTML = markup;
        }).catch(function () {
            host.appendChild(icon('headset'));         // خرابی بارگذاری آواتار نباید ویجت را بشکند
        });
    }

    /* ---------- وضعیت نمایش ---------- */
    function dismissedNow() {
        var until = parseInt(read('localStorage', LS_DISMISS), 10);
        return until && until > Date.now();
    }

    function motionAllowed() {
        if (!cfg.anim.enabled) { return false; }
        if (cfg.anim.respect_reduced_motion && window.matchMedia &&
            window.matchMedia('(prefers-reduced-motion: reduce)').matches) { return false; }
        return true;
    }

    function applyPosition() {
        var p = cfg.position;
        var mobile = MOBILE.matches;
        var x = mobile && p.x_mobile !== null ? p.x_mobile : p.x;
        var y = mobile && p.y_mobile !== null ? p.y_mobile : p.y;
        root.style.setProperty('--cw-x', x + 'px');
        root.style.setProperty('--cw-y', y + 'px');
        // موبایل با فاصله‌ی خودکار: بالای منوی پایین (ارتفاعش اندازه‌گیری می‌شود)
        var nav = document.getElementById('mobile-bottom-nav');
        var auto = mobile && p.y_mobile === null && nav && nav.offsetHeight > 0 ? nav.offsetHeight : 0;
        root.style.setProperty('--cw-nav', auto + 'px');
    }

    /* ---------- حباب و جلب‌توجه ---------- */
    function startBubble() {
        var b = cfg.bubble;
        if (!cfg.anim.bubble || !b.messages.length || read('sessionStorage', SS_BUBBLE)) { return; }
        var index = 0;

        function show() {
            if (state.open) { return; }
            bubbleEl.textContent = b.messages[index % b.messages.length];
            bubbleEl.classList.add('is-on');
            index += 1;
            state.bubbleTimer = setTimeout(hide, Math.min(6000, Math.max(2500, b.interval * 1000 - 1200)));
        }

        function hide() {
            bubbleEl.classList.remove('is-on');
            state.bubbleTimer = setTimeout(show, Math.max(1200, b.interval * 1000 - 3000));
        }

        state.bubbleTimer = setTimeout(show, b.first_delay * 1000);
    }

    function stopBubble() {
        clearTimeout(state.bubbleTimer);
        bubbleEl.classList.remove('is-on');
    }

    function startAttention() {
        if (!motionAllowed() || (!cfg.anim.pulse && !cfg.anim.wave)) { return; }
        function burst() {
            if (state.open || document.hidden) { return; }
            root.classList.add('cw-attn');
            setTimeout(function () { root.classList.remove('cw-attn'); }, 3200);
        }
        setTimeout(burst, 2500);
        state.attnTimer = setInterval(burst, cfg.anim.attention_interval * 1000);
    }

    /* ---------- محتوای زبانه‌ها ---------- */
    function liveAvailable() { return !!(cfg.availability && cfg.availability.live); }

    function subtitleText() {
        return liveAvailable() ? cfg.texts.subtitle_online : cfg.texts.subtitle_offline;
    }

    function buildLivePane(pane) {
        var bot = el('div', 'cw-bot');
        var av = el('span', 'cw-bot-av');
        bot.appendChild(av);
        bot.appendChild(el('div', 'cw-msg', cfg.texts.welcome));
        fillAvatar(av, cfg.avatar);
        pane.appendChild(bot);
        state.ui.liveBot = bot;
        var holder = el('div');
        pane.appendChild(holder);
        state.ui.liveHolder = holder;
        state.ui.forms.live = holder;
        renderLiveHolder();
    }

    // وضعیت ۱ (کارشناس آنلاین): فرم شروع گفتگوی زنده. وضعیت ۲ و ۳: کارت توضیح + رفتن به فرم آفلاین
    function renderLiveHolder() {
        var holder = state.ui.liveHolder;
        if (!holder) { return; }
        var hiddenNow = holder.hidden;
        holder.textContent = '';
        if (liveAvailable()) {
            buildForm(holder, 'live');
        } else {
            var card = el('div', 'cw-card');
            var icn = el('div', 'cw-card-icon');
            icn.appendChild(icon('clock'));
            card.appendChild(icn);
            card.appendChild(el('p', '', cfg.availability.state === 'after_hours' ? cfg.texts.after_hours : cfg.texts.no_operator));
            var hours = cfg.hours.today_label;
            if (cfg.availability.state === 'after_hours' && cfg.hours.next_open_label) {
                hours += ' · شروع بعدی: ' + cfg.hours.next_open_label;
            }
            if (hours) { card.appendChild(el('div', 'cw-hours', hours)); }
            if (hasTab('offline')) {
                var go = el('button', 'cw-link-btn', cfg.texts.to_offline);
                go.type = 'button';
                go.addEventListener('click', function () { selectTab('offline', true); });
                card.appendChild(go);
            }
            holder.appendChild(card);
        }
        holder.hidden = hiddenNow;
    }

    // دسترسی ممکن است بعد از بار شدن صفحه عوض شود (کارشناس آنلاین/آفلاین شد، ساعت کاری تمام شد)
    function applyAvailability(availability) {
        if (!availability) { return; }
        var before = liveAvailable();
        cfg.availability = availability;
        if (before === liveAvailable()) { return; }
        if (state.ui.subDot) { state.ui.subDot.className = liveAvailable() ? 'is-online' : ''; }
        if (state.ui.subText) { state.ui.subText.textContent = subtitleText(); }
        if (state.ui.launcherDot) { state.ui.launcherDot.className = 'cw-dot' + (liveAvailable() ? ' is-online' : ''); }
        renderLiveHolder();
    }

    function refreshState(force) {
        if (!cfg.api || (!force && Date.now() - state.lastStateAt < 20000)) { return Promise.resolve(); }
        state.lastStateAt = Date.now();
        return api('GET', cfg.api.state).then(function (data) {
            if (!data.ok || !data.enabled) { return; }
            applyAvailability(data.availability);
            applyBlocked(data.blocked);
            if (!data.conversation && state.conv) { setConversation(null); state.lastSeq = 0; if (state.ui.messagesEl) { state.ui.messagesEl.textContent = ''; } refreshTabView(); }
        }).catch(function () { /* ساکت */ });
    }

    function field(labelText, input, errorId) {
        var wrap = el('div');
        var label = el('label', '', labelText);
        label.setAttribute('for', input.id);
        wrap.appendChild(label);
        wrap.appendChild(input);
        var error = el('div', 'cw-field-error');
        error.id = errorId;
        error.hidden = true;
        wrap.appendChild(error);
        return {wrap: wrap, error: error};
    }

    /* ---------- زبانه‌ی پیام آفلاین: فرم اولیه ← نمای گفتگو (پیام‌ها و پاسخ‌ها) ---------- */
    function buildOfflinePane(pane) {
        var formView = el('div');
        pane.appendChild(formView);
        state.ui.forms.offline = formView;
        buildForm(formView, 'offline');
    }

    // گفتگوی جاری فقط یکی است و بین زبانه‌ی زنده و آفلاین مشترک است: همان عنصر به زبانه‌ی فعال منتقل می‌شود
    function isThreadTab() { return state.tab === 'offline' || state.tab === 'live'; }

    function showThread() {
        var pane = panel && panel.querySelector('[data-pane="' + state.tab + '"]');
        if (!pane || !isThreadTab() || !state.ui.threadView) { return; }
        Object.keys(state.ui.forms).forEach(function (key) { state.ui.forms[key].hidden = true; });
        if (state.ui.liveBot) { state.ui.liveBot.hidden = true; }              // فضا برای خودِ گفتگو
        if (state.ui.threadView.parentNode !== pane) { pane.appendChild(state.ui.threadView); }
        state.ui.threadView.hidden = false;
        scrollThread();
    }

    function showForm() {
        if (state.ui.threadView) { state.ui.threadView.hidden = true; }
        Object.keys(state.ui.forms).forEach(function (key) { state.ui.forms[key].hidden = false; });
        if (state.ui.liveBot) { state.ui.liveBot.hidden = false; }
        state.ui.formShownAt = Date.now();
    }

    function refreshTabView() {
        if (!isThreadTab()) { return; }
        if (state.conv && !state.conv.closed) { showThread(); } else { showForm(); }
    }

    function buildForm(host, channel) {
        host.appendChild(el('p', 'cw-hours', channel === 'live' ? cfg.texts.live_intro : cfg.texts.offline_intro));
        var form = el('form', 'cw-form');
        form.noValidate = true;
        var controls = {};
        state.ui.formShownAt = Date.now();

        function addInput(name, mode, placeholder, attrs) {
            if (mode === 'hidden' || cfg.viewer.authenticated) { return; }     // کاربر واردشده از حساب خوانده می‌شود
            var input = el('input');
            input.id = 'cw-' + channel + '-' + name;
            input.name = name;
            input.type = 'text';
            input.placeholder = placeholder;
            input.autocomplete = attrs.autocomplete;
            if (attrs.inputmode) { input.setAttribute('inputmode', attrs.inputmode); }
            if (attrs.dir) { input.dir = attrs.dir; }
            input.required = mode === 'required';
            var f = field(placeholder + (mode === 'required' ? ' *' : ''), input, 'cw-' + channel + '-err-' + name);
            controls[name] = {input: input, error: f.error, mode: mode};
            form.appendChild(f.wrap);
        }

        addInput('name', cfg.guest_form.name, cfg.texts.name_placeholder, {autocomplete: 'name'});
        addInput('phone', cfg.guest_form.phone, cfg.texts.phone_placeholder, {autocomplete: 'tel', inputmode: 'tel', dir: 'ltr'});

        var area = el('textarea');
        area.id = 'cw-' + channel + '-message';
        area.name = 'message';
        area.rows = 4;
        area.maxLength = cfg.limits.message_max_length;
        area.placeholder = cfg.texts.message_placeholder;
        var fm = field(cfg.texts.message_placeholder.replace(/[…\.]+$/, ''), area, 'cw-' + channel + '-err-message');
        form.appendChild(fm.wrap);
        var count = el('div', 'cw-count', '0 / ' + cfg.limits.message_max_length);
        fm.wrap.insertBefore(count, fm.error);
        area.addEventListener('input', function () { count.textContent = area.value.length + ' / ' + cfg.limits.message_max_length; });

        // تله‌ی ربات‌ها: کادری که کاربر واقعی هرگز نمی‌بیند و پر نمی‌کند
        var trap = el('input', 'cw-trap');
        trap.type = 'text';
        trap.name = 'website';
        trap.tabIndex = -1;
        trap.autocomplete = 'off';
        trap.setAttribute('aria-hidden', 'true');
        form.appendChild(trap);

        if (cfg.texts.privacy) { form.appendChild(el('div', 'cw-privacy', cfg.texts.privacy)); }

        var submit = el('button', 'cw-submit', cfg.texts.send);
        submit.type = 'submit';
        form.appendChild(submit);
        var note = el('div', 'cw-note');
        note.setAttribute('role', 'status');
        note.hidden = true;
        form.appendChild(note);

        function setError(ctrl, message) {
            ctrl.error.textContent = message || '';
            ctrl.error.hidden = !message;
            if (message) { ctrl.input.setAttribute('aria-invalid', 'true'); } else { ctrl.input.removeAttribute('aria-invalid'); }
        }

        var pending = null;                       // client_msg_id همین ارسال؛ تکرار بعد از قطعی پیام دوم نمی‌سازد
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var ok = true;
            Object.keys(controls).forEach(function (key) {
                var ctrl = controls[key];
                var value = toLatin(ctrl.input.value).trim();
                var message = '';
                if (ctrl.mode === 'required' && !value) { message = 'این کادر الزامی است.'; }
                if (!message && key === 'phone' && value && !/^09\d{9}$/.test(value)) { message = 'شماره موبایل را با قالب 09123456789 وارد کنید.'; }
                setError(ctrl, message);
                if (message) { ok = false; }
            });
            if (state.blocked) { note.hidden = false; note.textContent = cfg.texts.blocked; return; }
            var text = area.value.trim();
            var messageCtrl = {input: area, error: fm.error};
            setError(messageCtrl, text ? '' : 'متن پیام را بنویسید.');
            if (!text) { ok = false; }
            if (!ok) { return; }

            pending = pending || newId();
            submit.disabled = true;
            note.hidden = true;
            api('POST', cfg.api.create, {
                channel: channel, message: text, client_msg_id: pending, page_path: location.pathname, elapsed_ms: Date.now() - state.ui.formShownAt,
                name: controls.name ? controls.name.input.value.trim() : '', phone: controls.phone ? toLatin(controls.phone.input.value).trim() : '',
                website: trap.value
            }).then(function (data) {
                submit.disabled = false;
                if (!data.ok) {
                    note.hidden = false;
                    note.textContent = data.message || 'ارسال نشد؛ دوباره تلاش کنید.';
                    if (data.code === 'too_fast' || data.code === 'conflict') { pending = null; }
                    if (data.code === 'not_available') { pending = null; refreshState(true); }
                    if (data.code === 'blocked') { applyBlocked(true); }
                    return;
                }
                pending = null;
                area.value = '';
                count.textContent = '0 / ' + cfg.limits.message_max_length;
                setConversation(data.conversation);
                state.lastSeq = 0;
                state.ui.messagesEl.textContent = '';
                // گفتگوی موجود (بازگشایی/ادامه): تاریخچه‌ی قبلی را هم می‌خواهیم؛ پولینگ از ۰ همه را می‌آورد
                if (data.message.seq === 1) { addMessages([data.message]); }
                showThread();
                pollOnce();
            }).catch(function () {
                submit.disabled = false;
                note.hidden = false;
                note.textContent = 'اتصال برقرار نشد؛ دوباره تلاش کنید.';
            });
        });
        host.appendChild(form);
    }

    function buildThread(host) {
        var success = el('div', 'cw-note');
        success.hidden = true;
        success.setAttribute('role', 'status');
        host.appendChild(success);
        state.ui.successNote = success;

        var messages = el('div', 'cw-msgs');
        messages.setAttribute('role', 'log');
        messages.setAttribute('aria-live', 'polite');
        host.appendChild(messages);
        state.ui.messagesEl = messages;

        var typing = el('div', 'cw-typing');
        typing.hidden = true;
        typing.setAttribute('role', 'status');
        typing.appendChild(el('i'));
        typing.appendChild(el('i'));
        typing.appendChild(el('i'));
        typing.appendChild(el('span', '', cfg.texts.typing));
        host.appendChild(typing);
        state.ui.typingEl = typing;

        var composer = el('form', 'cw-compose');
        var input = el('textarea');
        input.rows = 2;
        input.maxLength = cfg.limits.message_max_length;
        input.placeholder = cfg.texts.message_placeholder;
        input.setAttribute('aria-label', cfg.texts.message_placeholder);
        var send = el('button', 'cw-send');
        send.type = 'submit';
        send.setAttribute('aria-label', cfg.texts.send);
        send.appendChild(icon('send'));
        var chosen = [];                                          // فایل‌های انتخاب‌شده برای پیام بعدی
        var attachCfg = cfg.attachments && cfg.attachments.enabled ? cfg.attachments : null;
        var fileInput = null;
        var chips = el('div', 'cw-chips');
        chips.hidden = true;
        if (attachCfg) {
            fileInput = el('input');
            fileInput.type = 'file';
            fileInput.multiple = true;
            fileInput.accept = attachCfg.accept;
            fileInput.hidden = true;
            fileInput.setAttribute('aria-hidden', 'true');
            var clip = el('button', 'cw-attach');
            clip.type = 'button';
            clip.setAttribute('aria-label', 'افزودن پیوست');
            clip.appendChild(icon('attach'));
            clip.addEventListener('click', function () { fileInput.click(); });
            composer.appendChild(clip);
            composer.appendChild(fileInput);
        }
        composer.appendChild(input);
        composer.appendChild(send);
        host.appendChild(chips);
        host.appendChild(composer);
        state.ui.composer = composer;

        function renderChips() {
            chips.textContent = '';
            chips.hidden = !chosen.length;
            chosen.forEach(function (file, index) {
                var chip = el('span', 'cw-chip');
                chip.appendChild(el('span', 'cw-chip-name', file.name));
                var remove = el('button', '', '×');
                remove.type = 'button';
                remove.setAttribute('aria-label', 'حذف ' + file.name);
                remove.addEventListener('click', function () { chosen.splice(index, 1); renderChips(); });
                chip.appendChild(remove);
                chips.appendChild(chip);
            });
        }
        if (fileInput) {
            fileInput.addEventListener('change', function () {
                var problem = '';
                Array.prototype.forEach.call(fileInput.files, function (file) {
                    if (chosen.length >= attachCfg.max_count) { problem = 'حداکثر ' + attachCfg.max_count + ' فایل در هر پیام مجاز است.'; return; }
                    if (file.size > attachCfg.max_mb * 1024 * 1024) { problem = 'حجم هر فایل حداکثر ' + attachCfg.max_mb + ' مگابایت است.'; return; }
                    chosen.push(file);
                });
                fileInput.value = '';
                info.hidden = !problem;
                info.textContent = problem;
                renderChips();
            });
        }

        var info = el('div', 'cw-note');
        info.hidden = true;
        info.setAttribute('role', 'status');
        host.appendChild(info);
        state.ui.info = info;

        var finish = el('button', 'cw-link-btn', cfg.texts.close_conversation || 'پایان گفتگو');
        finish.type = 'button';
        finish.addEventListener('click', function () {
            if (!state.conv) { return; }
            api('POST', url(cfg.api.close, state.conv.id)).then(function (data) {
                if (data.ok) { setConversation(null); state.lastSeq = 0; messages.textContent = ''; showForm(); }
            });
        });
        host.appendChild(finish);

        input.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); composer.requestSubmit ? composer.requestSubmit() : send.click(); }
        });
        input.addEventListener('input', function () {
            markActivity();
            if (!cfg.typing_enabled || !state.conv || !input.value.trim() || Date.now() - state.lastTypingSent < 3000) { return; }
            state.lastTypingSent = Date.now();
            api('POST', url(cfg.api.typing, state.conv.id));
        });
        composer.addEventListener('submit', function (event) {
            event.preventDefault();
            var text = input.value.trim();
            if ((!text && !chosen.length) || !state.conv || send.disabled) { return; }
            send.disabled = true;
            info.hidden = true;
            var payload = {body: text, client_msg_id: newId()};
            if (chosen.length) {
                payload = new FormData();
                payload.append('body', text);
                payload.append('client_msg_id', newId());
                chosen.forEach(function (file) { payload.append('files', file, file.name); });
            }
            api('POST', url(cfg.api.send, state.conv.id), payload).then(function (data) {
                send.disabled = false;
                if (!data.ok) {
                    if (data.code === 'closed') { setConversation(null); showForm(); return; }
                    if (data.code === 'blocked') { applyBlocked(true); }
                    info.hidden = false;
                    info.textContent = data.message || 'ارسال نشد.';
                    return;
                }
                chosen = [];
                renderChips();
                input.value = '';
                setConversation(data.conversation);
                addMessages([data.message]);
                markActivity();
            }).catch(function () { send.disabled = false; info.hidden = false; info.textContent = 'اتصال برقرار نشد؛ دوباره تلاش کنید.'; });
        });
    }

    function url(template, id) { return template.replace('__ID__', id); }

    function scrollThread() {
        var box = state.ui.messagesEl;
        if (box) { box.scrollTop = box.scrollHeight; }
    }

    function addMessages(items) {
        var box = state.ui.messagesEl;
        if (!box || !items || !items.length) { return false; }
        var added = false;
        items.forEach(function (m) {
            if (m.seq <= state.lastSeq) { return; }                 // تکراری (پولینگ هم‌پوشان یا پاسخ ارسال)
            state.lastSeq = m.seq;
            added = true;
            var own = m.sender === 'customer';
            var row = el('div', 'cw-row ' + (m.sender === 'system' ? 'is-system' : own ? 'is-own' : 'is-agent'));
            var bubble = el('div', 'cw-bubble-msg');
            if (m.sender === 'operator' && m.operator) { bubble.appendChild(el('div', 'cw-agent', m.operator)); }
            if (m.body) { bubble.appendChild(el('div', 'cw-text', m.body)); }
            (m.attachments || []).forEach(function (a) {
                var link = el('a', a.kind === 'image' ? 'cw-img-link' : 'cw-file');
                link.href = a.url;
                link.target = '_blank';
                link.rel = 'noopener';
                if (a.kind === 'image') {
                    var img = el('img', 'cw-img');
                    img.loading = 'lazy';
                    img.alt = a.name || 'تصویر';
                    img.src = a.url;
                    link.appendChild(img);
                } else {
                    link.textContent = '📄 ' + (a.name || 'فایل');
                }
                bubble.appendChild(link);
            });
            bubble.appendChild(el('time', 'cw-time', fmtTime(m.at)));
            row.appendChild(bubble);
            box.appendChild(row);
        });
        if (added) { scrollThread(); }
        return added;
    }

    /* ---------- وضعیت گفتگو، تعداد خوانده‌نشده و پولینگ ---------- */
    var badgeEl = null;

    function setConversation(conversation) {
        state.conv = conversation && !conversation.closed ? conversation : null;
        updateBadge();
        updateThreadNote();
        schedulePoll();
    }

    // مسدود: کادر پیام گفتگوی جاری غیرفعال و متن مسدودی دیده می‌شود؛ فرم‌های شروع هم درخواست نمی‌فرستند
    function applyBlocked(flag) {
        state.blocked = !!flag;
        if (state.ui.composer) { state.ui.composer.hidden = state.blocked; }
        updateThreadNote();
    }

    // یادداشت بالای گفتگو: «در انتظار کارشناس» برای صف زنده، «پیام ثبت شد» برای پیام آفلاین
    function updateThreadNote() {
        var note = state.ui.successNote;
        if (!note) { return; }
        var c = state.conv;
        var text = '';
        if (state.blocked) { text = cfg.texts.blocked; }
        else if (c && c.status === 'waiting_operator') { text = cfg.texts.live_waiting; }
        else if (c && c.status === 'offline' && c.channel === 'offline') { text = cfg.texts.offline_success; }
        note.hidden = !text;
        note.textContent = text;
    }

    function updateBadge() {
        if (!badgeEl) { return; }
        var unread = state.conv ? state.conv.unread : 0;
        badgeEl.hidden = !unread || (state.open && isThreadTab());
        badgeEl.textContent = unread > 9 ? '9+' : String(unread || '');
    }

    function markActivity() { state.lastActivity = Date.now(); }

    function markRead() {
        if (!state.conv || !state.open || !isThreadTab() || document.hidden) { return; }
        if (state.conv.unread > 0 || state.conv.last_read_seq < state.lastSeq) {
            api('POST', url(cfg.api.read, state.conv.id), {upto_seq: state.lastSeq}).then(function (data) {
                if (data.ok) { setConversation(data.conversation); }
            });
        }
    }

    function pollInterval() {
        var p = cfg.poll;
        var base = state.open ? (Date.now() - state.lastActivity > 60000 ? p.idle : p.active) * 1000 : (p.closed ? p.closed * 1000 : 0);
        if (!base) { return 0; }                              // ۰ = وقتی پنجره بسته است اصلاً درخواست نمی‌فرستد
        return Math.min(base * Math.pow(2, Math.min(state.failures, 3)), 120000);   // خطای پیاپی ← عقب‌نشینی نمایی تا ۱۲۰ ثانیه
    }

    function pollOnce() {
        if (!state.conv || state.polling) { return Promise.resolve(); }
        state.polling = true;
        return api('GET', url(cfg.api.messages, state.conv.id) + '?after=' + state.lastSeq).then(function (data) {
            state.polling = false;
            if (!data.ok) {
                if (data.status === 404) { setConversation(null); return; }
                state.failures += 1;
                return;
            }
            state.failures = 0;
            var added = addMessages(data.messages);
            setConversation(data.conversation);
            if (state.ui.typingEl) {
                var wasHidden = state.ui.typingEl.hidden;
                state.ui.typingEl.hidden = !data.typing;
                if (wasHidden && data.typing) { scrollThread(); }
            }
            if (data.conversation.closed) { showForm(); return; }
            if (added) { markRead(); }
        }).catch(function () { state.polling = false; state.failures += 1; });
    }

    function schedulePoll(delay) {
        clearTimeout(state.pollTimer);
        if (!state.conv) { return; }
        var interval = delay !== undefined ? delay : pollInterval();
        if (!interval) { return; }
        var jitter = interval * (0.8 + Math.random() * 0.4);   // ±۲۰٪ تا همه‌ی کلاینت‌ها هم‌زمان نپرسند
        state.pollTimer = setTimeout(function () {
            if (document.hidden) { schedulePoll(); return; }
            pollOnce().then(function () { schedulePoll(); }, function () { schedulePoll(); });
        }, jitter);
    }

    function buildAiPane(pane) {
        var card = el('div', 'cw-card');
        var holder = el('div', 'cw-card-icon');
        holder.appendChild(icon('spark'));
        card.appendChild(holder);
        card.appendChild(el('div', 'cw-title', cfg.tabs.filter(function (t) { return t.key === 'ai'; })[0].label));
        card.appendChild(el('p', '', cfg.texts.ai_coming_soon));
        pane.appendChild(card);
    }

    var BUILDERS = {live: buildLivePane, offline: buildOfflinePane, ai: buildAiPane};
    var TAB_ICON = {live: 'chat', offline: 'mail', ai: 'spark'};

    function hasTab(key) {
        return cfg.tabs.some(function (t) { return t.key === key; });
    }

    function selectTab(key, focus) {
        state.tab = key;
        Array.prototype.forEach.call(panel.querySelectorAll('.cw-tab'), function (tab) {
            var on = tab.getAttribute('data-tab') === key;
            tab.setAttribute('aria-selected', on ? 'true' : 'false');
            tab.tabIndex = on ? 0 : -1;
            if (on && focus) { tab.focus(); }
        });
        Array.prototype.forEach.call(panel.querySelectorAll('.cw-pane'), function (pane) {
            pane.hidden = pane.getAttribute('data-pane') !== key;
        });
        updateBadge();
        if (key === 'offline' || key === 'live') { refreshTabView(); scrollThread(); markRead(); }
    }

    function buildPanel() {
        panel = el('div', 'cw-panel');
        panel.hidden = true;
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-label', cfg.texts.title);
        panel.id = 'cw-panel';

        var head = el('div', 'cw-head');
        var headAvatar = el('span', 'cw-head-avatar');
        head.appendChild(headAvatar);
        fillAvatar(headAvatar, cfg.avatar);
        var text = el('div', 'cw-head-text');
        text.appendChild(el('strong', '', cfg.texts.title));
        var sub = el('span', 'cw-sub');
        var dot = el('i', liveAvailable() ? 'is-online' : '');
        var subLabel = el('span', '', subtitleText());
        sub.appendChild(dot);
        sub.appendChild(subLabel);
        state.ui.subDot = dot;
        state.ui.subText = subLabel;
        text.appendChild(sub);
        head.appendChild(text);
        var close = el('button', 'cw-x');
        close.type = 'button';
        close.setAttribute('aria-label', 'بستن پنجره‌ی گفتگو');
        close.appendChild(icon('close'));
        close.addEventListener('click', closePanel);
        head.appendChild(close);
        panel.appendChild(head);

        var tabs = el('div', 'cw-tabs');
        tabs.setAttribute('role', 'tablist');
        var body = el('div', 'cw-body');
        var threadView = el('div', 'cw-thread');         // نمای گفتگو یک‌بار ساخته می‌شود و بین زبانه‌ها جابه‌جا می‌شود
        threadView.hidden = true;
        state.ui.threadView = threadView;
        buildThread(threadView);
        cfg.tabs.forEach(function (tab) {
            var btn = el('button', 'cw-tab');
            btn.type = 'button';
            btn.setAttribute('role', 'tab');
            btn.setAttribute('data-tab', tab.key);
            btn.setAttribute('aria-controls', 'cw-pane-' + tab.key);
            btn.appendChild(icon(TAB_ICON[tab.key]));
            btn.appendChild(el('span', '', tab.label));
            if (tab.coming_soon) { btn.appendChild(el('span', 'cw-soon', 'به‌زودی')); }
            btn.addEventListener('click', function () { selectTab(tab.key, false); });
            btn.addEventListener('keydown', function (event) {
                var keys = cfg.tabs.map(function (t) { return t.key; });
                var at = keys.indexOf(tab.key);
                // راست‌به‌چپ: پیکان چپ = زبانه‌ی بعدی
                var next = event.key === 'ArrowLeft' ? at + 1 : event.key === 'ArrowRight' ? at - 1 : null;
                if (next === null) { return; }
                event.preventDefault();
                selectTab(keys[(next + keys.length) % keys.length], true);
            });
            tabs.appendChild(btn);

            var pane = el('div', 'cw-pane');
            pane.id = 'cw-pane-' + tab.key;
            pane.setAttribute('role', 'tabpanel');
            pane.setAttribute('data-pane', tab.key);
            BUILDERS[tab.key](pane);
            body.appendChild(pane);
        });
        panel.appendChild(tabs);
        panel.appendChild(body);
        root.appendChild(panel);

        selectTab(preferredTab(), false);
    }

    // پیش‌فرض: زبانه‌ی کانال گفتگوی جاری؛ بدون گفتگو: زنده اگر در دسترس است، وگرنه فرم آفلاین، وگرنه اولین زبانه
    function preferredTab() {
        if (state.conv && state.conv.channel === 'live' && hasTab('live')) { return 'live'; }
        if (state.conv && hasTab('offline')) { return 'offline'; }
        if (liveAvailable() && hasTab('live')) { return 'live'; }
        return hasTab('offline') ? 'offline' : cfg.tabs[0].key;
    }

    /* ---------- باز/بسته ---------- */
    function viewportFit() {
        if (!state.open || !MOBILE.matches || !window.visualViewport) { return; }
        panel.style.height = window.visualViewport.height + 'px';
        panel.style.top = window.visualViewport.offsetTop + 'px';
        panel.style.bottom = 'auto';
    }

    function openPanel() {
        if (state.open) { return; }
        state.open = true;
        panel.hidden = false;
        root.classList.add('cw-open');
        launcherBtn.setAttribute('aria-expanded', 'true');
        stopBubble();
        write('sessionStorage', SS_BUBBLE, '1');
        if (MOBILE.matches) { document.documentElement.classList.add('cw-lock'); }
        viewportFit();
        var active = panel.querySelector('.cw-tab[aria-selected="true"]');
        if (active) { active.focus(); }
        markActivity();
        updateBadge();
        refreshState(false);
        if (state.conv) { pollOnce().then(markRead); schedulePoll(); }
    }

    function closePanel() {
        if (!state.open) { return; }
        state.open = false;
        panel.hidden = true;
        panel.style.height = panel.style.top = panel.style.bottom = '';
        root.classList.remove('cw-open');
        launcherBtn.setAttribute('aria-expanded', 'false');
        document.documentElement.classList.remove('cw-lock');
        launcherBtn.focus();
        schedulePoll();
    }

    function dismissLauncher() {
        var hours = cfg.dismiss_hours;
        if (hours > 0) { write('localStorage', LS_DISMISS, String(Date.now() + hours * 3600 * 1000)); }
        stopBubble();
        clearInterval(state.attnTimer);
        closePanel();
        root.hidden = true;
    }

    /* ---------- ساخت دکمه‌ی شناور ---------- */
    function buildLauncher() {
        var launcher = el('div', 'cw-launcher');
        launcherBtn = el('button', 'cw-btn');
        launcherBtn.type = 'button';
        launcherBtn.setAttribute('aria-label', cfg.texts.title);
        launcherBtn.setAttribute('aria-haspopup', 'dialog');
        launcherBtn.setAttribute('aria-expanded', 'false');
        launcherBtn.setAttribute('aria-controls', 'cw-panel');
        var ring = el('span', 'cw-ring');
        var avatar = el('span', 'cw-avatar');
        launcherBtn.appendChild(ring);
        launcherBtn.appendChild(avatar);
        state.ui.launcherDot = el('span', 'cw-dot' + (liveAvailable() ? ' is-online' : ''));
        launcherBtn.appendChild(state.ui.launcherDot);
        badgeEl = el('span', 'cw-badge');
        badgeEl.hidden = true;
        badgeEl.setAttribute('aria-live', 'polite');
        launcher.appendChild(badgeEl);
        launcherBtn.addEventListener('click', function () { if (state.open) { closePanel(); } else { openPanel(); } });
        launcher.appendChild(launcherBtn);

        var dismiss = el('button', 'cw-dismiss');
        dismiss.type = 'button';
        dismiss.setAttribute('aria-label', 'بستن دکمه‌ی گفتگو');
        dismiss.appendChild(icon('close'));
        dismiss.addEventListener('click', dismissLauncher);
        launcher.appendChild(dismiss);

        bubbleEl = el('div', 'cw-bubble');
        bubbleEl.setAttribute('aria-hidden', 'true');
        launcher.appendChild(bubbleEl);
        root.appendChild(launcher);
        fillAvatar(avatar, cfg.avatar);
    }

    /* ---------- راه‌اندازی ---------- */
    function init(config) {
        cfg = config;
        if (!cfg || !cfg.enabled || !cfg.tabs || !cfg.tabs.length) { return; }
        if (dismissedNow()) { return; }

        root.style.setProperty('--cw-color', cfg.color);
        root.style.setProperty('--cw-color-dark', darken(cfg.color, 0.16));
        root.style.setProperty('--cw-on', luminance(cfg.color) > 0.5 ? '#1F2937' : '#FFFFFF');
        root.classList.add(cfg.position.side === 'right' ? 'cw-right' : 'cw-left');
        root.classList.add('cw-kind-' + cfg.avatar.kind);
        ['float', 'pulse', 'wave', 'bubble'].forEach(function (name) {
            if (cfg.anim[name]) { root.classList.add('cw-anim-' + name); }
        });
        if (!motionAllowed()) { root.classList.add('cw-still'); }
        if ('ontouchstart' in window) { root.classList.add('cw-touch'); }

        buildLauncher();
        buildPanel();
        applyPosition();
        root.hidden = false;
        startBubble();
        startAttention();
        loadConversationState();

        window.addEventListener('resize', function () { applyPosition(); viewportFit(); });
        if (MOBILE.addEventListener) { MOBILE.addEventListener('change', applyPosition); }
        if (window.visualViewport) {
            window.visualViewport.addEventListener('resize', viewportFit);
            window.visualViewport.addEventListener('scroll', viewportFit);
        }
        var nav = document.getElementById('mobile-bottom-nav');
        if (nav && window.ResizeObserver) { new ResizeObserver(applyPosition).observe(nav); }
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && state.open) { closePanel(); }
        });
    }

    function loadConversationState() {
        // گفتگوی باز همین بازدیدکننده/کاربر (کوکی HttpOnly خودکار می‌رود)؛ بدون گفتگو درخواست دیگری ساخته نمی‌شود
        if (!cfg.api) { return; }
        state.lastStateAt = Date.now();
        api('GET', cfg.api.state).then(function (data) {
            if (!data.ok || !data.enabled) { return; }
            applyAvailability(data.availability);
            applyBlocked(data.blocked);
            if (!data.conversation) { return; }
            setConversation(data.conversation);
            state.lastSeq = 0;
            if (state.ui.messagesEl) { state.ui.messagesEl.textContent = ''; }
            if (!state.open) { selectTab(preferredTab(), false); } else { refreshTabView(); }
            pollOnce();
        }).catch(function () { /* ساکت */ });
    }

    document.addEventListener('visibilitychange', function () {
        if (!document.hidden && state.conv) { pollOnce().then(markRead); schedulePoll(); }
    });

    function load() {
        fetch(root.getAttribute('data-config-url'), {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
            .then(function (response) { return response.ok ? response.json() : null; })
            .then(init)
            .catch(function () { /* پیکربندی در دسترس نیست؛ ویجت نمایش داده نمی‌شود، سایت کار می‌کند */ });
    }

    // بعد از بار شدن صفحه و در زمان بیکاری مرورگر؛ تأثیری روی سرعت اولیه‌ی صفحه ندارد
    function schedule() {
        if ('requestIdleCallback' in window) { window.requestIdleCallback(load, {timeout: 2500}); } else { setTimeout(load, 800); }
    }

    if (document.readyState === 'complete') { schedule(); } else { window.addEventListener('load', schedule); }
})();
