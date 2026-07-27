/* Chore chart management (MASTER_PLAN P5-2 #12). */
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

    function card(el) { return el.closest('[data-person]'); }

    document.addEventListener('submit', function (e) {
        var form = e.target.closest('#new-person');
        if (!form) return;
        e.preventDefault();
        var name = document.getElementById('new-person-name').value.trim();
        if (!name) return;
        api('/chores', {method: 'POST', body: JSON.stringify({name: name})})
            .then(function () { location.reload(); })
            .catch(function (err) { toast(err.message, 'error'); });
    });

    document.addEventListener('click', function (e) {
        var host = e.target.closest('[data-person]');
        if (!host) return;
        var id = host.dataset.person;

        if (e.target.closest('[data-save-person]')) {
            var chores = host.querySelector('[data-chores]').value.split('\n');
            var days = Array.prototype.slice.call(
                host.querySelectorAll('[data-day]:checked')).map(function (b) {
                    return parseInt(b.value, 10);
                });
            api('/chores/' + id, {
                method: 'PATCH',
                body: JSON.stringify({chores: chores, days: days})
            }).then(function () { toast('Saved.'); })
              .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        if (e.target.closest('[data-print-person]')) {
            api('/chores/' + id + '/print', {method: 'POST'})
                .then(function () { toast('Printed.'); })
                .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        var toggle = e.target.closest('[data-toggle-done]');
        if (toggle) {
            var undo = toggle.textContent.indexOf('Undo') !== -1;
            api('/chores/' + id + '/done', {method: undo ? 'DELETE' : 'POST'})
                .then(function () { location.reload(); })
                .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        if (e.target.closest('[data-rotate-token]')) {
            // Worth a confirm: any chart already on the fridge stops working.
            if (!confirm('Rotate the code? Any chart already printed will stop working.')) return;
            api('/chores/' + id, {method: 'PATCH', body: JSON.stringify({rotate: true})})
                .then(function () { toast('Rotated. Reprint the chart.'); })
                .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        if (e.target.closest('[data-delete-person]')) {
            var name = host.querySelector('.mica-card-title').textContent.trim();
            if (!confirm('Remove ' + name + ' and their streak history?')) return;
            api('/chores/' + id, {method: 'DELETE'})
                .then(function () { location.reload(); })
                .catch(function (err) { toast(err.message, 'error'); });
        }
    });
})();
