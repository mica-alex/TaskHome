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

  document.addEventListener('DOMContentLoaded', function () {
    wireDialogs();
    wireRecurrence();
  });

  global.TaskHome = {toast: toast, wireDialogs: wireDialogs, wireRecurrence: wireRecurrence};
})(window);
