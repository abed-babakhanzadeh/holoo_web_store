/*
 * ویجت گفتگوی آنلاین، فاز ۱ (اسکلت و ظاهر): دکمه‌ی شناور با انیمیشن‌ها، حباب، قابل بستن، و پنجره‌ی سه‌زبانه
 * (گفتگوی آنلاین / پیام آفلاین / چت هوشمند). همه‌ی متن‌ها و تنظیمات از GET /chat/config/ می‌آید (پنل ادمین).
 * ثبت و ارسال پیام و پولینگ در فازهای بعد اضافه می‌شود؛ تا آن موقع فرم آفلاین فقط اعتبارسنجی می‌کند.
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
    var state = {open: false, tab: null, bubbleTimer: null, attnTimer: null};

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

        // فاز ۱: گفتگوی زنده هنوز وجود ندارد (حضور کارشناس در فاز ۳)؛ طبق وضعیت ۲/۳ به فرم آفلاین هدایت می‌شود
        var card = el('div', 'cw-card');
        var holder = el('div', 'cw-card-icon');
        holder.appendChild(icon('clock'));
        card.appendChild(holder);
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
        pane.appendChild(card);
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

    function buildOfflinePane(pane) {
        pane.appendChild(el('p', 'cw-hours', cfg.texts.offline_intro));
        var form = el('form', 'cw-form');
        form.noValidate = true;
        var controls = {};

        function addInput(name, mode, placeholder, attrs) {
            if (mode === 'hidden' || cfg.viewer.authenticated) { return; }     // کاربر واردشده از حساب خوانده می‌شود (فاز ۲)
            var input = el('input');
            input.id = 'cw-' + name;
            input.name = name;
            input.type = 'text';
            input.placeholder = placeholder;
            input.autocomplete = attrs.autocomplete;
            if (attrs.inputmode) { input.setAttribute('inputmode', attrs.inputmode); }
            if (attrs.dir) { input.dir = attrs.dir; }
            input.required = mode === 'required';
            var f = field(placeholder + (mode === 'required' ? ' *' : ''), input, 'cw-err-' + name);
            controls[name] = {input: input, error: f.error, mode: mode};
            form.appendChild(f.wrap);
        }

        addInput('name', cfg.guest_form.name, cfg.texts.name_placeholder, {autocomplete: 'name'});
        addInput('phone', cfg.guest_form.phone, cfg.texts.phone_placeholder, {autocomplete: 'tel', inputmode: 'tel', dir: 'ltr'});

        var area = el('textarea');
        area.id = 'cw-message';
        area.name = 'message';
        area.rows = 4;
        area.maxLength = cfg.limits.message_max_length;
        area.placeholder = cfg.texts.message_placeholder;
        var fm = field(cfg.texts.message_placeholder.replace(/[…\.]+$/, ''), area, 'cw-err-message');
        form.appendChild(fm.wrap);
        var count = el('div', 'cw-count', '0 / ' + cfg.limits.message_max_length);
        fm.wrap.insertBefore(count, fm.error);
        area.addEventListener('input', function () { count.textContent = area.value.length + ' / ' + cfg.limits.message_max_length; });

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
            var text = area.value.trim();
            var messageCtrl = {input: area, error: fm.error};
            setError(messageCtrl, text ? '' : 'متن پیام را بنویسید.');
            if (!text) { ok = false; }
            if (!ok) { return; }
            // فاز ۱: API ثبت پیام هنوز ساخته نشده؛ فقط وقتی backend_ready باشد (فاز ۲) ارسال می‌شود
            note.hidden = false;
            note.textContent = cfg.backend_ready ? cfg.texts.offline_success : cfg.texts.backend_not_ready;
        });
        pane.appendChild(form);
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
        sub.appendChild(dot);
        sub.appendChild(el('span', '', subtitleText()));
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

        // پیش‌فرض: گفتگوی زنده اگر در دسترس است، وگرنه فرم آفلاین، وگرنه اولین زبانه
        selectTab(liveAvailable() && hasTab('live') ? 'live' : hasTab('offline') ? 'offline' : cfg.tabs[0].key, false);
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
        launcherBtn.appendChild(el('span', 'cw-dot' + (liveAvailable() ? ' is-online' : '')));
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
