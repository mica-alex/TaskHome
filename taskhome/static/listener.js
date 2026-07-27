/*
 * SeeClickFix category picker (MASTER_PLAN P4-2/P4-3).
 *
 * Keeps the hidden request_types field in sync with the visible chips, so the
 * form still submits a plain comma string and the server contract is
 * unchanged.
 */
(function () {
  'use strict';

  var field = document.getElementById('request-types');
  var chips = document.getElementById('type-chips');
  var panel = document.getElementById('browse-panel');
  var results = document.getElementById('browse-results');
  var filter = document.getElementById('browse-filter');
  var known = {};          // id -> {title, organization}
  var loaded = [];

  function ids() {
    return field.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function setIds(list) {
    // Dedupe while preserving order: the same category added twice would poll
    // fine but read as a mistake.
    var seen = {};
    field.value = list.filter(function (id) {
      if (!id || seen[id]) return false;
      seen[id] = true;
      return true;
    }).join(',');
    redrawChips();
  }

  function redrawChips() {
    var list = ids();
    chips.innerHTML = '';
    if (!list.length) {
      var empty = document.createElement('span');
      empty.className = 'mica-help';
      empty.textContent = 'None yet — nothing will print.';
      chips.appendChild(empty);
      return;
    }
    list.forEach(function (id) {
      var meta = known[id] || {};
      var chip = document.createElement('span');
      chip.className = 'mica-chip ' + (meta.title ? 'mica-chip-accent' : 'mica-chip-muted');
      chip.title = meta.organization || 'Unknown organization';
      chip.textContent = meta.title || ('#' + id);
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'chip-remove';
      remove.innerHTML = '&times;';
      remove.setAttribute('aria-label', 'Remove ' + (meta.title || id));
      remove.addEventListener('click', function () {
        setIds(ids().filter(function (x) { return x !== id; }));
      });
      chip.appendChild(remove);
      chips.appendChild(chip);
    });
  }

  // Seed from what the server already rendered, so removing a chip before any
  // lookup happens does not lose its name.
  Array.prototype.forEach.call(chips.querySelectorAll('[data-id]'), function (node) {
    known[node.dataset.id] = {
      title: node.textContent.trim().replace(/\s*×$/, ''),
      organization: node.title
    };
  });
  redrawChips();

  document.getElementById('browse-btn').addEventListener('click', function () {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) panel.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  });

  function draw() {
    var needle = (filter.value || '').toLowerCase();
    var chosen = ids();
    results.innerHTML = '';
    var shown = loaded.filter(function (t) {
      return !needle ||
        (t.title || '').toLowerCase().indexOf(needle) !== -1 ||
        (t.organization || '').toLowerCase().indexOf(needle) !== -1;
    });
    if (!shown.length) {
      results.innerHTML = '<p class="mica-help">No categories match.</p>';
      return;
    }
    var org = null;
    shown.forEach(function (t) {
      if (t.organization !== org) {
        org = t.organization;
        var heading = document.createElement('h3');
        heading.className = 'mica-help browse-org';
        heading.textContent = org || 'Other';
        results.appendChild(heading);
      }
      var row = document.createElement('label');
      row.className = 'browse-row';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = chosen.indexOf(t.id) !== -1;
      box.addEventListener('change', function () {
        known[t.id] = {title: t.title, organization: t.organization};
        if (box.checked) setIds(ids().concat([t.id]));
        else setIds(ids().filter(function (x) { return x !== t.id; }));
      });
      row.appendChild(box);
      var text = document.createElement('span');
      text.innerHTML = t.title + ' <span class="mica-help">#' + t.id + '</span>';
      row.appendChild(text);
      results.appendChild(row);
    });
  }

  filter.addEventListener('input', draw);

  document.getElementById('browse-go').addEventListener('click', function () {
    var button = this;
    button.disabled = true;
    results.innerHTML = '<p class="mica-help">Looking up…</p>';
    var lat = encodeURIComponent(document.getElementById('lat').value);
    var lng = encodeURIComponent(document.getElementById('lng').value);
    fetch('/api/scf/browse?lat=' + lat + '&lng=' + lng)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) {
          results.innerHTML = '<p class="studio-error">' + data.error + '</p>';
          return;
        }
        loaded = data.request_types;
        data.request_types.forEach(function (t) {
          if (!known[t.id]) known[t.id] = {title: t.title, organization: t.organization};
        });
        draw();
      })
      .catch(function () {
        results.innerHTML = '<p class="studio-error">Could not reach the server.</p>';
      })
      .finally(function () { button.disabled = false; });
  });
})();
