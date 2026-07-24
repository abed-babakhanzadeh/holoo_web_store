/* چک‌لیست زنده‌ی قوانین رمز عبور — روی هر [data-password-rules] فعال می‌شود و با تایپ در فیلد
   رمز (data-target)، هر قانون را بین ضربدر قرمز و تیک سبز toggle می‌کند.
   قوانین اینجا باید دقیقاً هم‌معنی با accounts/password_validators.py باشند. */
(function () {
    'use strict';

    var RULES = {
        length: function (v) { return v.length >= 8; },
        letter: function (v) { return /\p{L}/u.test(v); },
        digit: function (v) { return /\d/.test(v); },
        symbol: function (v) { return /[^\p{L}\p{N}\s]/u.test(v); }
    };

    function applyState(li, ok) {
        var icon = li.querySelector('.rule-icon');
        li.classList.remove('text-gray-400', 'dark:text-gray-500', 'text-red-500', 'text-green-600');
        li.classList.add(ok ? 'text-green-600' : 'text-red-500');
        if (icon) icon.textContent = ok ? '✓' : '✕';
    }

    function bind(list) {
        list.dataset.rulesBound = '1';
        var input = document.querySelector(list.dataset.target || '');
        if (!input) return;

        var items = {};
        list.querySelectorAll('[data-rule]').forEach(function (li) {
            items[li.dataset.rule] = li;
        });

        function evaluate() {
            var value = input.value || '';
            Object.keys(RULES).forEach(function (key) {
                if (items[key]) applyState(items[key], RULES[key](value));
            });
        }

        input.addEventListener('input', evaluate);
        evaluate();
    }

    function init(root) {
        (root || document).querySelectorAll('[data-password-rules]').forEach(function (list) {
            if (!list.dataset.rulesBound) bind(list);
        });
    }

    document.addEventListener('DOMContentLoaded', function () { init(document); });
    document.addEventListener('htmx:afterSettle', function (e) { init(e.target); });
})();
