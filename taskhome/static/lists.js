/*
 * Checklists (MASTER_PLAN P5-2 #11).
 *
 * Every action goes through /api/lists so a tick does not reload the page --
 * ticking six things off while standing in a kitchen should not cost six full
 * page loads.
 */
(function () {
    'use strict';

    function api(path, options) {
        return fetch('/api' + path, Object.assign({
            headers: {'Content-Type': 'application/json'}
        }, options || {})).then(function (r) {
            return r.json().then(function (d) {
                if (!r.ok || !d.ok) throw new Error(d.error || 'Request failed.');
                return d.data;
            });
        });
    }

    function card(el) { return el.closest('[data-list]'); }
    function listId(el) { return card(el).dataset.list; }

    function refreshCount(cardEl) {
        var left = cardEl.querySelectorAll('.list-items li:not(.is-done)[data-item]').length;
        var chip = cardEl.querySelector('[data-count]');
        if (chip) chip.textContent = left + ' left';
    }

    document.addEventListener('submit', function (e) {
        var newList = e.target.closest('#new-list');
        if (newList) {
            e.preventDefault();
            var name = document.getElementById('new-list-name').value.trim();
            if (!name) return;
            api('/lists', {method: 'POST', body: JSON.stringify({name: name})})
                .then(function () { location.reload(); })
                .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        var addItem = e.target.closest('[data-add-item]');
        if (addItem) {
            e.preventDefault();
            var field = addItem.querySelector('textarea');
            var text = field.value.trim();
            if (!text) return;
            api('/lists/' + listId(addItem) + '/items',
                {method: 'POST', body: JSON.stringify({text: text})})
                .then(function () { location.reload(); })
                .catch(function (err) { toast(err.message, 'error'); });
        }
    });

    document.addEventListener('change', function (e) {
        var box = e.target.closest('[data-toggle-item]');
        if (!box) return;
        var li = box.closest('[data-item]');
        var wanted = box.checked;
        api('/lists/' + listId(box) + '/items/' + li.dataset.item,
            {method: 'PATCH', body: JSON.stringify({done: wanted})})
            .then(function () {
                li.classList.toggle('is-done', wanted);
                refreshCount(card(box));
            })
            .catch(function (err) {
                // Put it back rather than showing a state the server rejected.
                box.checked = !wanted;
                toast(err.message, 'error');
            });
    });

    document.addEventListener('click', function (e) {
        var remove = e.target.closest('[data-remove-item]');
        if (remove) {
            var li = remove.closest('[data-item]');
            api('/lists/' + listId(remove) + '/items/' + li.dataset.item,
                {method: 'DELETE'})
                .then(function () {
                    var cardEl = card(remove);
                    li.remove();
                    refreshCount(cardEl);
                })
                .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        var print = e.target.closest('[data-print-list]');
        if (print) {
            print.disabled = true;
            api('/lists/' + listId(print) + '/print', {method: 'POST'})
                .then(function () { toast('Printed.'); })
                .catch(function (err) { toast(err.message, 'error'); })
                .finally(function () { print.disabled = false; });
            return;
        }

        var clear = e.target.closest('[data-clear-done]');
        if (clear) {
            if (!confirm('Remove every ticked item from this list?')) return;
            api('/lists/' + listId(clear) + '/clear', {method: 'POST'})
                .then(function (d) {
                    toast(d.removed + ' removed.');
                    location.reload();
                })
                .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        var del = e.target.closest('[data-delete-list]');
        if (del) {
            var name = card(del).querySelector('.mica-card-title').textContent.trim();
            if (!confirm('Delete "' + name + '" and everything on it?')) return;
            api('/lists/' + listId(del), {method: 'DELETE'})
                .then(function () { location.reload(); })
                .catch(function (err) { toast(err.message, 'error'); });
        }
    });
})();
