/*
 * پیشخوان کارشناس گفتگو: فهرست گفتگوها، گفتگوی انتخاب‌شده، پاسخ و یادداشت داخلی، پاسخ‌های آماده (با تایپ / در ابتدای کادر)،
 * کارت اطلاعات مشتری و اقدام‌ها (برداشتن، منتظر مشتری، بازگرداندن به صف، ارجاع، بستن، بازگشایی).
 * به‌روزرسانی با پولینگ سبک: صندوق هر ۴ ثانیه و گفتگوی باز با after=<seq> هر ۲٫۵ ثانیه؛ وقتی تب مخفی است متوقف می‌شود.
 * حضور: کلید «آنلاین/آفلاین» دستی کارشناس + نبض خودکار (فقط وقتی آنلاین است) با Web Worker تا تب مخفی/پس‌زمینه کند نشود؛
 * نشانگر «در حال نوشتن» دوطرفه (فقط Redis، هر ۳ ثانیه یک‌بار).
 * همه‌ی متن‌ها با textContent درج می‌شوند (ضد XSS).
 */
(function () {
    'use strict';

    var root = document.getElementById('chat-console');
    if (!root) { return; }
    var API = JSON.parse(document.getElementById('cc-api').textContent);
    var INBOX_MS = parseInt(root.getAttribute('data-poll-inbox'), 10) || 4000;
    var DETAIL_MS = parseInt(root.getAttribute('data-poll-detail'), 10) || 2500;

    var STATUS = {waiting_operator: 'در صف', active: 'در جریان', waiting_customer: 'منتظر مشتری', offline: 'آفلاین', closed: 'بسته'};
    var FILTERS = [['all', 'همه'], ['waiting', 'منتظر پاسخ'], ['unread', 'خوانده‌نشده'], ['mine', 'مال من'], ['offline', 'آفلاین'], ['closed', 'بسته']];
    var ACTION_LABEL = {claim: 'برداشتن', waiting: 'منتظر مشتری', release: 'بازگرداندن به صف', reassign: 'ارجاع', close: 'بستن', reopen: 'بازگشایی'};

    var els = {
        filters: root.querySelector('.cc-filters'), search: root.querySelector('.cc-search'), list: root.querySelector('.cc-list'),
        thread: root.querySelector('.cc-thread'), card: root.querySelector('.cc-card'), sound: document.getElementById('cc-sound')
    };
    var state = {filter: 'all', q: '', selected: null, lastSeq: 0, quick: [], counts: {}, prevUnread: null, sending: false, operators: null,
                 online: false, stopTicker: null, lastTypingAt: 0, lastPingAt: 0, blocked: false};
    var timers = {inbox: null, detail: null};
    var baseTitle = document.title;

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) { node.className = cls; }
        if (text !== undefined) { node.textContent = text; }
        return node;
    }

    function url(template, id) { return template.replace(/\/0\//, '/' + id + '/'); }

    function csrf() {
        var m = document.cookie.match(/csrftoken=([^;]+)/);
        if (m) { return m[1]; }
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : '';
    }

    function call(method, path, body) {
        var options = {method: method, credentials: 'same-origin', headers: {'Accept': 'application/json'}};
        if (method !== 'GET') {
            options.headers['X-CSRFToken'] = csrf();
            if (window.FormData && body instanceof FormData) {
                options.body = body;
            } else {
                options.headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(body || {});
            }
        }
        return fetch(path, options).then(function (r) { return r.json().catch(function () { return {ok: false, message: 'پاسخ نامعتبر'}; }); });
    }

    function uuid() {
        if (window.crypto && window.crypto.randomUUID) { return window.crypto.randomUUID(); }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            var r = Math.random() * 16 | 0;
            return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }

    function fmtTime(iso) {
        if (!iso) { return ''; }
        var d = new Date(iso);
        var now = new Date();
        var opts = d.toDateString() === now.toDateString() ? {hour: '2-digit', minute: '2-digit'} : {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'};
        try { return new Intl.DateTimeFormat('fa-IR', opts).format(d); } catch (error) { return d.toLocaleString(); }
    }

    /* ---------- صندوق ---------- */
    function renderFilters() {
        els.filters.textContent = '';
        FILTERS.forEach(function (f) {
            var b = el('button', 'cc-filter');
            b.type = 'button';
            b.setAttribute('role', 'tab');
            b.setAttribute('aria-selected', state.filter === f[0] ? 'true' : 'false');
            b.appendChild(document.createTextNode(f[1]));
            var n = f[0] === 'unread' ? state.counts.unread : f[0] === 'waiting' ? state.counts.waiting : f[0] === 'offline' ? state.counts.offline : f[0] === 'mine' ? state.counts.mine : null;
            if (n) { b.appendChild(el('b', '', String(n))); }
            b.addEventListener('click', function () { state.filter = f[0]; loadInbox(); });
            els.filters.appendChild(b);
        });
    }

    function renderList(rows) {
        els.list.textContent = '';
        if (!rows.length) { els.list.appendChild(el('li', 'cc-empty', 'گفتگویی نیست.')); return; }
        rows.forEach(function (c) {
            var li = el('li', 'cc-item' + (state.selected === c.id ? ' is-selected' : ''));
            var top = el('div', 'cc-item-top');
            top.appendChild(el('span', '', c.name + (c.registered ? '' : ' (مهمان)')));
            top.appendChild(el('time', '', fmtTime(c.last_message_at)));
            li.appendChild(top);
            li.appendChild(el('div', 'cc-item-preview', (c.last_sender === 'operator' ? 'شما: ' : '') + (c.preview || '')));
            var meta = el('div', 'cc-item-meta');
            meta.appendChild(el('span', 'cc-badge st-' + c.status, STATUS[c.status] || c.status));
            if (c.assigned) { meta.appendChild(el('span', 'cc-badge', c.assigned)); }
            if (c.unread) { meta.appendChild(el('span', 'cc-unread', String(c.unread))); }
            li.appendChild(meta);
            li.addEventListener('click', function () { selectConversation(c.id); });
            els.list.appendChild(li);
        });
    }

    function beep() {
        try {
            var Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) { return; }
            var ctx = new Ctx(), osc = ctx.createOscillator(), gain = ctx.createGain();
            osc.frequency.value = 880; gain.gain.value = 0.05;
            osc.connect(gain); gain.connect(ctx.destination);
            osc.start(); osc.stop(ctx.currentTime + 0.18);
        } catch (error) { /* بی‌صدا */ }
    }

    function loadInbox() {
        var q = '?filter=' + encodeURIComponent(state.filter) + (state.q ? '&q=' + encodeURIComponent(state.q) : '');
        return call('GET', API.inbox + q).then(function (data) {
            if (!data.ok) { return; }
            state.counts = data.counts;
            renderFilters();
            renderList(data.conversations);
            var unread = data.counts.unread || 0;
            document.title = (unread ? '(' + unread + ') ' : '') + baseTitle;
            if (state.prevUnread !== null && unread > state.prevUnread && els.sound.checked) { beep(); }
            state.prevUnread = unread;
        });
    }

    function schedule() {
        clearTimeout(timers.inbox);
        timers.inbox = setTimeout(function () {
            if (document.hidden) { schedule(); return; }
            loadInbox().then(schedule, schedule);
        }, INBOX_MS);
    }

    /* ---------- گفتگو ---------- */
    function selectConversation(id) {
        state.selected = id;
        state.lastSeq = 0;
        els.thread.textContent = '';
        els.thread.appendChild(el('div', 'cc-empty', 'در حال بارگذاری…'));
        pollDetail(true);
        loadInbox();
    }

    function buildThread(data) {
        var c = data.conversation;
        els.thread.textContent = '';
        var head = el('div', 'cc-head');
        head.appendChild(el('strong', '', c.name + (c.registered ? '' : ' (مهمان)')));
        if (c.phone) { head.appendChild(el('span', 'cc-badge', c.phone)); }
        var badge = el('span', 'cc-badge st-' + c.status, STATUS[c.status] || c.status);
        badge.id = 'cc-status';
        head.appendChild(badge);
        var actions = el('div', 'cc-actions');
        actions.id = 'cc-actions';
        head.appendChild(actions);
        var blockBtn = el('button', 'cc-btn danger', 'مسدودسازی');
        blockBtn.type = 'button';
        blockBtn.id = 'cc-block';
        blockBtn.addEventListener('click', toggleBlock);
        head.appendChild(blockBtn);
        setBlocked(!!data.blocked);
        els.thread.appendChild(head);
        var msgs = el('div', 'cc-msgs');
        msgs.id = 'cc-msgs';
        els.thread.appendChild(msgs);
        var typing = el('div', 'cc-typing');
        typing.id = 'cc-typing';
        typing.hidden = true;
        typing.appendChild(el('i'));
        typing.appendChild(el('i'));
        typing.appendChild(el('i'));
        typing.appendChild(document.createTextNode('مشتری در حال نوشتن…'));
        els.thread.appendChild(typing);
        els.thread.appendChild(buildComposer());
        renderActions(data.actions, c);
    }

    function renderActions(actions, c) {
        var box = document.getElementById('cc-actions');
        if (!box) { return; }
        box.textContent = '';
        actions.forEach(function (name) {
            var b = el('button', 'cc-btn' + (name === 'claim' ? ' primary' : name === 'close' ? ' danger' : ''), ACTION_LABEL[name]);
            b.type = 'button';
            b.addEventListener('click', function () { doAction(name); });
            box.appendChild(b);
        });
        var badge = document.getElementById('cc-status');
        if (badge) { badge.className = 'cc-badge st-' + c.status; badge.textContent = STATUS[c.status] || c.status; }
        var compose = els.thread.querySelector('.cc-compose');
        if (compose) { compose.hidden = c.status === 'closed'; }
    }

    function addMessages(items) {
        var box = document.getElementById('cc-msgs');
        if (!box || !items.length) { return; }
        var stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        items.forEach(function (m) {
            if (m.seq <= state.lastSeq) { return; }
            state.lastSeq = m.seq;
            var cls = m.note ? 'note' : m.sender;
            var node = el('div', 'cc-msg ' + cls);
            if (m.body) { node.appendChild(document.createTextNode(m.body)); }
            (m.attachments || []).forEach(function (a) {
                var link = el('a', a.kind === 'image' ? 'cc-img-link' : 'cc-file');
                link.href = a.url;
                link.target = '_blank';
                link.rel = 'noopener';
                if (a.kind === 'image') {
                    var img = el('img', 'cc-img');
                    img.loading = 'lazy';
                    img.alt = a.name || 'تصویر';
                    img.src = a.url;
                    link.appendChild(img);
                } else {
                    link.textContent = '📄 ' + (a.name || 'فایل');
                }
                node.appendChild(link);
            });
            node.appendChild(el('small', '', (m.note ? 'یادداشت داخلی · ' : m.sender === 'operator' ? (m.operator || 'کارشناس') + ' · ' : '') + fmtTime(m.at)));
            box.appendChild(node);
        });
        if (stick) { box.scrollTop = box.scrollHeight; }
    }

    function renderCard(card) {
        els.card.textContent = '';
        els.card.appendChild(el('h4', '', 'اطلاعات مشتری'));
        var dl = el('dl');
        function row(label, value, href) {
            if (value === undefined || value === null || value === '') { return; }
            dl.appendChild(el('dt', '', label));
            var dd = el('dd');
            if (href) { var a = el('a', '', value); a.href = href; dd.appendChild(a); } else { dd.textContent = value; }
            dl.appendChild(dd);
        }
        row('نام', card.name, card.user_url);
        row('موبایل', card.phone);
        row('موبایل تأییدشده', card.phone_verified ? 'بله' : 'خیر');
        row('وضعیت تأیید', card.approval);
        row('سطح قیمت', card.price_level);
        row('کد هلو', card.erp_code);
        row('صفحه‌ی شروع', card.source_path);
        row('شروع', fmtTime(card.created_at));
        els.card.appendChild(dl);
        if (card.orders && card.orders.length) {
            els.card.appendChild(el('h4', '', 'آخرین سفارش‌ها (' + card.orders_count + ' سفارش)'));
            var ul = el('ul', 'cc-orders');
            card.orders.forEach(function (o) {
                ul.appendChild(el('li', '', '#' + o.id + ' · ' + o.status + ' · ' + o.total.toLocaleString('fa-IR') + ' تومان'));
            });
            els.card.appendChild(ul);
        }
    }

    function pollDetail(first) {
        clearTimeout(timers.detail);
        var id = state.selected;
        if (!id) { return; }
        call('GET', url(API.detail, id) + '?after=' + state.lastSeq).then(function (data) {
            if (id !== state.selected) { return; }
            if (!data.ok) { els.thread.textContent = ''; els.thread.appendChild(el('div', 'cc-empty', data.message || 'گفتگو در دسترس نیست.')); return; }
            if (first || !document.getElementById('cc-msgs')) { buildThread(data); renderCard(data.card); }
            else { renderActions(data.actions, data.conversation); }
            var typingBox = document.getElementById('cc-typing');
            if (typingBox) { typingBox.hidden = !data.typing; }
            if (data.blocked !== undefined && data.blocked !== state.blocked) { setBlocked(!!data.blocked); }
            addMessages(data.messages);
            if (data.conversation.unread > 0 && !document.hidden) {
                call('POST', url(API.read, id), {upto_seq: state.lastSeq});
            }
        }).then(function () {
            scheduleDetail();
        });
    }

    function scheduleDetail() {
        clearTimeout(timers.detail);
        timers.detail = setTimeout(function () {
            if (document.hidden) { scheduleDetail(); } else { pollDetail(false); }   // تب مخفی: بدون درخواست
        }, DETAIL_MS);
    }

    function setBlocked(flag) {
        state.blocked = flag;
        var btn = document.getElementById('cc-block');
        if (btn) {
            btn.textContent = flag ? 'رفع مسدودیت' : 'مسدودسازی';
            btn.className = 'cc-btn' + (flag ? '' : ' danger');
        }
        var head = els.thread.querySelector('.cc-head');
        var tag = document.getElementById('cc-blocked-tag');
        if (flag && head && !tag) {
            tag = el('span', 'cc-badge st-closed', 'مسدود');
            tag.id = 'cc-blocked-tag';
            head.insertBefore(tag, head.children[2] || null);
        } else if (!flag && tag) { tag.remove(); }
    }

    function toggleBlock() {
        if (!state.selected) { return; }
        var body = {action: state.blocked ? 'unblock' : 'block'};
        if (!state.blocked) {
            var reason = window.prompt('دلیل مسدودسازی (فقط برای کارشناسان دیده می‌شود):', '');
            if (reason === null) { return; }
            var hours = window.prompt('مدت مسدودیت به ساعت (خالی = دائمی):', '');
            if (hours === null) { return; }
            body.reason = reason;
            body.hours = hours.trim();
            body.close = window.confirm('گفتگوی باز هم بسته شود؟');
        } else if (!window.confirm('مسدودیت این بازدیدکننده برداشته شود؟')) { return; }
        call('POST', url(API.block, state.selected), body).then(function (data) {
            if (!data.ok) { window.alert(data.message || 'انجام نشد.'); return; }
            setBlocked(!!data.blocked);
            if (data.conversation) { renderActions(data.actions, data.conversation); }
            loadInbox();
        });
    }

    function doAction(name) {
        var body = {action: name};
        if (name === 'reassign') {
            var target = window.prompt('شناسه‌ی عددی کارشناس مقصد را بنویسید (نام‌ها: ' + (state.operators || []).map(function (o) { return o.id + '=' + o.name; }).join('، ') + ')');
            if (!target) { return; }
            body.operator_id = parseInt(target, 10);
        }
        if (name === 'close' && !window.confirm('این گفتگو بسته شود؟')) { return; }
        call('POST', url(API.action, state.selected), body).then(function (data) {
            if (!data.ok) { window.alert(data.message || 'انجام نشد.'); return; }
            renderActions(data.actions, data.conversation);
            loadInbox();
        });
    }

    function signalTyping(text, isNote) {
        if (!state.selected || isNote || !text.trim() || text.charAt(0) === '/') { return; }
        var now = Date.now();
        if (now - state.lastTypingAt < 3000) { return; }
        state.lastTypingAt = now;
        call('POST', url(API.typing, state.selected));
    }

    /* ---------- حضور (آنلاین/آفلاین و نبض) ---------- */
    var presenceEls = {dot: document.getElementById('cc-presence-dot'), text: document.getElementById('cc-presence-text'),
                       toggle: document.getElementById('cc-presence-toggle'), count: document.getElementById('cc-presence-count')};

    function ticker(ms, fn) {
        // Web Worker از کندسازی تایمر تب مخفی مستثناست؛ اگر ساخته نشد، setInterval معمولی
        try {
            var blob = new Blob(['setInterval(function(){postMessage(1)},' + ms + ')'], {type: 'application/javascript'});
            var objectUrl = URL.createObjectURL(blob);
            var worker = new Worker(objectUrl);
            worker.onmessage = fn;
            return function () { worker.terminate(); URL.revokeObjectURL(objectUrl); };
        } catch (error) {
            var id = setInterval(fn, ms);
            return function () { clearInterval(id); };
        }
    }

    function ping() {
        // تیک‌های صف‌شده‌ی Worker وقتی تب دوباره فعال می‌شود یک‌جا می‌رسند؛ فاصله‌ی حداقلی آن‌ها را به یک درخواست تبدیل می‌کند
        if (Date.now() - state.lastPingAt < 5000) { return; }
        state.lastPingAt = Date.now();
        call('POST', API.presence, {}).then(function (d) { if (d.ok) { renderPresence(d); } });
    }

    function renderPresence(data) {
        state.online = !!data.online;
        presenceEls.dot.className = 'cc-dot' + (state.online ? ' is-online' : '');
        presenceEls.text.textContent = state.online
            ? 'شما آنلاین هستید؛ مشتری‌ها می‌توانند گفتگوی زنده شروع کنند.'
            : 'شما آفلاین هستید؛ گفتگوی زنده فقط وقتی یک کارشناس آنلاین باشد در دسترس است.';
        presenceEls.toggle.hidden = false;
        presenceEls.toggle.textContent = state.online ? 'آفلاین شو' : 'آنلاین شو';
        presenceEls.toggle.className = 'cc-btn' + (state.online ? '' : ' primary');
        presenceEls.count.textContent = data.online_count + ' کارشناس آنلاین';
        if (state.online && !state.stopTicker) {
            var interval = Math.max(10, Math.floor((data.timeout || 60) / 3)) * 1000;
            state.stopTicker = ticker(interval, ping);
        } else if (!state.online && state.stopTicker) {
            state.stopTicker();
            state.stopTicker = null;
        }
    }

    presenceEls.toggle.addEventListener('click', function () {
        presenceEls.toggle.disabled = true;
        call('POST', API.presence, {online: !state.online}).then(function (d) {
            presenceEls.toggle.disabled = false;
            if (d.ok) { renderPresence(d); } else { window.alert(d.message || 'انجام نشد.'); }
        });
    });
    document.addEventListener('visibilitychange', function () { if (!document.hidden && state.online) { ping(); } });
    call('GET', API.presence).then(function (d) { if (d.ok) { renderPresence(d); } });

    /* ---------- نوشتن پاسخ ---------- */
    function buildComposer() {
        var box = el('div', 'cc-compose');
        var area = el('textarea');
        area.placeholder = 'پاسخ را بنویسید… (با / پاسخ‌های آماده را باز کنید؛ Ctrl+Enter ارسال)';
        area.setAttribute('aria-label', 'متن پاسخ');
        box.appendChild(area);
        var row = el('div', 'cc-compose-row');
        var noteLabel = el('label');
        var note = el('input');
        note.type = 'checkbox';
        noteLabel.appendChild(note);
        noteLabel.appendChild(document.createTextNode(' یادداشت داخلی (مشتری نمی‌بیند)'));
        row.appendChild(noteLabel);
        var chosen = [];
        var attachCfg = API.attachments && API.attachments.enabled ? API.attachments : null;
        var chips = el('div', 'cc-chips');
        chips.hidden = true;
        var fileInput = null;
        function renderChips() {
            chips.textContent = '';
            chips.hidden = !chosen.length;
            chosen.forEach(function (file, index) {
                var chip = el('span', 'cc-chip', file.name);
                var remove = el('button', '', '×');
                remove.type = 'button';
                remove.addEventListener('click', function () { chosen.splice(index, 1); renderChips(); });
                chip.appendChild(remove);
                chips.appendChild(chip);
            });
        }
        if (attachCfg) {
            fileInput = el('input');
            fileInput.type = 'file';
            fileInput.multiple = true;
            fileInput.accept = attachCfg.accept;
            fileInput.hidden = true;
            fileInput.addEventListener('change', function () {
                Array.prototype.forEach.call(fileInput.files, function (file) {
                    if (chosen.length >= attachCfg.max_count) { window.alert('حداکثر ' + attachCfg.max_count + ' فایل در هر پیام مجاز است.'); return; }
                    if (file.size > attachCfg.max_mb * 1024 * 1024) { window.alert('حجم هر فایل حداکثر ' + attachCfg.max_mb + ' مگابایت است.'); return; }
                    chosen.push(file);
                });
                fileInput.value = '';
                renderChips();
            });
            var attachBtn = el('button', 'cc-btn', '📎 پیوست');
            attachBtn.type = 'button';
            attachBtn.addEventListener('click', function () { fileInput.click(); });
            row.appendChild(attachBtn);
            row.appendChild(fileInput);
        }
        box.appendChild(chips);
        var quickBtn = el('button', 'cc-btn', 'پاسخ آماده');
        quickBtn.type = 'button';
        quickBtn.setAttribute('aria-haspopup', 'listbox');
        row.appendChild(quickBtn);
        if (API.quick_admin) {
            var manage = el('a', 'cc-manage', 'مدیریت');
            manage.href = API.quick_admin;
            manage.target = '_blank';
            manage.rel = 'noopener';
            row.appendChild(manage);
        }
        var send = el('button', 'cc-btn primary', 'ارسال');
        send.type = 'button';
        row.appendChild(send);
        box.appendChild(row);
        var menu = null;
        var quickId = null;
        var active = -1;

        function closeMenu() { if (menu) { menu.remove(); menu = null; } active = -1; }
        function markActive(next) {
            if (!menu) { return; }
            var buttons = menu.querySelectorAll('button');
            if (!buttons.length) { return; }
            active = (next + buttons.length) % buttons.length;
            Array.prototype.forEach.call(buttons, function (b, i) { b.classList.toggle('is-active', i === active); });
            buttons[active].scrollIntoView({block: 'nearest'});
        }
        function pick(item, customerName) {
            area.value = item.body.replace(/\{customer_name\}/g, customerName || 'مشتری');
            quickId = item.id;
            closeMenu();
            area.focus();
        }
        function openMenu(showAll) {
            var text = showAll ? '' : area.value.slice(1).trim().toLowerCase();
            var items = state.quick.filter(function (q) {
                return !text || q.title.toLowerCase().indexOf(text) !== -1 || (q.shortcut || '').toLowerCase().indexOf(text) !== -1;
            }).slice(0, showAll ? 30 : 8);
            closeMenu();
            if (!items.length) {
                if (showAll) {
                    menu = el('div', 'cc-qr');
                    menu.appendChild(el('div', 'cc-empty', 'پاسخ آماده‌ای تعریف نشده است؛ از «مدیریت» اضافه کنید.'));
                    box.appendChild(menu);
                }
                return;
            }
            menu = el('div', 'cc-qr');
            menu.setAttribute('role', 'listbox');
            var name = (els.thread.querySelector('.cc-head strong') || {}).textContent || '';
            items.forEach(function (q) {
                var b = el('button', '', q.title);
                b.type = 'button';
                b.appendChild(el('small', '', q.body.slice(0, 80)));
                b.addEventListener('click', function () { pick(q, name.replace(/ \(مهمان\)$/, '')); });
                menu.appendChild(b);
            });
            box.appendChild(menu);
            markActive(0);
        }
        quickBtn.addEventListener('click', function () { if (menu) { closeMenu(); } else { openMenu(true); } });
        area.addEventListener('input', function () {
            quickId = null;
            if (area.value.charAt(0) === '/') { openMenu(); } else { closeMenu(); }
            signalTyping(area.value, note.checked);
        });
        area.addEventListener('keydown', function (event) {
            if (menu && menu.querySelector('button')) {
                if (event.key === 'ArrowDown') { event.preventDefault(); markActive(active + 1); return; }
                if (event.key === 'ArrowUp') { event.preventDefault(); markActive(active - 1); return; }
                if (event.key === 'Enter' && !event.ctrlKey && !event.metaKey) {
                    event.preventDefault();
                    var chosen = menu.querySelectorAll('button')[active];
                    if (chosen) { chosen.click(); }
                    return;
                }
            }
            if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); submit(); }
            if (event.key === 'Escape') { closeMenu(); }
        });
        function submit() {
            var text = area.value.trim();
            var withFiles = chosen.length && !note.checked;
            if ((!text && !withFiles) || state.sending) { return; }
            state.sending = true;
            send.disabled = true;
            var payload = {body: text, note: note.checked, client_msg_id: uuid(), quick_reply_id: quickId};
            if (withFiles) {
                payload = new FormData();
                payload.append('body', text);
                payload.append('client_msg_id', uuid());
                if (quickId) { payload.append('quick_reply_id', quickId); }
                chosen.forEach(function (file) { payload.append('files', file, file.name); });
            }
            call('POST', url(API.reply, state.selected), payload).then(function (data) {
                state.sending = false;
                send.disabled = false;
                if (!data.ok) { window.alert(data.message || 'ارسال نشد.'); return; }
                area.value = '';
                note.checked = false;
                quickId = null;
                chosen = [];
                renderChips();
                addMessages([data.message]);
                renderActions(data.actions, data.conversation);
                loadInbox();
            });
        }
        send.addEventListener('click', submit);
        return box;
    }

    /* ---------- راه‌اندازی ---------- */
    els.search.addEventListener('input', function () {
        state.q = els.search.value.trim();
        clearTimeout(els.search._t);
        els.search._t = setTimeout(loadInbox, 250);
    });
    try { els.sound.checked = window.localStorage.getItem('cc_sound') === '1'; } catch (error) { /* ok */ }
    els.sound.addEventListener('change', function () { try { window.localStorage.setItem('cc_sound', els.sound.checked ? '1' : '0'); } catch (error) { /* ok */ } });
    call('GET', API.quick_replies).then(function (d) { if (d.ok) { state.quick = d.items; } });
    call('GET', API.operators).then(function (d) { if (d.ok) { state.operators = d.items; } });
    renderFilters();
    loadInbox().then(schedule, schedule);
})();
