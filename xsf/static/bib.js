/* ── 共享书目元数据编辑器 ──
 *
 * 依赖页面提供以下全局变量 (由服务端模板注入):
 *   BIB_TYPE_LABELS   — { '@book': '图书', ... }
 *   BIB_TYPE_FIELDS   — { '@book': ['author','title',...], ... }
 *   BIB_FIELD_LABELS  — { 'author': '作者', ... }
 *
 * 调用:
 *   bibEditor.mount(panelElId, { type, data, docId, coll, citeKey, readOnlyCiteKey })
 *   bibEditor.getData() → { bib_type, bib_data }
 */

var bibEditor = (function() {

  var _state = {
    type: '',
    data: {},
    docId: 0,
    coll: '',
    citeKey: '',
    readOnlyCiteKey: false,
    extraFields: [],
    panelEl: null,
  };

  function _label(f) {
    return (window.BIB_FIELD_LABELS && window.BIB_FIELD_LABELS[f]) || f;
  }

  function _escape(s) {
    if (!s) return '';
    var div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
  }

  /**
   * 挂载 bib 编辑器到指定容器.
   * @param {string} panelId - 容器元素 ID
   * @param {object} opts - { type, data, docId, coll, citeKey, readOnlyCiteKey }
   */
  function mount(panelId, opts) {
    _state.panelEl = document.getElementById(panelId);
    if (!_state.panelEl) return;
    _state.type = opts.type || '';
    _state.data = Object.assign({}, opts.data || {});
    _state.docId = opts.docId || 0;
    _state.coll = opts.coll || '';
    _state.citeKey = opts.citeKey || '';
    _state.readOnlyCiteKey = !!opts.readOnlyCiteKey;
    _state.extraFields = [];

    var presetFields = (window.BIB_TYPE_FIELDS && window.BIB_TYPE_FIELDS[_state.type]) || [];
    for (var k in _state.data) {
      if (!_data_has(presetFields, k)) _state.extraFields.push(k);
    }

    _render();
  }

  function _data_has(arr, v) {
    for (var i = 0; i < arr.length; i++) { if (arr[i] === v) return true; }
    return false;
  }

  function _render() {
    var el = _state.panelEl;
    var html = '';

    // 类型选择
    html += '<div class="bib-row bib-type-row"><label>文献类型</label>';
    html += '<select id="' + el.id + '_typeSel" onchange="bibEditor.onTypeChange()">';
    html += '<option value="">— 未指定 —</option>';
    var labels = window.BIB_TYPE_LABELS || {};
    for (var t in labels) {
      html += '<option value="' + t + '"' + (t === _state.type ? ' selected' : '') + '>' + _escape(labels[t]) + '</option>';
    }
    if (_state.type && !labels[_state.type]) {
      html += '<option value="' + _escape(_state.type) + '" selected>' + _escape(_state.type.replace(/^@/, '') + ' (自定义)') + '</option>';
    }
    html += '</select></div>';

    // 导入区
    html += '<div class="bib-import-row">';
    html += '<input type="file" id="' + el.id + '_bibFile" accept=".bib,.bibtex,.txt" style="display:none" onchange="bibEditor.importFile(event)">';
    html += '<button type="button" class="bib-import-btn" onclick="document.getElementById(\'' + el.id + '_bibFile\').click()">导入 .bib</button>';
    html += '<textarea class="bib-paste-input" placeholder="粘贴 BibTeX 后点「识别」" rows="3"></textarea>';
    html += '<button type="button" class="bib-import-btn" onclick="bibEditor.onPaste(this.parentElement.querySelector(\'.bib-paste-input\'))">识别</button>';
    html += '<span class="bib-import-msg" id="' + el.id + '_msg"></span>';
    html += '</div>';

    // 字段区
    html += '<div id="' + el.id + '_fields"></div>';

    // 自定义字段添加
    html += '<div class="bib-add-field"><input id="' + el.id + '_newKey" placeholder="自定义字段名（如 keywords）" onkeydown="if(event.key===\'Enter\'){event.preventDefault();bibEditor.addField();}"><button class="bib-save-btn" style="padding:2px 10px;font-size:0.72rem;" onclick="bibEditor.addField()">+</button></div>';

    // cite_key
    var ckClass = _state.readOnlyCiteKey ? 'bib-citekey-readonly' : 'bib-citekey-val';
    html += '<div class="bib-row bib-citekey-row"><label>cite_key</label><span class="' + ckClass + '" id="' + el.id + '_ck">' + _escape(_state.citeKey || '(自动生成)') + '</span></div>';

    // 保存
    html += '<div class="bib-save-row"><button class="bib-save-btn" id="' + el.id + '_saveBtn" onclick="bibEditor.save()">保存元数据</button><span class="bib-saved-msg" id="' + el.id + '_savedMsg"></span></div>';

    el.innerHTML = html;
    _renderFields();
  }

  function _renderFields() {
    var container = document.getElementById(_state.panelEl.id + '_fields');
    if (!container) return;
    var presetFields = (window.BIB_TYPE_FIELDS && window.BIB_TYPE_FIELDS[_state.type]) || [];
    var all = presetFields.concat(_state.extraFields);
    var html = '';
    for (var i = 0; i < all.length; i++) {
      var f = all[i];
      var isPreset = _data_has(presetFields, f);
      html += '<div class="bib-row"><label>' + _escape(_label(f)) + '</label>';
      html += '<input type="text" data-bibkey="' + _escape(f) + '" value="' + _escape(_state.data[f] || '') + '" placeholder="' + _escape(_label(f)) + '">';
      if (!isPreset) {
        html += '<span class="bib-field-del" onclick="bibEditor.removeField(\'' + _escape(f) + '\')">&times;</span>';
      }
      html += '</div>';
    }
    container.innerHTML = html;
  }

  function onTypeChange() {
    var sel = document.getElementById(_state.panelEl.id + '_typeSel');
    _state.type = sel.value;
    _renderFields();
  }

  function addField() {
    var input = document.getElementById(_state.panelEl.id + '_newKey');
    var key = (input.value || '').trim();
    if (!key) return;
    var preset = (window.BIB_TYPE_FIELDS && window.BIB_TYPE_FIELDS[_state.type]) || [];
    if (_data_has(preset, key) || _data_has(_state.extraFields, key)) return;
    _state.extraFields.push(key);
    input.value = '';
    _renderFields();
  }

  function removeField(key) {
    _state.extraFields = _state.extraFields.filter(function(f) { return f !== key; });
    delete _state.data[key];
    _renderFields();
  }

  function collectData() {
    var data = {};
    var inputs = _state.panelEl.querySelectorAll('input[data-bibkey]');
    inputs.forEach(function(inp) {
      var key = inp.dataset.bibkey;
      var val = (inp.value || '').trim();
      if (val) data[key] = val;
    });
    return data;
  }

  function getData() {
    return { bib_type: _state.type, bib_data: collectData() };
  }

  function save() {
    var btnId = _state.panelEl.id + '_saveBtn';
    var msgId = _state.panelEl.id + '_savedMsg';
    var btn = document.getElementById(btnId);
    var msg = document.getElementById(msgId);
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    if (msg) msg.textContent = '';
    var bibData = collectData();
    var body = new URLSearchParams();
    body.append('bib_type', _state.type);
    body.append('bib_data', JSON.stringify(bibData));
    fetch('/collections/' + encodeURIComponent(_state.coll) + '/doc/' + _state.docId, {
      method: 'PATCH', body: body
    }).then(function(res) {
      return res.json().then(function(result) {
        if (!res.ok || result.error) {
          if (msg) msg.textContent = '✗ ' + (result.error || '保存失败');
          return;
        }
        if (msg) msg.textContent = '✓ 已保存';
        if (result.cite_key) {
          _state.citeKey = result.cite_key;
          var ckEl = document.getElementById(_state.panelEl.id + '_ck');
          if (ckEl) ckEl.textContent = result.cite_key;
        }
        _state.data = bibData;
        setTimeout(function() { if (msg) msg.textContent = ''; }, 3000);
      });
    }).catch(function(e) {
      if (msg) msg.textContent = '✗ ' + e.message;
    }).finally(function() {
      if (btn) { btn.disabled = false; btn.textContent = '保存元数据'; }
    });
  }

  /* ── BibTeX 解析 + 导入 ── */

  var _TYPE_ALIASES = {
    '@conference': '@inproceedings',
    '@electronic': '@online',
    '@www': '@online'
  };

  function _normalizeType(type, data) {
    if (type === '@thesis') {
      var tv = ((data && data.type) || '').toLowerCase();
      if (tv.indexOf('master') >= 0 || tv.indexOf('mathesis') >= 0) return '@mastersthesis';
      return '@phdthesis';
    }
    return _TYPE_ALIASES[type] || type;
  }

  function parseBibtex(text) {
    var atIdx = text.indexOf('@');
    if (atIdx < 0) return null;
    var i = atIdx + 1;
    while (i < text.length && /[a-zA-Z]/.test(text[i])) i++;
    var type = text.slice(atIdx + 1, i).toLowerCase();
    while (i < text.length && /\s/.test(text[i])) i++;
    if (text[i] !== '{') return null;
    var depth = 1, j = i + 1;
    var chars = [];
    while (j < text.length && depth > 0) {
      if (text[j] === '{') depth++;
      else if (text[j] === '}') { depth--; if (depth === 0) break; }
      chars.push(text[j]);
      j++;
    }
    var body = chars.join('');
    var commaIdx = body.indexOf(',');
    if (commaIdx < 0) return null;
    var citeKey = body.slice(0, commaIdx).trim();
    var fieldsStr = body.slice(commaIdx + 1);
    var data = {};
    var fieldRe = /(\w+)\s*=\s*(?:\{([^}]*)\}|"([^"]*)")/g;
    var fm;
    while ((fm = fieldRe.exec(fieldsStr)) !== null) {
      var key = fm[1].toLowerCase();
      var val = (fm[2] !== undefined ? fm[2] : fm[3]).trim();
      if (val) data[key] = val;
    }
    return { bib_type: _normalizeType('@' + type, data), cite_key: citeKey, bib_data: data };
  }

  function _applyResult(result) {
    if (!result || !result.bib_data || Object.keys(result.bib_data).length === 0) {
      _showMsg('✗ 解析失败，请检查 BibTeX 格式');
      return;
    }
    _state.type = result.bib_type;
    _state.data = result.bib_data;
    _state.extraFields = [];
    var preset = (window.BIB_TYPE_FIELDS && window.BIB_TYPE_FIELDS[result.bib_type]) || [];
    for (var k in result.bib_data) {
      if (!_data_has(preset, k)) _state.extraFields.push(k);
    }
    _render();
    var freshCk = document.getElementById(_state.panelEl.id + '_ck');
    if (freshCk) freshCk.textContent = result.cite_key || '(自动生成)';
    _showMsg('✓ 导入 ' + Object.keys(result.bib_data).length + ' 字段');
  }

  function _showMsg(text) {
    var msg = document.getElementById(_state.panelEl.id + '_msg');
    if (msg) {
      msg.textContent = text;
      setTimeout(function() { msg.textContent = ''; }, 4000);
    }
  }

  function importFile(event) {
    var file = event.target.files[0];
    if (!file) return;
    _showMsg('解析中…');
    var reader = new FileReader();
    var self = this;
    reader.onload = function(e) {
      _applyResult(parseBibtex(e.target.result));
    };
    reader.readAsText(file);
    event.target.value = '';
  }

  function onPaste(el) {
    var text = (el.value || '').trim();
    if (!text || text.indexOf('@') < 0) return;
    var result = parseBibtex(text);
    if (result && result.bib_data && Object.keys(result.bib_data).length > 0) {
      _applyResult(result);
      var ta = _state.panelEl.querySelector('.bib-paste-input');
      if (ta) ta.value = '';
    } else {
      _showMsg('✗ 解析失败，请检查格式');
    }
  }

  return {
    mount: mount,
    getData: getData,
    onTypeChange: onTypeChange,
    addField: addField,
    removeField: removeField,
    save: save,
    importFile: importFile,
    onPaste: onPaste,
    parseBibtex: parseBibtex,
  };
})();
