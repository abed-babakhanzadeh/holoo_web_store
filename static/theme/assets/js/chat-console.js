/*
 * پیشخوان کارشناس گفتگو: فهرست گفتگوها، گفتگوی انتخاب‌شده، پاسخ و یادداشت داخلی، پاسخ‌های آماده (با تایپ / در ابتدای کادر)،
 * کارت اطلاعات مشتری و اقدام‌ها (برداشتن، منتظر مشتری، بازگرداندن به صف، ارجاع، بستن، بازگشایی).
 * به‌روزرسانی با پولینگ سبک: صندوق هر ۴ ثانیه و گفتگوی باز با after=<seq> هر ۲٫۵ ثانیه؛ وقتی تب مخفی است متوقف می‌شود.
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
    var state = {filter: 'all', q: '', selected: null, lastSeq: 0, quick: [], counts: {}, prevUnread: null, sending: false, operators: null};
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
            options.headers['Content-Type'] = 'application/json';
            options.headers['X-CSRFToken'] = csrf();
            options.body = JSON.stringify(body || {});
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
        els.thread.appendChild(head);
        var msgs = el('div', 'cc-msgs');
        msgs.id = 'cc-msgs';
        els.thread.appendChild(msgs);
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
            node.appendChild(document.createTextNode(m.body));
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
        var send = el('button', 'cc-btn primary', 'ارسال');
        send.type = 'button';
        row.appendChild(send);
        box.appendChild(row);
        var menu = null;
        var quickId = null;

        function closeMenu() { if (menu) { menu.remove(); menu = null; } }
        function pick(item, customerName) {
            area.value = item.body.replace(/\{customer_name\}/g, customerName || 'مشتری');
            quickId = item.id;
            closeMenu();
            area.focus();
        }
        function openMenu() {
            var text = area.value.slice(1).trim().toLowerCase();
            var items = state.quick.filter(function (q) {
                return !text || q.title.toLowerCase().indexOf(text) !== -1 || (q.shortcut || '').toLowerCase().indexOf(text) !== -1;
            }).slice(0, 8);
            closeMenu();
            if (!items.length) { return; }
            menu = el('div', 'cc-qr');
            var name = (els.thread.querySelector('.cc-head strong') || {}).textContent || '';
            items.forEach(function (q) {
                var b = el('button', '', q.title);
                b.type = 'button';
                b.appendChild(el('small', '', q.body.slice(0, 80)));
                b.addEventListener('click', function () { pick(q, name.replace(/ \(مهمان\)$/, '')); });
                menu.appendChild(b);
            });
            box.appendChild(menu);
        }
        area.addEventListener('input', function () {
            quickId = null;
            if (area.value.charAt(0) === '/') { openMenu(); } else { closeMenu(); }
        });
        area.addEventListener('keydown', function (event) {
            if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); submit(); }
            if (event.key === 'Escape') { closeMenu(); }
        });
        function submit() {
            var text = area.value.trim();
            if (!text || state.sending) { return; }
            state.sending = true;
            send.disabled = true;
            call('POST', url(API.reply, state.selected), {body: text, note: note.checked, client_msg_id: uuid(), quick_reply_id: quickId}).then(function (data) {
                state.sending = false;
                send.disabled = false;
                if (!data.ok) { window.alert(data.message || 'ارسال نشد.'); return; }
                area.value = '';
                note.checked = false;
                quickId = null;
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
