/*
 * Behaviour for the schema-driven settings renderer (MASTER_PLAN P2-11).
 *
 * Deliberately generic: it binds to the data- attributes the macro emits, so a
 * new listener gets all of this without a line of JavaScript. Anything here
 * that had to know a listener's name would defeat the point of the schema.
 *
 * Everything degrades: with JS off the token field still submits its hidden
 * comma value, the matrix still posts its checkboxes, and depends_on fields are
 * simply all visible.
 */
(function () {
    'use strict';

    /* --- token chips (multiselect) --------------------------------------- */

    function tokens(key) {
        var hidden = document.getElementById('v-' + key);
        return hidden.value.split(',').map(function (s) { return s.trim(); })
            .filter(Boolean);
    }

    function writeTokens(key, values) {
        // Deduplicate on write rather than on add: paste and manual entry are
        // two paths to the same mistake, and one guard covers both.
        var unique = values.filter(function (v, i) { return values.indexOf(v) === i; });
        document.getElementById('v-' + key).value = unique.join(',');
        var host = document.querySelector('.token-chips[data-for="' + key + '"]');
        if (!host) return;
        host.innerHTML = '';
        unique.forEach(function (value) {
            var chip = document.createElement('span');
            chip.className = 'mica-chip mica-chip-accent';
            chip.dataset.value = value;
            chip.textContent = value;
            var remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'chip-remove';
            remove.dataset.remove = value;
            remove.setAttribute('aria-label', 'Remove ' + value);
            remove.innerHTML = '&times;';
            chip.appendChild(remove);
            host.appendChild(chip);
        });
    }

    function addToken(key) {
        var input = document.querySelector('[data-token-input="' + key + '"]');
        if (!input) return;
        // Accept a pasted "03101, 03102" in one go.
        var added = input.value.split(/[,\s]+/).map(function (s) { return s.trim(); })
            .filter(Boolean);
        if (!added.length) return;
        writeTokens(key, tokens(key).concat(added));
        input.value = '';
        input.focus();
    }

    document.addEventListener('click', function (e) {
        var add = e.target.closest('[data-token-add]');
        if (add) { addToken(add.dataset.tokenAdd); return; }

        var remove = e.target.closest('.token-chips .chip-remove');
        if (remove) {
            var key = remove.closest('.token-chips').dataset.for;
            writeTokens(key, tokens(key).filter(function (v) {
                return v !== remove.dataset.remove;
            }));
        }
    });

    document.addEventListener('keydown', function (e) {
        var input = e.target.closest('[data-token-input]');
        if (!input || e.key !== 'Enter') return;
        // Enter in a token field means "add this", not "submit the form" --
        // submitting on the way to adding a second ZIP would be maddening.
        e.preventDefault();
        addToken(input.dataset.tokenInput);
    });

    /* --- matrix ---------------------------------------------------------- */

    document.addEventListener('input', function (e) {
        var filter = e.target.closest('[data-matrix-filter]');
        if (!filter) return;
        var table = document.querySelector('[data-matrix="' + filter.dataset.matrixFilter + '"]');
        if (!table) return;
        var needle = filter.value.trim().toLowerCase();
        table.querySelectorAll('tbody tr').forEach(function (row) {
            row.hidden = needle && row.dataset.row.toLowerCase().indexOf(needle) === -1;
        });
    });

    document.addEventListener('click', function (e) {
        var all = e.target.closest('[data-matrix-all]');
        var none = e.target.closest('[data-matrix-none]');
        if (!all && !none) return;
        var key = (all || none).dataset.matrixAll || (all || none).dataset.matrixNone;
        var table = document.querySelector('[data-matrix="' + key + '"]');
        if (!table) return;
        // Only visible rows, so "select none" after filtering to "Advisory"
        // does what it looks like it does rather than clearing the lot.
        table.querySelectorAll('tbody tr:not([hidden]) input[type="checkbox"]')
            .forEach(function (box) { box.checked = !!all; });
    });

    /* --- progressive disclosure ------------------------------------------ */

    function applyDependencies() {
        document.querySelectorAll('[data-depends-on]').forEach(function (host) {
            var source = document.getElementById('f-' + host.dataset.dependsOn);
            if (!source) return;
            var on = source.type === 'checkbox' ? source.checked : !!source.value;
            host.hidden = !on;
        });
    }

    document.addEventListener('change', applyDependencies);
    applyDependencies();
})();

/*
 * A multiselect with fixed options renders as checkboxes over a hidden comma
 * field (P4-5). Keeping the hidden field in step is what lets the server tell
 * "none selected" from "field not submitted" -- absent means unchanged, empty
 * means cleared, and they must not look the same.
 */
(function () {
    'use strict';
    document.addEventListener('change', function (e) {
        var box = e.target.closest('[data-option]');
        if (!box) return;
        var key = box.dataset.option;
        var checked = Array.prototype.slice.call(
            document.querySelectorAll('[data-option="' + key + '"]:checked')
        ).map(function (b) { return b.value; });
        var hidden = document.getElementById('v-' + key);
        if (hidden) hidden.value = checked.join(',');
    });
})();

/*
 * Notice panels (P5-2). A listener can surface something to copy or an action
 * to run -- the webhook's URL and token rotation. Generic on purpose: the next
 * listener that needs a notice gets this for free.
 */
(function () {
    'use strict';

    document.addEventListener('click', function (e) {
        var copy = e.target.closest('[data-copy]');
        if (copy) {
            var text = copy.dataset.copy;
            // clipboard API needs a secure context; over plain HTTP on a LAN it
            // is undefined, so fall back rather than failing silently.
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(text)
                    .then(function () { window.toast && toast('Copied.'); })
                    .catch(function () { prompt('Copy this URL:', text); });
            } else {
                prompt('Copy this URL:', text);
            }
            return;
        }

        var action = e.target.closest('[data-notice-action]');
        if (action) {
            if (action.dataset.confirm && !confirm(action.dataset.confirm)) return;
            action.disabled = true;
            fetch(action.dataset.noticeAction, {method: 'POST'})
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    if (!d.ok) throw new Error(d.error || 'Failed.');
                    location.reload();
                })
                .catch(function (err) {
                    action.disabled = false;
                    window.toast ? toast(err.message, 'error') : alert(err.message);
                });
        }
    });
})();
