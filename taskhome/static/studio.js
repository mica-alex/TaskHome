/*
 * Receipt Style Studio (MASTER_PLAN P3-4).
 *
 * This file builds the editor and asks the server to render previews. It
 * deliberately contains NO layout logic: measuring text, wrapping it, or
 * estimating height here would create a second renderer that can disagree with
 * the printer. That has already happened once -- the printer hard-wrapped
 * mid-word while the preview wrapped on word boundaries -- and the whole point
 * of the shared renderer is that it cannot happen again.
 */
(function () {
  'use strict';

  var KIND = new URLSearchParams(location.search).get('kind') || 'task';
  var template = JSON.parse(document.getElementById('template-data').textContent);
  var LIST_SOURCES = JSON.parse(document.getElementById('list-sources').textContent);
  var SOURCE_NAMES = Object.keys(LIST_SOURCES);
  var blocksEl = document.getElementById('blocks');
  var previewEl = document.getElementById('preview-body');
  var metaEl = document.getElementById('preview-meta');
  var errorEl = document.getElementById('preview-error');
  var lastFocused = null;

  var FIELDS = {
    text: [
      {key: 'value', label: 'Text', type: 'text', wide: true},
      {key: 'font', label: 'Font', type: 'select', options: [['b', 'Small (64 cols)'], ['a', 'Large (48 cols)']]},
      {key: 'width', label: 'Width', type: 'number', min: 1, max: 4},
      {key: 'height', label: 'Height', type: 'number', min: 1, max: 4},
      {key: 'align', label: 'Align', type: 'select', options: [['center', 'Center'], ['left', 'Left'], ['right', 'Right']]},
      {key: 'bold', label: 'Bold', type: 'checkbox'}
    ],
    qr: [{key: 'value', label: 'URL', type: 'text', wide: true},
         {key: 'size', label: 'Size', type: 'number', min: 1, max: 10}],
    barcode: [{key: 'value', label: 'Value', type: 'text', wide: true},
              {key: 'height', label: 'Height', type: 'number', min: 10, max: 200}],
    rule: [{key: 'char', label: 'Character', type: 'text'}],
    gap: [{key: 'dots', label: 'Dots', type: 'number', min: 1, max: 100}],
    blank: [{key: 'count', label: 'Lines', type: 'number', min: 1, max: 5}],
    /*
     * A list block prints its fields once per item of a repeating source -- a
     * digest's entries, say. The server expands it into ordinary text and qr
     * blocks at fill time, so the preview below is still the real renderer.
     */
    list: [
      {key: 'source', label: 'Repeat over', type: 'select',
       options: SOURCE_NAMES.map(function (name) { return [name, name]; })},
      {key: 'value', label: 'Text per item', type: 'text', wide: true},
      {key: 'qr_value', label: 'QR per item (blank for none)', type: 'text', wide: true},
      {key: 'font', label: 'Font', type: 'select', options: [['b', 'Small (64 cols)'], ['a', 'Large (48 cols)']]},
      {key: 'width', label: 'Width', type: 'number', min: 1, max: 4},
      {key: 'height', label: 'Height', type: 'number', min: 1, max: 4},
      {key: 'align', label: 'Align', type: 'select', options: [['left', 'Left'], ['center', 'Center'], ['right', 'Right']]},
      {key: 'bold', label: 'Bold', type: 'checkbox'},
      {key: 'size', label: 'QR size', type: 'number', min: 1, max: 10},
      {key: 'gap', label: 'Space between items', type: 'number', min: 0, max: 100}
    ]
  };

  var DEFAULTS = {
    text: {type: 'text', value: 'New line', font: 'b', width: 1, height: 1, align: 'center', bold: false},
    qr: {type: 'qr', value: '{qr_url}', size: 4},
    barcode: {type: 'barcode', value: '{id}', height: 60},
    rule: {type: 'rule', char: '-'},
    gap: {type: 'gap', dots: 8},
    blank: {type: 'blank', count: 1},
    list: listDefault()
  };

  /* A new list block, guessing sensible fields from the item placeholders the
   * source offers -- an empty one previews as nothing at all, which reads as a
   * broken button. */
  function listDefault() {
    var source = SOURCE_NAMES[0];
    if (!source) return {type: 'list'};
    var names = LIST_SOURCES[source] || [];
    function first(re) {
      return names.filter(function (n) { return re.test(n); })[0] || '';
    }
    var label = first(/title|name|label|text/) || names[0] || '';
    var link = first(/link|url/);
    return {
      type: 'list', source: source,
      value: label ? '{' + label + '}' : '',
      qr_value: link ? '{' + link + '}' : '',
      font: 'b', width: 1, height: 1, align: 'left', bold: false,
      size: 3, gap: 8
    };
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderField(block, spec, index) {
    var wrap = el('div', 'studio-field' + (spec.wide ? ' studio-field-wide' : ''));
    var id = 'b' + index + '-' + spec.key;
    var label = el('label', null, spec.label);
    label.setAttribute('for', id);
    wrap.appendChild(label);

    var input;
    if (spec.type === 'select') {
      input = el('select');
      spec.options.forEach(function (opt) {
        var option = el('option', null, opt[1]);
        option.value = opt[0];
        if (String(block[spec.key]) === opt[0]) option.selected = true;
        input.appendChild(option);
      });
    } else {
      input = el('input');
      input.type = spec.type === 'checkbox' ? 'checkbox' : (spec.type === 'number' ? 'number' : 'text');
      if (spec.type === 'checkbox') input.checked = !!block[spec.key];
      else input.value = block[spec.key] === undefined ? '' : block[spec.key];
      if (spec.min !== undefined) input.min = spec.min;
      if (spec.max !== undefined) input.max = spec.max;
    }
    input.id = id;
    input.className = 'mica-input';
    input.addEventListener('input', function () {
      block[spec.key] = spec.type === 'checkbox' ? input.checked
        // `|| 1` for a field whose floor is 1; a field that allows 0 has to
        // keep it, or "no space between items" silently becomes one dot.
        : spec.type === 'number' ? (parseInt(input.value, 10) || (spec.min === 0 ? 0 : 1))
        : input.value;
      schedulePreview();
    });
    input.addEventListener('change', schedulePreview);
    if (spec.key === 'value' || spec.key === 'qr_value') {
      input.addEventListener('focus', function () { lastFocused = input; });
    }
    wrap.appendChild(input);
    return wrap;
  }

  function move(index, delta) {
    var target = index + delta;
    if (target < 0 || target >= template.blocks.length) return;
    var moved = template.blocks.splice(index, 1)[0];
    template.blocks.splice(target, 0, moved);
    draw();
  }

  function draw() {
    blocksEl.innerHTML = '';
    template.blocks.forEach(function (block, index) {
      var card = el('div', 'studio-block');
      var head = el('div', 'studio-block-head');
      head.appendChild(el('span', 'mica-chip mica-chip-muted', block.type));

      var controls = el('div', 'studio-block-controls');
      [['↑', -1], ['↓', 1]].forEach(function (pair) {
        var btn = el('button', 'mica-btn mica-btn-secondary', pair[0]);
        btn.type = 'button';
        btn.addEventListener('click', function () { move(index, pair[1]); });
        controls.appendChild(btn);
      });
      var remove = el('button', 'mica-btn mica-btn-danger', '×');
      remove.type = 'button';
      remove.title = 'Remove this block';
      remove.addEventListener('click', function () {
        template.blocks.splice(index, 1);
        draw();
      });
      controls.appendChild(remove);
      head.appendChild(controls);
      card.appendChild(head);

      var grid = el('div', 'studio-block-fields');
      (FIELDS[block.type] || []).forEach(function (spec) {
        grid.appendChild(renderField(block, spec, index));
      });
      card.appendChild(grid);
      blocksEl.appendChild(card);
    });
    schedulePreview();
  }

  /*
   * Paint the rows the server produced. Deliberately dumb: it sets text and a
   * couple of custom properties and nothing else. Any measuring or wrapping
   * here would be a second renderer that can disagree with the printer.
   */
  function drawPreview(rows) {
    previewEl.innerHTML = '';
    rows.forEach(function (row) {
      var node = document.createElement('div');
      if (row.kind === 'text') {
        node.className = 'r-line r-' + (row.align || 'center') + (row.bold ? ' r-bold' : '');
        node.style.setProperty('--scale', row.scale);
        node.style.setProperty('--h', row.h);
        node.style.setProperty('--w', row.w);
        var span = document.createElement('span');
        span.textContent = row.text;
        node.appendChild(span);
      } else if (row.kind === 'qr') {
        node.className = 'r-qr';
        node.style.setProperty('--modules', row.modules);
        node.style.setProperty('--size', row.size);
        node.title = row.value;
        node.appendChild(document.createTextNode('QR'));
      } else if (row.kind === 'barcode') {
        node.className = 'r-barcode';
        node.style.setProperty('--bh', row.height);
        var label = document.createElement('span');
        label.textContent = row.value;
        node.appendChild(label);
      } else if (row.kind === 'gap') {
        node.className = 'r-gap';
        node.style.setProperty('--dots', row.dots);
      } else {
        node.className = 'r-blank';
      }
      previewEl.appendChild(node);
    });
  }

  var timer = null;
  function schedulePreview() {
    clearTimeout(timer);
    timer = setTimeout(refreshPreview, 200);
  }

  function refreshPreview() {
    template.name = document.getElementById('template-name').value;
    fetch('/api/receipt/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({template: template})
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.ok) {
        drawPreview(data.rows);
        metaEl.textContent = data.height_mm + ' mm · ' + data.columns + ' columns';
        errorEl.hidden = true;
      } else {
        // Keep the last good preview on screen: blanking it while someone is
        // mid-edit loses the context they are working against.
        errorEl.textContent = data.error;
        errorEl.hidden = false;
      }
    }).catch(function () {
      errorEl.textContent = 'Could not reach the server.';
      errorEl.hidden = false;
    });
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (d) { return {status: r.status, data: d}; }); });
  }

  function save(activate, button) {
    template.name = document.getElementById('template-name').value;
    template.builtin = false;      // saving always produces a user template
    button.disabled = true;
    post('/api/receipt/templates/' + KIND, {template: template, activate: activate})
      .then(function (res) {
        if (res.data.ok) {
          location.search = '?kind=' + KIND + '&name=' + encodeURIComponent(res.data.name);
        } else {
          errorEl.textContent = res.data.error;
          errorEl.hidden = false;
        }
      })
      .finally(function () { button.disabled = false; });
  }

  document.getElementById('save-btn').addEventListener('click', function () { save(true, this); });
  document.getElementById('save-only-btn').addEventListener('click', function () { save(false, this); });

  document.getElementById('test-print-btn').addEventListener('click', function () {
    var button = this;
    if (!confirm('Print this template on real paper?')) return;
    button.disabled = true;
    post('/api/receipt/test_print/' + KIND, {template: template})
      .then(function (res) {
        if (!res.data.ok) {
          errorEl.textContent = res.data.error;
          errorEl.hidden = false;
        }
      })
      .finally(function () { button.disabled = false; });
  });

  document.querySelectorAll('[data-add]').forEach(function (button) {
    button.addEventListener('click', function () {
      template.blocks.push(Object.assign({}, DEFAULTS[button.dataset.add]));
      draw();
    });
  });

  document.querySelectorAll('[data-placeholder]').forEach(function (button) {
    button.addEventListener('click', function () {
      if (!lastFocused) return;
      var token = '{' + button.dataset.placeholder + '}';
      var start = lastFocused.selectionStart || lastFocused.value.length;
      lastFocused.value = lastFocused.value.slice(0, start) + token +
                          lastFocused.value.slice(lastFocused.selectionEnd || start);
      lastFocused.dispatchEvent(new Event('input'));
      lastFocused.focus();
    });
  });

  document.getElementById('template-name').addEventListener('input', schedulePreview);
  draw();
})();
