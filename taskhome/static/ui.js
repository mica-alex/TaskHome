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
