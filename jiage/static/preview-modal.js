/* ── 预览模态框共享 JS ──
   用法:
     pmOpen(collection, docId, opts)
       opts.pageNum / opts.blockNum  — 命中点 (有则用 context API radius=3)
       opts.highlightTerms           — 高亮词数组
       opts.title                    — 自定义标题 (可选)
     pmClose()
   依赖: 页面已 include _preview_modal.html
*/
function _pmEsc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}

function _pmHighlight(text, terms){
  if(!text) return '';
  var html = _pmEsc(text);
  if(terms && terms.length){
    for(var i=0;i<terms.length;i++){
      var term = terms[i];
      if(!term) continue;
      var escaped = term.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
      html = html.replace(new RegExp(escaped,'g'),'<mark>'+term+'</mark>');
    }
  }
  return html;
}

function _pmRenderLines(lines, terms){
  /* 按 block_num 分组渲染 */
  var byBlock = {};
  var order = [];
  for(var i=0;i<lines.length;i++){
    var ln = lines[i];
    var bk = ln.block_num;
    if(!(bk in byBlock)){byBlock[bk]={label:ln.block_label||'',lines:[]};order.push(bk);}
    byBlock[bk].lines.push(ln.text);
  }
  var h = '';
  for(var j=0;j<order.length;j++){
    var bl = byBlock[order[j]];
    h += '<div class="pv-block">';
    if(bl.label) h += '<div class="pv-block-label">'+_pmEsc(bl.label)+'</div>';
    for(var k=0;k<bl.lines.length;k++){
      h += '<div class="pv-line">'+_pmHighlight(bl.lines[k], terms)+'</div>';
    }
    h += '</div>';
  }
  return h || '<div class="preview-loading">无内容</div>';
}

function _pmRenderPages(pages, terms){
  var h = '';
  for(var i=0;i<pages.length;i++){
    var pg = pages[i];
    h += '<div class="pv-page"><div class="pv-page-num">第 '+pg.page_num+' 页</div>';
    var blocks = pg.blocks || [];
    for(var j=0;j<blocks.length;j++){
      var bl = blocks[j];
      h += '<div class="pv-block">';
      if(bl.block_label) h += '<div class="pv-block-label">'+_pmEsc(bl.block_label)+'</div>';
      var lns = bl.lines || [];
      for(var k=0;k<lns.length;k++){
        h += '<div class="pv-line">'+_pmHighlight(lns[k].text, terms)+'</div>';
      }
      h += '</div>';
    }
    h += '</div>';
  }
  return h || '<div class="preview-loading">无内容</div>';
}

async function pmOpen(collection, docId, opts){
  opts = opts || {};
  var overlay = document.getElementById('pmOverlay');
  if(!overlay) return;
  var titleEl = document.getElementById('pmTitle');
  var metaEl = document.getElementById('pmMeta');
  var bodyEl = document.getElementById('pmBody');
  overlay.classList.add('active');
  titleEl.textContent = opts.title || '加载中…';
  metaEl.innerHTML = '';
  bodyEl.innerHTML = '<div class="preview-loading">加载中…</div>';
  var collEnc = encodeURIComponent(collection);
  try{
    if(opts.pageNum && opts.blockNum){
      /* 命中上下文模式: context API radius=3 */
      var ctxUrl = '/collections/'+collEnc+'/context/'+docId+'/'+opts.pageNum+'/'+opts.blockNum+'?radius=3';
      var res = await fetch(ctxUrl);
      var data = await res.json();
      var lines = data.lines || data || [];
      if(!Array.isArray(lines)) lines = [];
      /* 取 doc 元信息 (从 meta API 或用 opts) */
      if(opts.title) titleEl.textContent = opts.title;
      else if(data.doc && data.doc.title) titleEl.textContent = data.doc.title;
      var metaParts = [];
      if(data.doc){
        if(data.doc.author) metaParts.push('作者: '+_pmEsc(data.doc.author));
        if(data.doc.cite_key) metaParts.push('['+_pmEsc(data.doc.cite_key)+']');
      }
      metaEl.innerHTML = metaParts.join(' · ');
      bodyEl.innerHTML = _pmRenderLines(lines, opts.highlightTerms);
    } else {
      /* 文档开头模式: content API limit=10 */
      var contentUrl = '/collections/'+collEnc+'/doc/'+docId+'/content?limit=10';
      var res2 = await fetch(contentUrl);
      var data2 = await res2.json();
      var doc = data2.doc || {};
      titleEl.textContent = doc.title || doc.filename || '(无标题)';
      var metaParts2 = [];
      if(doc.author) metaParts2.push('作者: '+_pmEsc(doc.author));
      if(doc.cite_key) metaParts2.push('['+_pmEsc(doc.cite_key)+']');
      if(doc.page_count) metaParts2.push(doc.page_count+' 页');
      if(doc.bib_type) metaParts2.push(_pmEsc(doc.bib_type));
      metaEl.innerHTML = metaParts2.join(' · ');
      bodyEl.innerHTML = _pmRenderPages(data2.pages || [], opts.highlightTerms);
    }
  }catch(e){
    bodyEl.innerHTML = '<div class="preview-loading">加载失败: '+_pmEsc(e.message)+'</div>';
  }
}

function pmClose(){
  var overlay = document.getElementById('pmOverlay');
  if(overlay) overlay.classList.remove('active');
}

document.addEventListener('keydown', function(e){
  if(e.key === 'Escape') pmClose();
});
