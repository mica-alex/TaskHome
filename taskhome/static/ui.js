/*
 * Shared UI helpers, replacing what Materialize provided (P2A-4).
 *
 * Two things were actually used: toasts and modals. Modals are now native
 * <dialog>, which handles focus trapping, Escape and the backdrop itself --
 * so this file only wires the open/close buttons.
 */
(function (global) {
  'use strict';

  var host = null;

  function toastHost() {
    if (!host) host = document.getElementById('toasts');
    return host;
  }

  /**
   * Show a transient message.
   * @param {string} message
   * @param {string} [kind] 'ok' (default), 'error' or 'info'
   */
  function toast(message, kind) {
    var container = toastHost();
    if (!container) return;
    var node = document.createElement('div');
    node.className = 'toast toast-' + (kind || 'ok');
    node.textContent = message;
    container.appendChild(node);
    // Errors linger: they usually need reading, and often acting on.
    var life = kind === 'error' ? 6000 : 3000;
    setTimeout(function () {
      node.classList.add('toast-leaving');
      setTimeout(function () { node.remove(); }, 250);
    }, life);
  }

  /*
   * Dialogs. [data-open-dialog="id"] opens, [data-close-dialog] closes.
   * showModal() gives focus trapping, Escape-to-close and inert background
   * for free -- all of which Materialize implemented by hand, and less well.
   */
  function wireDialogs(scope) {
    (scope || document).querySelectorAll('[data-open-dialog]').forEach(function (trigger) {
      trigger.addEventListener('click', function (event) {
        event.preventDefault();
        var dialog = document.getElementById(trigger.dataset.openDialog);
        if (!dialog) return;
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else dialog.setAttribute('open', '');   // very old browsers
      });
    });

    (scope || document).querySelectorAll('[data-close-dialog]').forEach(function (button) {
      button.addEventListener('click', function (event) {
        event.preventDefault();
        var dialog = button.closest('dialog');
        if (!dialog) return;
        if (typeof dialog.close === 'function') dialog.close();
        else dialog.removeAttribute('open');
      });
    });

    // Clicking the backdrop closes. <dialog> reports the dialog itself as the
    // target when the click lands outside its content box.
    (scope || document).querySelectorAll('dialog').forEach(function (dialog) {
      dialog.addEventListener('click', function (event) {
        if (event.target === dialog) dialog.close();
      });
    });
  }

  /** Reveal the weekday picker only for a custom recurrence. */
  function wireRecurrence(scope) {
    (scope || document).querySelectorAll('select[name="recurring"]').forEach(function (select) {
      function sync() {
        var form = select.closest('form');
        if (!form) return;
        var days = form.querySelector('.custom-days');
        if (days) days.hidden = select.value !== 'custom';
      }
      select.addEventListener('change', sync);
      sync();
    });
  }

  /*
   * Timestamps.
   *
   * TaskHome stores two different kinds and they must be treated differently:
   *
   *   naive  ("2025-08-26T09:36:54.106869") -- task times and print times.
   *          No offset, so the browser cannot know which instant is meant.
   *          These are already local to the appliance, so they are formatted
   *          in place and NOT converted; converting would silently shift them
   *          by the difference between the two machines' zones.
   *
   *   aware  ("2025-08-26T13:36:42Z") -- SCF report times. A genuine instant,
   *          so it is converted to the viewer's timezone.
   *
   * The raw value stays in the title attribute either way, because a
   * prettified time is worse than a precise one when something looks wrong.
   */
  function parseStamp(value, aware) {
    if (!value) return null;
    // JS accepts at most milliseconds; Python writes microseconds.
    var text = String(value).replace(/(\.\d{3})\d+/, '$1');
    if (!aware) {
      // Strip any offset and parse as local, so no conversion happens.
      text = text.replace(/(Z|[+-]\d{2}:?\d{2})$/, '');
    }
    var date = new Date(text);
    return isNaN(date.getTime()) ? null : date;
  }

  function formatStamp(date) {
    var now = new Date();
    var diff = (now - date) / 1000;
    var absolute = date.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit'
    });
    // Relative for the recent past: "12 min ago" answers "did that just
    // happen?" far faster than a date does.
    if (diff >= 0 && diff < 60) return 'just now';
    if (diff >= 0 && diff < 3600) return Math.floor(diff / 60) + ' min ago';
    if (diff >= 0 && diff < 86400) {
      var hours = Math.floor(diff / 3600);
      return hours + (hours === 1 ? ' hour ago' : ' hours ago');
    }
    var startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var days = Math.round((startOfToday - new Date(
      date.getFullYear(), date.getMonth(), date.getDate())) / 86400000);
    var time = date.toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'});
    if (days === 0) return 'Today, ' + time;
    if (days === 1) return 'Yesterday, ' + time;
    if (days === -1) return 'Tomorrow, ' + time;
    return absolute;
  }

  function wireTimestamps(scope) {
    (scope || document).querySelectorAll('time.ts').forEach(function (node) {
      var raw = node.getAttribute('datetime');
      var date = parseStamp(raw, node.dataset.aware === 'true');
      if (!date) return;                     // leave the raw value visible
      node.textContent = formatStamp(date);
      node.title = raw + (node.dataset.aware === 'true'
        ? ' (converted to your timezone)'
        : ' (local to TaskHome)');
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    wireDialogs();
    wireRecurrence();
    wireTimestamps();
  });

  global.TaskHome = {toast: toast, wireDialogs: wireDialogs,
                   wireRecurrence: wireRecurrence, wireTimestamps: wireTimestamps,
                   formatStamp: formatStamp, parseStamp: parseStamp};
})(window);

