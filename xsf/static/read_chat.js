/* 细读·AI 浮窗 (2026-11-03)
 * 可拖拽/右下角缩放/位置尺寸 localStorage 记忆/收起成悬浮球。
 * 依赖 window.__READ = {enabled, vision:[...]}; window.__readState.pendingCtx。
 */
(function () {
  'use strict';
  if (!window.__READ || !window.__READ.enabled) return;

  var GEOM_KEY = 'read_chat_geom', OPEN_KEY = 'read_chat_open';
  var el = null, ball = null, bodyEl = null, taEl = null;
  var vision = window.__READ.vision || [];
  var history = [];
  var sending = false;

  function api(path) {
    return '/collections/' + encodeURIComponent(collection) +
      '/doc/' + docId + path;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  document.addEventListener('DOMContentLoaded', function () {
    buildDom();
    loadHistory();
    if (sessionStorage.getItem(OPEN_KEY) === '1') open();
  });

  // ── DOM ──────────────────────────────────
  function buildDom() {
    ball = document.createElement('button');
    ball.id = 'readBall';
    ball.title = 'AI 细读问答';
    ball.textContent = 'AI';
    ball.addEventListener('click', open);
    document.body.appendChild(ball);

    el = document.createElement('div');
    el.id = 'readChatWin';
    el.innerHTML =
      '<div class="rcw-head" id="rcwHead"><span class="rcw-title">细读 AI</span>' +
      '<button id="rcwClear" title="清空本篇对话">清空</button>' +
      '<button id="rcwClose" title="收起">收起</button></div>' +
      '<div class="rcw-body" id="rcwBody"></div>' +
      '<div class="rcw-foot">' +
      '<div class="rcw-row">' +
      '<select id="rcwMode"><option value="text">问文本</option>' +
      '<option value="vision"' + (vision.length ? '' : ' disabled') + '>问图</option></select>' +
      (vision.length > 1 ? '<select id="rcwProv">' +
        vision.map(function (v) {
          return '<option value="' + esc(v.id) + '">' + esc(v.name) + '</option>';
        }).join('') + '</select>' : '') +
      '<label title="把你在本页的批注一并作为上下文"><input type="checkbox" id="rcwNotes" checked>带批注</label>' +
      '<span id="rcwCtx" style="color:var(--text-muted)"></span>' +
      '</div>' +
      '<textarea class="rcw-ta" id="rcwTa" placeholder="提问…（可先在图上框选, 点「问 AI」自动带选区）"></textarea>' +
      '<button class="rcw-send" id="rcwSend">发送</button>' +
      '</div><div class="rcw-resize" id="rcwResize"></div>';
    document.body.appendChild(el);
    bodyEl = el.querySelector('#rcwBody');
    taEl = el.querySelector('#rcwTa');

    el.querySelector('#rcwClose').addEventListener('click', close);
    el.querySelector('#rcwClear').addEventListener('click', clearThread);
    el.querySelector('#rcwSend').addEventListener('click', send);
    taEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
    });
    el.querySelector('#rcwMode').addEventListener('change', updateCtxHint);

    restoreGeom();
    makeDrag(el.querySelector('#rcwHead'), el);
    makeResize(el.querySelector('#rcwResize'), el);
  }

  function open() {
    el.style.display = 'flex';
    ball.style.display = 'none';
    sessionStorage.setItem(OPEN_KEY, '1');
    updateCtxHint();
    bodyEl.scrollTop = bodyEl.scrollHeight;
    taEl.focus();
  }
  function close() {
    el.style.display = 'none';
    ball.style.display = 'block';
    sessionStorage.setItem(OPEN_KEY, '0');
  }
  window.__readChatOpen = open;

  function geom() {
    try { return JSON.parse(localStorage.getItem(GEOM_KEY) || 'null'); }
    catch (e) { return null; }
  }
  function restoreGeom() {
    var g = geom();
    if (g) {
      el.style.left = Math.min(g.x, window.innerWidth - 80) + 'px';
      el.style.top = Math.min(g.y, window.innerHeight - 60) + 'px';
      el.style.width = g.w + 'px';
      el.style.height = g.h + 'px';
    } else {
      el.style.right = '26px';
      el.style.bottom = '76px';
      el.style.width = '380px';
      el.style.height = '480px';
    }
  }
  function saveGeom() {
    var r = el.getBoundingClientRect();
    localStorage.setItem(GEOM_KEY, JSON.stringify({
      x: Math.round(r.left), y: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height),
    }));
  }

  function makeDrag(handle, target) {
    var sx, sy, ox, oy, moving = false;
    handle.addEventListener('pointerdown', function (e) {
      if (e.target.tagName === 'BUTTON') return;
      moving = true;
      sx = e.clientX; sy = e.clientY;
      var r = target.getBoundingClientRect();
      ox = r.left; oy = r.top;
      handle.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    handle.addEventListener('pointermove', function (e) {
      if (!moving) return;
      target.style.left = Math.max(0, Math.min(ox + e.clientX - sx,
        window.innerWidth - 60)) + 'px';
      target.style.top = Math.max(0, Math.min(oy + e.clientY - sy,
        window.innerHeight - 40)) + 'px';
      target.style.right = 'auto';
      target.style.bottom = 'auto';
    });
    handle.addEventListener('pointerup', function () {
      moving = false; saveGeom();
    });
  }

  function makeResize(handle, target) {
    var sx, sy, sw, sh, moving = false;
    handle.addEventListener('pointerdown', function (e) {
      moving = true; sx = e.clientX; sy = e.clientY;
      var r = target.getBoundingClientRect();
      sw = r.width; sh = r.height;
      handle.setPointerCapture(e.pointerId);
      e.preventDefault();
      e.stopPropagation();
    });
    handle.addEventListener('pointermove', function (e) {
      if (!moving) return;
      target.style.width = Math.max(300, sw + e.clientX - sx) + 'px';
      target.style.height = Math.max(320, sh + e.clientY - sy) + 'px';
    });
    handle.addEventListener('pointerup', function () {
      moving = false; saveGeom();
    });
  }

  // ── 历史 ─────────────────────────────────
  function loadHistory() {
    fetch(api('/read/chat')).then(function (r) { return r.json(); })
      .then(function (d) {
        history = d.items || [];
        renderHistory();
      });
  }

  function renderHistory() {
    if (!history.length) {
      bodyEl.innerHTML = '<div class="rcw-empty">暂无对话——图上框选后点「问 AI」，' +
        '或直接输入提问（带本页上下文）</div>';
      return;
    }
    bodyEl.innerHTML = '';
    history.forEach(function (m, i) { appendMsg(m, i); });
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function appendMsg(m, idx) {
    if (bodyEl.querySelector('.rcw-empty')) bodyEl.innerHTML = '';
    var div = document.createElement('div');
    div.className = 'rcw-msg ' + m.role;
    var ctxHtml = '';
    if (m.role === 'user' && m.ctx) {
      var c = {};
      try { c = JSON.parse(m.ctx); } catch (e) {}
      var bits = [];
      if (c.page) bits.push('第' + c.page + '页');
      if (c.rect) bits.push('选区');
      if (c.mode === 'vision') bits.push('问图');
      if (c.with_notes) bits.push('带批注');
      if (bits.length) ctxHtml = '<div class="rcw-ctx">' + esc(bits.join(' · ')) +
        (c.clip_text ? ' · 「' + esc(c.clip_text.slice(0, 60)) +
          (c.clip_text.length > 60 ? '…' : '') + '」' : '') + '</div>';
    }
    if (m.role === 'assistant') {
      ctxHtml += '<div class="rcw-ops">' +
        '<button data-op="toann" data-idx="' + idx + '"' +
        (hasRectBefore(idx) ? '' : ' disabled title="需先框选提问才能转批注"') +
        '>转批注</button>' +
        '<button data-op="copy">复制</button></div>';
    }
    div.innerHTML = esc(m.content) + ctxHtml;
    bodyEl.appendChild(div);
    div.querySelectorAll('.rcw-ops button').forEach(function (b) {
      if (b.dataset.op === 'copy') {
        b.addEventListener('click', function () {
          navigator.clipboard && navigator.clipboard.writeText(m.content);
          b.textContent = '已复制';
          setTimeout(function () { b.textContent = '复制'; }, 1200);
        });
      } else if (b.dataset.op === 'toann' && !b.disabled) {
        b.addEventListener('click', function () { toAnnotation(+b.dataset.idx, b); });
      }
    });
    return div;
  }

  function hasRectBefore(idx) {
    for (var i = idx - 1; i >= 0 && history[i]; i--) {
      if (history[i].role === 'user' && history[i].ctx) {
        try {
          var c = JSON.parse(history[i].ctx);
          return !!(c.rect && c.page);
        } catch (e) { return false; }
      }
    }
    return false;
  }

  // AI 回答 → 批注 (人工逐条采纳, origin='ai')
  function toAnnotation(idx, btn) {
    var msg = history[idx];
    var ctx = null;
    for (var i = idx - 1; i >= 0 && history[i]; i--) {
      if (history[i].role === 'user' && history[i].ctx) {
        try { ctx = JSON.parse(history[i].ctx); } catch (e) {}
        break;
      }
    }
    if (!ctx || !ctx.rect || !ctx.page) {
      window.__readToast && window.__readToast('该回答没有选区锚点');
      return;
    }
    btn.disabled = true;
    window.__readSave('rect', ctx.rect,
      { note: msg.content, origin: 'ai' },
      function () {
        btn.textContent = '已转';
        window.__readToast && window.__readToast('已存为批注 (标 AI)');
      });
  }

  // ── 发送 ─────────────────────────────────
  function updateCtxHint() {
    var st = window.__readState;
    var hint = el.querySelector('#rcwCtx');
    if (!hint) return;
    if (st && st.pendingCtx) {
      hint.textContent = '带选区 (第' + st.pendingCtx.page + '页)';
    } else {
      hint.textContent = '';
    }
  }

  function send() {
    if (sending) return;
    var q = taEl.value.trim();
    if (!q) return;
    var st = window.__readState || {};
    var pc = st.pendingCtx || null;
    var body = new URLSearchParams();
    body.append('question', q);
    body.append('mode', el.querySelector('#rcwMode').value);
    body.append('with_notes', el.querySelector('#rcwNotes').checked ? 1 : 0);
    var prov = el.querySelector('#rcwProv');
    if (prov) body.append('provider', prov.value);
    if (pc) {
      body.append('page_num', pc.page);
      body.append('rect', JSON.stringify(pc.rect));
    } else {
      body.append('page_num', (window.__readPage ? window.__readPage() : page));
    }
    sending = true;
    var sendBtn = el.querySelector('#rcwSend');
    sendBtn.disabled = true;
    sendBtn.textContent = '思考中…';
    fetch(api('/read/chat'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    }).then(function (r) { return r.json(); }).then(function (d) {
      sending = false;
      sendBtn.disabled = false;
      sendBtn.textContent = '发送';
      if (d.error) {
        window.__readToast && window.__readToast('失败: ' + d.error);
        return;
      }
      taEl.value = '';
      if (window.__readState) window.__readState.pendingCtx = null;
      updateCtxHint();
      history.push(d.user, d.assistant);
      appendMsg(d.user, history.length - 2);
      appendMsg(d.assistant, history.length - 1);
      bodyEl.scrollTop = bodyEl.scrollHeight;
    }).catch(function (e) {
      sending = false;
      sendBtn.disabled = false;
      sendBtn.textContent = '发送';
      window.__readToast && window.__readToast('请求失败: ' + e.message);
    });
  }

  function clearThread() {
    if (!confirm('清空本篇全部对话？')) return;
    fetch(api('/read/chat'), { method: 'DELETE' })
      .then(function () { history = []; renderHistory(); });
  }
})();
