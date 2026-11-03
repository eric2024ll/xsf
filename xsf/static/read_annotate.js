/* 细读·批注层 + 本页卡片流 (2026-11-03)
 * 依赖页面全局: collection / docId / page / pageCount / origWidth / origHeight
 * window.__READ = {enabled, vision:[{id,name,model}]}
 * 坐标: 存归一化 0-1; 渲染乘 img 显示尺寸。
 */
(function () {
  'use strict';
  if (!window.__READ || !window.__READ.enabled) return;

  var COLORS = ['#e11d48', '#2563eb', '#16a34a', '#d97706', '#7c3aed'];
  var state = {
    tool: null,            // 'rect' | 'pen' | 'highlight' | null
    color: COLORS[0],
    items: [],             // 当前文献批注
    filter: 'all',
    pendingCtx: null,      // {page, rect} 问 AI 待带选区
  };
  window.__readState = state;

  var svgNS = 'http://www.w3.org/2000/svg';
  var img, layer, svg;

  function api(path) {
    return '/collections/' + encodeURIComponent(collection) +
      '/doc/' + docId + path;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── 初始化 ───────────────────────────────
  document.addEventListener('DOMContentLoaded', init);

  function init() {
    img = document.getElementById('pageImage');
    var wrap = img ? img.parentNode : null;
    if (!wrap) return;
    svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('id', 'readSvg');
    layer = document.createElement('div');
    layer.id = 'readLayer';
    layer.appendChild(svg);
    wrap.appendChild(layer);
    img.addEventListener('load', relayout);
    window.addEventListener('resize', relayout);
    // 缩放/適寬改 img 内联尺寸 → 批注层跟随重排
    new MutationObserver(function () { relayout(); })
      .observe(img, { attributes: true, attributeFilter: ['style', 'width'] });
    bindTools();
    bindDraw(wrap);
    loadItems();
    renderOcrCard();
  }

  function relayout() {
    if (!img || !img.clientWidth) return;
    svg.setAttribute('width', img.clientWidth);
    svg.setAttribute('height', img.clientHeight);
    svg.setAttribute('viewBox', '0 0 ' + img.clientWidth + ' ' + img.clientHeight);
    renderBoxes();
  }

  // ── 工具条 ───────────────────────────────
  function bindTools() {
    document.querySelectorAll('.rt-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        var t = b.dataset.tool;
        setTool(state.tool === t ? null : t);
      });
    });
    document.querySelectorAll('.rt-color').forEach(function (c) {
      c.addEventListener('click', function () {
        state.color = c.dataset.color;
        document.querySelectorAll('.rt-color').forEach(function (x) {
          x.classList.toggle('on', x === c);
        });
      });
    });
    // 与框选重OCR模式互斥
    if (typeof window.toggleReocrMode === 'function') {
      var _orig = window.toggleReocrMode;
      window.toggleReocrMode = function () {
        setTool(null);
        return _orig.apply(this, arguments);
      };
    }
  }

  function setTool(t) {
    state.tool = t;
    document.querySelectorAll('.rt-btn').forEach(function (b) {
      b.classList.toggle('on', b.dataset.tool === t);
    });
    if (t && typeof window.exitReocrMode === 'function') {
      try { window.exitReocrMode(); } catch (e) {}
    }
    var cursor = t === 'pen' ? 'crosshair' : (t ? 'crosshair' : '');
    if (layer) layer.style.cursor = cursor;
    layer && layer.classList.toggle('interactive', !!t);
  }
  window.__readSetTool = setTool;

  // ── 画布: 拖矩形 / 画笔 ───────────────────
  function bindDraw(wrap) {
    var drawing = false, kind = null, sx = 0, sy = 0, temp = null, pts = [];
    layer.addEventListener('pointerdown', function (e) {
      if (!state.tool || e.button !== 0) return;
      e.preventDefault();
      drawing = true; kind = state.tool; pts = [];
      var r = img.getBoundingClientRect();
      sx = e.clientX - r.left; sy = e.clientY - r.top;
      if (kind === 'pen') {
        temp = document.createElementNS(svgNS, 'polyline');
        temp.setAttribute('fill', 'none');
        temp.setAttribute('stroke', state.color);
        temp.setAttribute('stroke-width', '2.5');
        temp.setAttribute('stroke-linejoin', 'round');
        temp.setAttribute('stroke-linecap', 'round');
        temp.setAttribute('opacity', '0.85');
        svg.appendChild(temp);
        pts.push([sx, sy]);
      } else {
        temp = document.createElementNS(svgNS, 'rect');
        temp.setAttribute('stroke', state.color);
        temp.setAttribute('stroke-width', '2');
        temp.setAttribute('fill', kind === 'highlight' ? state.color : 'none');
        temp.setAttribute('fill-opacity', kind === 'highlight' ? '0.18' : '0');
        temp.setAttribute('stroke-dasharray', '4 3');
        svg.appendChild(temp);
      }
      layer.setPointerCapture && layer.setPointerCapture(e.pointerId);
    });
    layer.addEventListener('pointermove', function (e) {
      if (!drawing) return;
      var r = img.getBoundingClientRect();
      var x = Math.max(0, Math.min(e.clientX - r.left, r.width));
      var y = Math.max(0, Math.min(e.clientY - r.top, r.height));
      if (kind === 'pen') {
        var lp = pts[pts.length - 1];
        if (!lp || Math.abs(x - lp[0]) + Math.abs(y - lp[1]) > 3) {
          pts.push([x, y]);
          temp.setAttribute('points', pts.map(function (p) {
            return p[0] + ',' + p[1];
          }).join(' '));
        }
      } else {
        var x0 = Math.min(sx, x), y0 = Math.min(sy, y);
        var w = Math.abs(x - sx), h = Math.abs(y - sy);
        temp.setAttribute('x', x0); temp.setAttribute('y', y0);
        temp.setAttribute('width', w); temp.setAttribute('height', h);
      }
    });
    function finish(e) {
      if (!drawing) return;
      drawing = false;
      var r = img.getBoundingClientRect();
      if (kind === 'pen') {
        if (pts.length >= 2) {
          var norm = pts.map(function (p) {
            return { x: +(p[0] / r.width).toFixed(4),
                     y: +(p[1] / r.height).toFixed(4) };
          });
          save(kind, norm);
        }
      } else {
        var x = +temp.getAttribute('x'), y = +temp.getAttribute('y');
        var w = +temp.getAttribute('width'), h = +temp.getAttribute('height');
        if (w >= 10 && h >= 10) {
          save(kind, { x: +(x / r.width).toFixed(4), y: +(y / r.height).toFixed(4),
                       w: +(w / r.width).toFixed(4), h: +(h / r.height).toFixed(4) });
        }
      }
      if (temp && temp.parentNode) temp.parentNode.removeChild(temp);
      temp = null;
    }
    layer.addEventListener('pointerup', finish);
    layer.addEventListener('pointercancel', finish);
  }

  // ── 持久化 ───────────────────────────────
  function save(kind, coords, extra, cb) {
    var body = new URLSearchParams();
    body.append('page_num', page);
    body.append('kind', kind);
    body.append('coords', JSON.stringify(coords));
    body.append('color', state.color);
    if (extra) {
      if (extra.note) body.append('note', extra.note);
      if (extra.origin) body.append('origin', extra.origin);
    }
    fetch(api('/read/annotations'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { alert('批注保存失败: ' + d.error); return; }
      state.items.push(d.item);
      renderBoxes(); renderCards();
      if (kind === 'rect' && !extra) showBubble(d.item);
      if (cb) cb(d.item);
    }).catch(function (e) { alert('批注保存失败: ' + e.message); });
  }
  window.__readSave = save;

  function patch(aid, fields) {
    var body = new URLSearchParams();
    if (fields.note !== undefined) body.append('note', fields.note);
    if (fields.color !== undefined) body.append('color', fields.color);
    return fetch(api('/read/annotations/' + aid), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    }).then(function (r) { return r.json(); });
  }

  function del(aid) {
    if (!confirm('删除这条批注？')) return;
    fetch(api('/read/annotations/' + aid), { method: 'DELETE' })
      .then(function (r) { return r.json(); }).then(function (d) {
        state.items = state.items.filter(function (x) { return x.id !== aid; });
        renderBoxes(); renderCards();
      });
  }

  function loadItems() {
    fetch(api('/read/annotations?page=' + page))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        state.items = d.items || [];
        renderBoxes(); renderCards();
      });
  }
  window.__readReload = loadItems;

  // ── overlay 渲染 ─────────────────────────
  function renderBoxes() {
    if (!svg || !img || !img.clientWidth) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    state.items.forEach(function (it) {
      var el;
      if (it.kind === 'pen') {
        el = document.createElementNS(svgNS, 'polyline');
        el.setAttribute('points', (it.coords || []).map(function (p) {
          return (p.x * img.clientWidth) + ',' + (p.y * img.clientHeight);
        }).join(' '));
        el.setAttribute('fill', 'none');
        el.setAttribute('stroke', it.color || COLORS[0]);
        el.setAttribute('stroke-width', '2.5');
        el.setAttribute('stroke-linecap', 'round');
        el.setAttribute('opacity', '0.85');
      } else {
        el = document.createElementNS(svgNS, 'rect');
        el.setAttribute('x', (it.coords.x || 0) * img.clientWidth);
        el.setAttribute('y', (it.coords.y || 0) * img.clientHeight);
        el.setAttribute('width', (it.coords.w || 0) * img.clientWidth);
        el.setAttribute('height', (it.coords.h || 0) * img.clientHeight);
        var fill = it.kind === 'highlight' ? (it.color || COLORS[0]) : 'none';
        el.setAttribute('fill', fill);
        el.setAttribute('fill-opacity', it.kind === 'highlight' ? 0.16 : 0);
        el.setAttribute('stroke', it.color || COLORS[0]);
        el.setAttribute('stroke-width', it.kind === 'highlight' ? 1 : 1.8);
      }
      el.setAttribute('class', 'read-ann-box');
      el.setAttribute('data-aid', it.id);
      el.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var card = document.querySelector('.rc-card[data-aid="' + it.id + '"]');
        if (card) {
          card.scrollIntoView({ behavior: 'smooth', block: 'center' });
          card.style.transition = 'background .3s';
          card.style.background = '#fef3c7';
          setTimeout(function () { card.style.background = ''; }, 900);
        }
      });
      svg.appendChild(el);
    });
  }

  // ── 框选气泡 ─────────────────────────────
  function showBubble(item) {
    dismissBubble();
    var b = document.createElement('div');
    b.className = 'read-bubble';
    b.id = 'readBubble';
    b.innerHTML =
      '<button class="pri" data-op="ask">问 AI</button>' +
      '<button data-op="note">批注</button>' +
      '<button data-op="loc">定位文本块</button>' +
      '<button data-op="x" style="border:none;background:none;color:var(--text-muted)">×</button>';
    img.parentNode.appendChild(b);
    var ix = (item.coords.x + item.coords.w) * img.clientWidth;
    var iy = (item.coords.y) * img.clientHeight - 34;
    b.style.left = Math.max(2, Math.min(ix - 60, img.clientWidth - 230)) + 'px';
    b.style.top = Math.max(2, iy) + 'px';
    b.addEventListener('click', function (ev) {
      var op = ev.target.dataset && ev.target.dataset.op;
      if (!op) return;
      if (op === 'ask') {
        state.pendingCtx = { page: page, rect: item.coords };
        window.__readChatOpen && window.__readChatOpen();
      } else if (op === 'note') {
        var ta = document.querySelector('.rc-card[data-aid="' + item.id + '"] textarea');
        if (ta) { ta.focus(); ta.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
      } else if (op === 'loc') {
        locateBlocks(item.coords);
      }
      dismissBubble();
    });
    setTimeout(function () {
      document.addEventListener('pointerdown', outside);
    }, 50);
    function outside(ev) {
      if (!b.contains(ev.target)) { dismissBubble(); }
    }
    window.__readDismissBubble = function () {
      document.removeEventListener('pointerdown', outside);
      if (b.parentNode) b.parentNode.removeChild(b);
    };
  }
  function dismissBubble() {
    window.__readDismissBubble && window.__readDismissBubble();
  }

  // ── 定位文本块 (选区交 bbox, 闪黄滚动) ─────
  function locateBlocks(rect) {
    if (!origWidth || !origHeight) {
      toast('本页无块坐标 (原生 PDF), 请直接在文本列查看');
      return;
    }
    var rx0 = rect.x * origWidth, ry0 = rect.y * origHeight;
    var rx1 = (rect.x + rect.w) * origWidth, ry1 = (rect.y + rect.h) * origHeight;
    var hits = [];
    document.querySelectorAll('.bbox-overlay').forEach(function (el) {
      var x0 = +el.dataset.x0, y0 = +el.dataset.y0;
      var x1 = +el.dataset.x1, y1 = +el.dataset.y1;
      var ix = Math.max(0, Math.min(x1, rx1) - Math.max(x0, rx0));
      var iy = Math.max(0, Math.min(y1, ry1) - Math.max(y0, ry0));
      var barea = Math.max((x1 - x0) * (y1 - y0), 1e-9);
      if (ix * iy / barea >= 0.3) hits.push(el);
    });
    if (!hits.length) { toast('选区内没有文本块'); return; }
    hits.forEach(function (el) {
      el.classList.add('rc-locating');
      setTimeout(function () { el.classList.remove('rc-locating'); }, 2600);
    });
    hits[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function toast(msg) {
    var t = document.createElement('div');
    t.textContent = msg;
    t.style.cssText = 'position:fixed;z-index:400;left:50%;bottom:60px;' +
      'transform:translateX(-50%);background:#111;color:#fff;padding:6px 14px;' +
      'border-radius:6px;font-size:.78rem;opacity:.92';
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 2200);
  }
  window.__readToast = toast;

  // ── 本页卡片流 ───────────────────────────
  function renderCards() {
    var box = document.getElementById('readCardsBody');
    if (!box) return;
    var ocr = readOcrData();
    var list = state.items.slice();
    var html = '';
    if (ocr) {
      html += '<div class="rc-card ocr" id="rcOcr">' +
        '<div class="rc-meta"><span class="rc-badge">OCR</span>' +
        '<span>第 ' + ocr.page + ' 页 · 重OCR ' + ocr.blocks + ' 块</span></div>' +
        '<div class="rc-clip">' + esc((ocr.texts || []).join(' / ')) + '</div>' +
        '<div class="rc-ops"><button onclick="sessionStorage.removeItem(\'read_ocr_card\');document.getElementById(\'rcOcr\').remove()">忽略</button></div>' +
        '</div>';
    }
    var shown = list.filter(function (it) {
      return state.filter === 'all' || state.filter === 'ann';
    });
    if (!shown.length && !ocr) {
      html = '<div class="rc-empty">本页暂无批注——工具条选「框选批注 / 画笔 / 高亮」在图上勾画</div>';
    }
    shown.forEach(function (it) { html += cardHtml(it); });
    box.innerHTML = html;
    bindCardEvents(box);
  }

  function cardHtml(it) {
    var clip = it.clip_text
      ? '<div class="rc-clip" data-aid="' + it.id + '" title="点击在图上高亮">' + esc(it.clip_text) + '</div>'
      : '';
    return '<div class="rc-card" data-aid="' + it.id + '" style="--rc-color:' +
      (it.color || COLORS[0]) + '">' +
      '<div class="rc-meta"><span>' +
      (it.kind === 'pen' ? '画笔' : it.kind === 'highlight' ? '高亮' : '框选') +
      '</span>' + (it.origin === 'ai' ? '<span class="rc-badge ai">AI</span>' : '') +
      '<span style="margin-left:auto;font-size:.68rem">#' + it.id + '</span></div>' +
      clip +
      '<textarea placeholder="批注文字…">' + esc(it.note || '') + '</textarea>' +
      '<div class="rc-ops">' +
      (it.kind === 'rect' ? '<button data-op="ask" data-aid="' + it.id + '">问 AI</button>' : '') +
      '<button data-op="del" data-aid="' + it.id + '" class="del">删除</button>' +
      '</div></div>';
  }

  function bindCardEvents(box) {
    box.querySelectorAll('textarea').forEach(function (ta) {
      var timer = null;
      ta.addEventListener('input', function () {
        var aid = +ta.closest('.rc-card').dataset.aid;
        clearTimeout(timer);
        timer = setTimeout(function () {
          patch(aid, { note: ta.value }).then(function (d) {
            if (d.item) {
              var it = state.items.find(function (x) { return x.id === aid; });
              if (it) it.note = d.item.note;
            }
          });
        }, 600);
      });
    });
    box.querySelectorAll('.rc-ops button').forEach(function (b) {
      b.addEventListener('click', function () {
        var aid = +b.dataset.aid;
        var it = state.items.find(function (x) { return x.id === aid; });
        if (b.dataset.op === 'del') { del(aid); return; }
        if (b.dataset.op === 'ask' && it) {
          state.pendingCtx = { page: page, rect: it.coords };
          window.__readChatOpen && window.__readChatOpen();
        }
      });
    });
    box.querySelectorAll('.rc-clip').forEach(function (c) {
      c.addEventListener('click', function () {
        var it = state.items.find(function (x) { return x.id === +c.dataset.aid; });
        if (it && it.kind !== 'pen') locateBlocks(it.coords);
      });
    });
  }

  // 卡片流头 (筛选 + 检索)
  document.addEventListener('DOMContentLoaded', function () {
    var head = document.getElementById('readCardsHead');
    if (!head) return;
    head.querySelectorAll('.rc-chips button').forEach(function (b) {
      b.addEventListener('click', function () {
        state.filter = b.dataset.f;
        head.querySelectorAll('.rc-chips button').forEach(function (x) {
          x.classList.toggle('on', x === b);
        });
        renderCards();
      });
    });
    var si = document.getElementById('rcSearch');
    var timer = null;
    si.addEventListener('input', function () {
      clearTimeout(timer);
      var q = si.value.trim();
      var hitBox = document.getElementById('rcHits');
      if (!q) { hitBox.innerHTML = ''; return; }
      timer = setTimeout(function () {
        fetch('/collections/' + encodeURIComponent(collection) +
              '/read/annotations/search?q=' + encodeURIComponent(q))
          .then(function (r) { return r.json(); })
          .then(function (d) {
            var items = d.items || [];
            hitBox.innerHTML = items.length ? items.map(function (it, i) {
              return '<div class="rc-hit" data-i="' + i + '">' +
                '<b>' + esc(it.title || ('文档#' + it.doc_id)) + '</b>' +
                ' <span class="rc-hit-meta">第 ' + it.page_num + ' 页</span>' +
                '<div class="rc-hit-meta">' +
                esc(((it.note || '') + ' — ' + (it.clip_text || '')).slice(0, 90)) +
                '</div></div>';
            }).join('') : '<div class="rc-empty">无命中</div>';
            hitBox.querySelectorAll('.rc-hit').forEach(function (h) {
              h.addEventListener('click', function () {
                var it = items[+h.dataset.i];
                if (it.doc_id === docId) {
                  location.href = location.pathname + '?page=' + it.page_num;
                } else {
                  location.href = '/collections/' + encodeURIComponent(collection) +
                    '/doc/' + it.doc_id + '/proofread?page=' + it.page_num;
                }
              });
            });
          });
      }, 350);
    });
  });

  // ── OCR 瞬态卡 (sessionStorage 跨页面重载) ──
  function readOcrData() {
    try {
      var d = JSON.parse(sessionStorage.getItem('read_ocr_card') || 'null');
      if (d && d.docId === docId && d.page === page) return d;
    } catch (e) {}
    return null;
  }
  window.__readOcrCard = function (data) {
    try {
      sessionStorage.setItem('read_ocr_card', JSON.stringify({
        docId: docId, page: page,
        blocks: data.new_blocks, texts: data.texts || [],
        at: Date.now(),
      }));
    } catch (e) {}
  };
  function renderOcrCard() { /* renderCards 内统一渲染 */ }
})();