/*
 * Live status (MASTER_PLAN P2-5).
 *
 * Plain fetch on an interval, not SSE or WebSockets. One or two LAN clients
 * and a 60-second scheduler granularity do not justify long-lived connections
 * and the proxy/timeout care they need.
 *
 * The appbar already renders printer state server-side on page load; this
 * keeps it true without a refresh, and adds the two things that are otherwise
 * invisible: queue depth, and whether the scheduler is still ticking.
 */
(function () {
    'use strict';

    var INTERVAL_MS = 10000;
    var timer = null;
    var failures = 0;

    function el(id) { return document.getElementById(id); }

    function setDot(status) {
        var dot = el('printer-status');
        if (!dot) return;
        var online = status.printer.connected;
        dot.classList.toggle('is-online', online);
        dot.classList.toggle('is-offline', !online);
        dot.textContent = online ? 'Ready' : 'No printer';
        dot.title = online ? 'Printer connected' : 'Printer not connected';
    }

    function setQueue(status) {
        var chip = el('queue-chip');
        if (!chip) return;
        var waiting = status.queue.waiting || 0;
        chip.hidden = waiting === 0;
        chip.textContent = waiting + ' queued';
        chip.title = status.queue.paper_mm
            ? 'About ' + status.queue.paper_mm + ' mm of paper waiting'
            : 'Receipts waiting to print';
    }

    function setProblems(status) {
        var banner = el('status-problems');
        if (!banner) return;
        // Only surface what someone has to act on. A disconnected printer is
        // normal for this appliance and is already shown by the dot.
        if (!status.problems || !status.problems.length) {
            banner.hidden = true;
            return;
        }
        banner.hidden = false;
        banner.innerHTML = '';
        status.problems.forEach(function (text) {
            var line = document.createElement('div');
            line.textContent = text;
            banner.appendChild(line);
        });
    }

    function apply(status) {
        failures = 0;
        document.body.classList.remove('is-disconnected');
        setDot(status);
        setQueue(status);
        setProblems(status);
    }

    function poll() {
        fetch('/api/status', {headers: {'Accept': 'application/json'}})
            .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
            .then(apply)
            .catch(function () {
                // Two consecutive failures before saying anything: one is a
                // restart or a dropped packet, and flashing "disconnected"
                // every time the app reloads would be noise.
                if (++failures >= 2) document.body.classList.add('is-disconnected');
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!el('printer-status') && !el('queue-chip')) return;
        poll();
        timer = setInterval(poll, INTERVAL_MS);
    });

    // Stop polling while the tab is hidden. On a phone left open on the
    // counter this is the difference between a background tab that costs
    // nothing and one that wakes the radio every ten seconds all day.
    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            clearInterval(timer);
            timer = null;
        } else if (!timer) {
            poll();
            timer = setInterval(poll, INTERVAL_MS);
        }
    });
})();

/*
 * Reprint and poll-now (MASTER_PLAN P4-6).
 *
 * Both emit real paper, so both confirm first. A misclick here costs a receipt
 * and, for poll-now on a listener with a backlog, potentially several.
 */
