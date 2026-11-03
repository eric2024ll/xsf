/* 细读·批注层 + 本页卡片流 (2026-11-03)
 * 依赖页面全局: collection / docId / page / pageCount / origWidth / origHeight
 * window.__READ = {enabled, vision:[{id,name,model}]}
 * 坐标: 存归一化 0-1; 渲染乘 img 显示尺寸。
 * 事件层: mouse + touch 双轨, 不依赖 pointer events (国产浏览器兼容模式/
 * IE 内核无 pointerdown, 2026-11-03 用户实测画不出框的根因)。
 * 交互: 画(框/笔/高亮) / 选中调整(拖动移动, 四角拉伸) / 删除(✕ 或 Delete 键)。
 */
(function () {
  'use strict';
  if (!window.__READ || !window.__READ.enabled) return;

  var COLORS = ['#e11d48', '#2563eb', '#16a34a', '#d97706', '#7c3aed'];
  var HANDLE_R = 6;      // 角柄命中半径 (px)
  var MIN_SIZE = 8;      // 框最小像素
  var state = {
    tool: null,          // 'rect' | 'pen' | 'highlight' | null
    color: COLORS[0],
    items: [],
    filter: 'all',
    pendingCtx: null,    // {page, rect} 问 AI 待带选区
    selected: null,      // 选中的批注 id
  };
  window.__readState = state;

  var svgNS = 'http://www.w3.org/2000/svg';
  var img, layer, svg;
  var selBtn = null;     // 选中态 ✕ 删除钮 (HTML)

  function api(path) {
    return '/collections/' + encodeURIComponent(collection) +
      '/doc/' + docId + path;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function toNorm(x, y) {
    var r = img.getBoundingClientRect();
    return { x: Math.min(Math.max(x - r.left, 0), r.width) / r.width,
             y: Math.min(Math.max(y - r.top, 0), r.height) / r.height };
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
    bindCanvas(wrap);
    loadItems();
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
    if (t) select(null);   // 画图与选中调整互斥
    document.querySelectorAll('.rt-btn').forEach(function (b) {
      b.classList.toggle('on', b.dataset.tool === t);
    });
    if (t && typeof window.exitReocrMode === 'function') {
      try { window.exitReocrMode(); } catch (e) {}
    }
    if (img && img.parentNode) img.parentNode.style.cursor = t ? 'crosshair' : '';
    // 工具激活时避免拖画带出文本选择
    document.body.style.userSelect = t ? 'none' : '';
  }
  window.__readSetTool = setTool;

  // ── 画布: wrap 捕获阶段按需拦截 (mouse + touch 双轨) ──
  // layer 本身 pointer-events:none (纯视觉); 事件挂 image-wrap 捕获阶段:
  //   工具激活 / 命中批注(选中/手柄) → 截下 (平移不启动)
  //   否则放行 → 拖拽平移、bbox hover 等原有交互不受影响
  // 不依赖 pointer events: 国产浏览器兼容模式/IE 内核无之 (2026-11-03 实测)。
  function bindCanvas(wrap) {
    function gate(e, cx, cy) {
      if (state.tool) {
        e.preventDefault(); e.stopPropagation();
        onDown(cx, cy);
        return;
      }
      var hit = hitHandle(cx, cy) || hitItem(cx, cy);
      if (hit) {
        e.preventDefault(); e.stopPropagation();
        onDown(cx, cy);
      }
      // miss: 放行 (平移/其他)
    }
    wrap.addEventListener('mousedown', function (e) {
      if (e.button !== 0) return;
      gate(e, e.clientX, e.clientY);
    }, true);
    wrap.addEventListener('touchstart', function (e) {
      if (e.touches.length !== 1) return;
      var t = e.touches[0];
      gate(e, t.clientX, t.clientY);
    }, { passive: false, capture: true });

    document.addEventListener('mousemove', function (e) { onMove(e.clientX, e.clientY); });
    document.addEventListener('mouseup', function () { onUp(); });
    document.addEventListener('touchmove', function (e) {
      if (!drag || e.touches.length !== 1) return;
      e.preventDefault();
      var t = e.touches[0];
      onMove(t.clientX, t.clientY);
    }, { passive: false });
    document.addEventListener('touchend', function () { onUp(); });

    document.addEventListener('keydown', function (e) {
      if ((e.key === 'Delete' || e.key === 'Backspace') && state.selected) {
        var active = document.activeElement;
        if (active && /INPUT|TEXTAREA/.test(active.tagName)) return;
        e.preventDefault();
        del(state.selected);
      }
      if (e.key === 'Escape') select(null);
    });
  }

  // drag: {mode:'draw'|'move'|'resize', kind, sx, sy, temp, pts, item, handle}
  var drag = null;
  window.__dbg = {down:0, move:0, up:0};

  function onDown(cx, cy) {
    dismissBubble();
    var pt = toNorm(cx, cy);
    if (state.tool) {
      // ── 画 ──
      drag = { mode: 'draw', kind: state.tool, sx: cx, sy: cy, temp: null, pts: [] };
      if (state.tool === 'pen') {
        drag.temp = document.createElementNS(svgNS, 'polyline');
        drag.temp.setAttribute('fill', 'none');
        drag.temp.setAttribute('stroke', state.color);
        drag.temp.setAttribute('stroke-width', '2.5');
        drag.temp.setAttribute('stroke-linejoin', 'round');
        drag.temp.setAttribute('stroke-linecap', 'round');
        drag.temp.setAttribute('opacity', '0.85');
        svg.appendChild(drag.temp);
        drag.pts.push([cx, cy]);
      } else {
        drag.temp = document.createElementNS(svgNS, 'rect');
        drag.temp.setAttribute('stroke', state.color);
        drag.temp.setAttribute('stroke-width', '2');
        drag.temp.setAttribute('fill', state.tool === 'highlight' ? state.color : 'none');
        drag.temp.setAttribute('fill-opacity', state.tool === 'highlight' ? 0.18 : 0);
        drag.temp.setAttribute('stroke-dasharray', '4 3');
        svg.appendChild(drag.temp);
      }
      return;
    }
    // ── 选中/调整 (无工具时) ──
    var hit = hitHandle(cx, cy) || hitItem(cx, cy);
    if (!hit) { select(null); return; }
    select(hit.item.id, true);   // 立即出选中态 (手柄)
    if (hit.handle) {
      drag = { mode: 'resize', item: hit.item, handle: hit.handle,
               orig: JSON.parse(JSON.stringify(hit.item.coords)) };
    } else {
      drag = { mode: 'move', item: hit.item, sx: cx, sy: cy,
               orig: JSON.parse(JSON.stringify(hit.item.coords)) };
    }
  }

  function onMove(cx, cy) {
    if (!drag) return;
    if (drag.mode === 'draw') {
      var r = img.getBoundingClientRect();
      var x = Math.max(0, Math.min(cx - r.left, r.width));
      var y = Math.max(0, Math.min(cy - r.top, r.height));
      if (drag.kind === 'pen') {
        var lp = drag.pts[drag.pts.length - 1];
        if (!lp || Math.abs(x - lp[0]) + Math.abs(y - lp[1]) > 3) {
          drag.pts.push([x, y]);
          drag.temp.setAttribute('points', drag.pts.map(function (p) {
            return p[0] + ',' + p[1];
          }).join(' '));
        }
      } else {
        var x0 = Math.min(drag.sx - r.left, x), y0 = Math.min(drag.sy - r.top, y);
        var w = Math.abs(x - (drag.sx - r.left)), h = Math.abs(y - (drag.sy - r.top));
        drag.temp.setAttribute('x', x0); drag.temp.setAttribute('y', y0);
        drag.temp.setAttribute('width', w); drag.temp.setAttribute('height', h);
      }
      return;
    }
    var r2 = img.getBoundingClientRect();
    if (drag.mode === 'move') {
      var dx = (cx - drag.sx) / r2.width, dy = (cy - drag.sy) / r2.height;
      var o = drag.orig;
      if (drag.item.kind === 'pen') {
        drag.item.coords = o.map(function (p) {
          return { x: +(p.x + dx).toFixed(4), y: +(p.y + dy).toFixed(4) };
        });
      } else {
        drag.item.coords = { x: +(o.x + dx).toFixed(4), y: +(o.y + dy).toFixed(4),
                             w: o.w, h: o.h };
      }
      renderBoxes();
    } else if (drag.mode === 'resize') {
      var pt = toNorm(cx, cy);
      var o2 = drag.orig, H = drag.handle;
      var x1 = o2.x, y1 = o2.y, x2 = o2.x + o2.w, y2 = o2.y + o2.h;
      if (H.indexOf('n') >= 0) y1 = Math.min(pt.y, y2 - 0.005);
      if (H.indexOf('s') >= 0) y2 = Math.max(pt.y, y1 + 0.005);
      if (H.indexOf('w') >= 0) x1 = Math.min(pt.x, x2 - 0.005);
      if (H.indexOf('e') >= 0) x2 = Math.max(pt.x, x1 + 0.005);
      drag.item.coords = { x: +x1.toFixed(4), y: +y1.toFixed(4),
                           w: +(x2 - x1).toFixed(4), h: +(y2 - y1).toFixed(4) };
      renderBoxes();
    }
  }

  function onUp() {
    if (!drag) return;
    var d = drag; drag = null;
    if (d.mode === 'draw') {
      var r = img.getBoundingClientRect();
      if (d.kind === 'pen') {
        if (d.pts.length >= 2) {
          var norm = d.pts.map(function (p) {
            return { x: +(p[0] / r.width).toFixed(4), y: +(p[1] / r.height).toFixed(4) };
          });
          save(d.kind, norm);
        }
      } else {
        var x = +d.temp.getAttribute('x'), y = +d.temp.getAttribute('y');
        var w = +d.temp.getAttribute('width'), h = +d.temp.getAttribute('height');
        if (w >= 10 && h >= 10) {
          save(d.kind, { x: +(x / r.width).toFixed(4), y: +(y / r.height).toFixed(4),
                         w: +(w / r.width).toFixed(4), h: +(h / r.height).toFixed(4) });
        }
      }
      if (d.temp && d.temp.parentNode) d.temp.parentNode.removeChild(d.temp);
    } else {
      // move/resize 落库
      patch(d.item.id, { coords: d.item.coords });
    }
  }

  // ── 命中检测 ─────────────────────────────
  function itemPx(it) {
    var r = img.getBoundingClientRect();
    if (it.kind === 'pen') {
      var xs = it.coords.map(function (p) { return p.x * r.width; });
      var ys = it.coords.map(function (p) { return p.y * r.height; });
      return { x: Math.min.apply(null, xs), y: Math.min.apply(null, ys),
               w: Math.max.apply(null, xs) - Math.min.apply(null, xs),
               h: Math.max.apply(null, ys) - Math.min.apply(null, ys) };
    }
    return { x: (it.coords.x || 0) * r.width, y: (it.coords.y || 0) * r.height,
             w: (it.coords.w || 0) * r.width, h: (it.coords.h || 0) * r.height };
  }

  function hitItem(cx, cy) {
    var r = img.getBoundingClientRect();
    var lx = cx - r.left, ly = cy - r.top;
    var found = null;
    state.items.forEach(function (it) {
      if (it.kind === 'highlight') return;   // 高亮不挡鼠标, 点穿到下层框
      var b = itemPx(it);
      // 笔迹加粗命中带
      var pad = it.kind === 'pen' ? 6 : 2;
      if (lx >= b.x - pad && lx <= b.x + b.w + pad &&
          ly >= b.y - pad && ly <= b.y + b.h + pad) {
        if (!found || it.id > found.id) found = it;   // 取最上层
      }
    });
    return found ? { item: found } : null;
  }

  function hitHandle(cx, cy) {
    if (!state.selected) return null;
    var it = state.items.find(function (x) { return x.id === state.selected; });
    if (!it || it.kind === 'pen') return null;
    var r = img.getBoundingClientRect();
    var lx = cx - r.left, ly = cy - r.top;
    var b = itemPx(it);
    var corners = { nw: [b.x, b.y], ne: [b.x + b.w, b.y],
                    sw: [b.x, b.y + b.h], se: [b.x + b.w, b.y + b.h] };
    for (var k in corners) {
      var c = corners[k];
      if (Math.abs(lx - c[0]) <= HANDLE_R + 2 && Math.abs(ly - c[1]) <= HANDLE_R + 2) {
        return { item: it, handle: k };
      }
    }
    return null;
  }

  // ── 选中态 ───────────────────────────────
  function select(aid, keepDrag) {
    state.selected = aid;
    if (selBtn) { selBtn.remove(); selBtn = null; }
    renderBoxes();
    if (aid == null) return;
    var it = state.items.find(function (x) { return x.id === aid; });
    if (!it) return;
    // ✕ 删除钮挂框右上
    var b = itemPx(it);
    selBtn = document.createElement('button');
    selBtn.className = 'read-del-btn';
    selBtn.textContent = '×';
    selBtn.title = '删除该批注 (Delete)';
    selBtn.style.left = (b.x + b.w + 4) + 'px';
    selBtn.style.top = (b.y - 10) + 'px';
    selBtn.addEventListener('mousedown', function (e) {
      e.stopPropagation(); e.preventDefault();
    });
    selBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      del(aid);
    });
    layer.appendChild(selBtn);
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
    if (fields.coords !== undefined) body.append('coords', JSON.stringify(fields.coords));
    return fetch(api('/read/annotations/' + aid), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.item) {
        var it = state.items.find(function (x) { return x.id === aid; });
        if (it) {
          it.note = d.item.note;
          it.coords = d.item.coords;
          it.color = d.item.color;
        }
      }
      renderCards();
      return d;
    });
  }

  function del(aid) {
    fetch(api('/read/annotations/' + aid), { method: 'DELETE' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        state.items = state.items.filter(function (x) { return x.id !== aid; });
        if (state.selected === aid) select(null);
        renderBoxes(); renderCards();
        toast('批注已删除');
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
        el.setAttribute('stroke-width', state.selected === it.id ? 4 : 2.5);
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
        el.setAttribute('stroke-width', state.selected === it.id ? 3 : (it.kind === 'highlight' ? 1 : 1.8));
      }
      el.setAttribute('data-aid', it.id);
      svg.appendChild(el);
      // 选中态: 四角手柄 (笔迹无手柄)
      if (state.selected === it.id && it.kind !== 'pen') {
        ['nw', 'ne', 'sw', 'se'].forEach(function (h) {
          var b = itemPx(it);
          var cx2 = (h.indexOf('w') >= 0 ? b.x : b.x + b.w);
          var cy2 = (h.indexOf('n') >= 0 ? b.y : b.y + b.h);
          var hd = document.createElementNS(svgNS, 'circle');
          hd.setAttribute('cx', cx2); hd.setAttribute('cy', cy2);
          hd.setAttribute('r', HANDLE_R);
          hd.setAttribute('class', 'read-handle');
          hd.setAttribute('data-h', h);
          svg.appendChild(hd);
        });
      }
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
    layer.appendChild(b);
    var ix = (item.coords.x + item.coords.w) * img.clientWidth;
    var iy = (item.coords.y) * img.clientHeight - 34;
    b.style.left = Math.max(2, Math.min(ix - 60, layer.clientWidth - 240)) + 'px';
    b.style.top = Math.max(2, iy) + 'px';
    b.addEventListener('mousedown', function (e) { e.stopPropagation(); });
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
      document.addEventListener('mousedown', outside);
    }, 50);
    function outside(ev) {
      if (!b.contains(ev.target)) { dismissBubble(); }
    }
    window.__readDismissBubble = function () {
      document.removeEventListener('mousedown', outside);
      if (b.parentNode) b.parentNode.removeChild(b);
    };
  }
  function dismissBubble() {
    window.__readDismissBubble && window.__readDismissBubble();
    window.__readDismissBubble = null;
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
      html = '<div class="rc-empty">本页暂无批注——工具条选「框选批注 / 画笔 / 高亮」在图上勾画; 无工具时点批注可选中调整</div>';
    }
    shown.forEach(function (it) { html += cardHtml(it); });
    box.innerHTML = html;
    bindCardEvents(box);
  }

  function cardHtml(it) {
    var clip = it.clip_text
      ? '<div class="rc-clip" data-aid="' + it.id + '" title="点击在图上选中">' + esc(it.clip_text) + '</div>'
      : '';
    return '<div class="rc-card' + (state.selected === it.id ? ' rc-card-sel' : '') +
      '" data-aid="' + it.id + '" style="--rc-color:' +
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
        timer = setTimeout(function () { patch(aid, { note: ta.value }); }, 600);
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
        if (it) {
          select(it.id);
          if (it.kind !== 'pen' && it.kind !== 'highlight') locateBlocks(it.coords);
        }
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
})();