(function () {
    'use strict';

    function busy(button, label) {
        button.disabled = true;
        button.dataset.label = button.textContent;
        button.textContent = label;
    }

    function done(button) {
        button.disabled = false;
        if (button.dataset.label) button.textContent = button.dataset.label;
    }

    document.addEventListener('click', function (e) {
        var reprint = e.target.closest('[data-reprint]');
        if (reprint) {
            if (!confirm('Print this receipt again?')) return;
            busy(reprint, 'Printing...');
            fetch('/api/history/reprint/' + encodeURIComponent(reprint.dataset.reprint),
                  {method: 'POST'})
                .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
                .then(function (result) {
                    toast(result[0] ? 'Reprinted.' : (result[1].error || 'Reprint failed.'),
                          result[0] ? '' : 'error');
                })
                .catch(function () { toast('Reprint failed.', 'error'); })
                .finally(function () { done(reprint); });
            return;
        }

        var poll = e.target.closest('[data-poll-now]');
        if (poll) {
            if (!confirm('Check now? Anything new will print immediately.')) return;
            busy(poll, 'Checking...');
            fetch('/api/listeners/' + encodeURIComponent(poll.dataset.pollNow) + '/poll',
                  {method: 'POST'})
                .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
                .then(function (result) {
                    var ok = result[0], data = result[1];
                    if (!ok) { toast(data.error || 'Check failed.', 'error'); return; }
                    // "0 printed" is a real, useful answer here -- it means the
                    // pipeline works and there is genuinely nothing new.
                    toast(data.printed
                        ? data.printed + ' receipt(s) printed.'
                        : 'Checked - nothing new.');
                })
                .catch(function () { toast('Check failed.', 'error'); })
                .finally(function () { done(poll); });
        }
    });
})();

/*
 * Task row actions (MASTER_PLAN P2-2).
 *
 * All of these go through /api/tasks (P2-3) rather than form posts, so the row
 * updates in place instead of reloading and losing the reader's position in a
 * long list. The HTML forms still exist and still work with JavaScript off --
 * a LAN appliance that cannot add a task without JS is a downgrade.
 */
(function () {
    'use strict';

    function api(path, options) {
        return fetch('/api' + path, Object.assign({
            headers: {'Content-Type': 'application/json'}
        }, options || {})).then(function (r) {
            return r.json().then(function (d) {
                // The uniform envelope is what makes this one helper enough
                // for every call.
                if (!r.ok || !d.ok) throw new Error(d.error || 'Request failed.');
                return d.data;
            });
        });
    }

    function row(id) { return document.querySelector('[data-task="' + id + '"]'); }

    function closeMenus() {
        document.querySelectorAll('.row-menu[open]').forEach(function (m) {
            m.removeAttribute('open');
        });
    }

    document.addEventListener('click', function (e) {
        // A menu left open behind a dialog, or two open at once, both look
        // broken. Close on any click that is not inside one.
        if (!e.target.closest('.row-menu')) closeMenus();

        var toggle = e.target.closest('[data-toggle-task]');
        if (toggle) return;   // handled on 'change'

        var print = e.target.closest('[data-print-task]');
        if (print) {
            closeMenus();
            if (!confirm('Print "' + print.dataset.title + '" now?')) return;
            api('/tasks/' + print.dataset.printTask + '/print', {method: 'POST'})
                // Says "does not change the schedule" because that is the
                // question someone asks right after pressing it.
                .then(function () { toast('Printed. The schedule is unchanged.'); })
                .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        var duplicate = e.target.closest('[data-duplicate-task]');
        if (duplicate) {
            closeMenus();
            api('/tasks/' + duplicate.dataset.duplicateTask + '/duplicate', {method: 'POST'})
                .then(function () {
                    toast('Duplicated, and left paused so you can edit it.');
                    location.reload();
                })
                .catch(function (err) { toast(err.message, 'error'); });
            return;
        }

        var del = e.target.closest('[data-delete-task]');
        if (del) {
            closeMenus();
            if (!confirm('Delete "' + del.dataset.title + '"? This cannot be undone.')) return;
            api('/tasks/' + del.dataset.deleteTask, {method: 'DELETE'})
                .then(function () {
                    var tr = row(del.dataset.deleteTask);
                    if (tr) tr.remove();
                    toast('Deleted.');
                })
                .catch(function (err) { toast(err.message, 'error'); });
        }
    });

    document.addEventListener('change', function (e) {
        var toggle = e.target.closest('[data-toggle-task]');
        if (!toggle) return;
        var id = toggle.dataset.toggleTask;
        var wanted = toggle.checked;

        api('/tasks/' + id, {
            method: 'PATCH',
            body: JSON.stringify({enabled: wanted})
        }).then(function (task) {
            var tr = row(id);
            if (tr) tr.classList.toggle('task-inactive', !task.enabled);
            toast(task.enabled ? 'Resumed.' : 'Paused.');
        }).catch(function (err) {
            // Put the switch back. Leaving it showing a state the server
            // rejected is how someone comes to believe a task is running when
            // it is not.
            toggle.checked = !wanted;
            toast(err.message, 'error');
        });
    });

    // Escape closes an open row menu, like every other menu on the platform.
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeMenus();
    });
})();
