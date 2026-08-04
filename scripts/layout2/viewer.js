/* ============================================================
   lecture-to-notes layout2 viewer
   Sections:
     1. data + state
     2. helpers
     3. video control + sequential autoplay
     4. view state (home / segment / mode)
     5. playback↔note sync (auto-highlight + auto-scroll)
     6. drawer (burger)
     7. floating video window (drag / resize / persist)
     8. responsive tier defaults (matchMedia)
     9. pane resizers
    10. event bindings + init
   ============================================================ */

/* ---- 1. data + state ---- */
const mediaRoot = TIMELINE.media_root_relative_to_html || '..';
const mediaSrc = TIMELINE.media_src || {};
// parts the exporter could not find on disk (exists:false) — notes and timestamps
// stay useful, but pointing the player at them would just fail silently
const missingMedia = new Set((TIMELINE.media_parts||[]).filter(p=>p.exists===false).map(p=>p.file));
const blocks = [...TIMELINE.note_blocks].sort((a,b)=>a.start_sec-b.start_sec || String(a.block_id).localeCompare(String(b.block_id)));
const segments = TIMELINE.segments || [];
let activeSegmentId = segments[0]?.segment_id || '';
let currentView = 'home';            // 'home' | 'segment' — gates whether sync may switch panels
let autoScroll = true;
let programmaticScroll = false;
let autoFollowSuspendedUntil = 0;

/* ---- 2. helpers ---- */
function q(sel, root=document){return root.querySelector(sel)}
function qa(sel, root=document){return [...root.querySelectorAll(sel)]}
function mediaUrl(file){
  if(mediaSrc[file]){const p=mediaSrc[file].split('/').map(encodeURIComponent).join('/'); return p;}
  return mediaRoot.replace(/\/$/,'') + '/' + encodeURIComponent(file);
}
function fmt(t){t=Math.max(0,Math.floor(t||0)); return String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0')}
function segmentById(id){return segments.find(s=>s.segment_id===id)}
function segFiles(seg){return (seg && (seg.media_files || [seg.media_file]).filter(Boolean)) || []}  // R9 fallback

/* ---- 3. video control + sequential autoplay ---- */
function setVideo(file, t, autoplay=true){
  const v=q('#player'), now=q('#now');
  if(missingMedia.has(file)){
    now.textContent='⚠ '+file+' @ '+fmt(t)+'（來源檔已遺失，無法播放）';
    v.removeAttribute('src'); delete v.dataset.file; v.load();
    return;
  }
  now.textContent='▶ '+file+' @ '+fmt(t);
  if(v.dataset.file!==file){
    v.src=mediaUrl(file); v.dataset.file=file;
    v.addEventListener('loadedmetadata',()=>{try{v.currentTime=t}catch(e){}; if(autoplay) v.play().catch(()=>{});},{once:true});
    v.load();
  }else{try{v.currentTime=t}catch(e){}; if(autoplay) v.play().catch(()=>{});}
}
// when a clip ends, continue to the next clip of the SAME segment (R8: order from media_files only)
function playNextClip(){
  const v=q('#player'); const seg=segmentById(activeSegmentId); const files=segFiles(seg);
  const i=files.indexOf(v.dataset.file);
  if(i>=0 && i+1<files.length){
    const nxt=files.slice(i+1).find(f=>!missingMedia.has(f));
    if(nxt) setVideo(nxt, 0, true);
  }
}

/* ---- 4. view state ---- */
function setMode(mode){
  document.body.classList.toggle('mode-summary', mode==='summary');
  document.body.classList.toggle('mode-replay', mode==='replay');
  qa('.modebar button').forEach(btn=>btn.classList.toggle('active', btn.dataset.mode===mode));
}
function showPaneSection(paneSel, sectionId, emptySel, scrollTop=false){
  qa('.note-panel', q(paneSel)).forEach(el=>{ if(el.id!=='home') el.classList.toggle('active', el.id===sectionId); });
  const has = Boolean(sectionId && q('#'+CSS.escape(sectionId), q(paneSel)));
  q(emptySel).classList.toggle('active', !has);
  if(scrollTop) q(paneSel).scrollTop = 0;
}
function showHome(){
  currentView='home';
  document.body.classList.add('home-view');
  qa('.segment-card').forEach(el=>el.classList.remove('is-active'));
  q('#segment-title').textContent = TIMELINE.course || '首頁';
}
function showSegment(segmentId, scrollTop=false){
  const seg = segmentById(segmentId); if(!seg) return;
  currentView='segment';
  document.body.classList.remove('home-view');
  activeSegmentId = segmentId;
  qa('.segment-card').forEach(el=>el.classList.toggle('is-active', el.dataset.segmentId===segmentId));
  showPaneSection('#summary-pane', seg.summary_section_id || '', '#summary-empty', scrollTop);
  showPaneSection('#replay-pane', seg.replay_section_id || '', '#replay-empty', scrollTop);
  q('#segment-title').textContent = seg.segment_number + ' — ' + seg.title;
}

/* ---- 5. playback↔note sync ---- */
function setAutoScroll(value){
  autoScroll = Boolean(value);
  const button = q('#toggle-scroll');
  const suspended = autoScroll && Date.now() < autoFollowSuspendedUntil;
  button.textContent = suspended ? '自動捲動：暫停' : (autoScroll ? '自動捲動：開' : '自動捲動：關');
  button.classList.toggle('active', autoScroll && !suspended);
}
function effectiveAutoScroll(){
  if(!autoScroll) return false;
  if(Date.now() < autoFollowSuspendedUntil) return false;
  return !q('#player').paused;
}
function suspendAutoFollowBriefly(){
  if(!autoScroll || q('#player').paused) return;
  autoFollowSuspendedUntil = Date.now() + 5000;
  setAutoScroll(true);
  window.clearTimeout(suspendAutoFollowBriefly.timer);
  suspendAutoFollowBriefly.timer = window.setTimeout(()=>setAutoScroll(true), 5100);
}
function scrollBlockInsidePane(block, paneSel){
  const pane = q(paneSel);
  if(!pane) return;
  const el = q('[data-sync-block="'+CSS.escape(block.block_id)+'"]', pane);
  if(!el) return;
  const paneRect = pane.getBoundingClientRect();
  const elRect = el.getBoundingClientRect();
  const target = pane.scrollTop + (elRect.top - paneRect.top) - pane.clientHeight * 0.42;
  programmaticScroll = true;
  pane.scrollTo({top: Math.max(0, target), behavior: 'auto'});
  window.setTimeout(()=>{programmaticScroll = false;}, 80);
}
function matchingBlocks(file,t){
  return blocks.filter(b => b.media_file===file && Number(b.start_sec)-0.15 <= t && t < Number(b.end_sec)+0.15);
}
function activateForTime(file,t, doScroll=true, force=false){
  if(!force && q('#player').paused) return;
  // R1: in home view, reflect playback in the now-bar but NEVER switch panels back to a segment
  if(currentView==='home'){
    const cands = matchingBlocks(file,t);
    if(cands.length){ const tgt=cands.find(b=>b.section_kind==='summary_note')||cands[0];
      q('#now').textContent='▶ '+tgt.media_file+' @ '+fmt(tgt.start_sec)+' — '+(tgt.text||''); }
    return;
  }
  const candidates = matchingBlocks(file,t);
  if(!candidates.length) return;
  const preferred = candidates.find(b=>b.segment_id===activeSegmentId) || candidates.find(b=>b.section_kind==='summary_note') || candidates[0];
  const segId = preferred.segment_id;
  showSegment(segId, false);
  const active = candidates.filter(b=>b.segment_id===segId);
  qa('[data-sync-block]').forEach(el=>el.classList.remove('is-active'));
  for(const block of active){
    const el = q('[data-sync-block="'+CSS.escape(block.block_id)+'"]');
    if(el) el.classList.add('is-active');
  }
  const target = active.find(b=>b.section_kind==='summary_note') || active[0];
  q('#now').textContent = '▶ '+target.media_file+' @ '+fmt(target.start_sec)+' — '+(target.text||'');
  setAutoScroll(autoScroll);
  if(doScroll && effectiveAutoScroll()){
    for(const paneSel of ['#summary-pane','#replay-pane']){
      const block = active.find(item => {
        const pane = q(paneSel);
        return pane && q('[data-sync-block="'+CSS.escape(item.block_id)+'"]', pane);
      });
      if(block) scrollBlockInsidePane(block, paneSel);
    }
  }
}
for(const paneSel of ['#summary-pane','#replay-pane']){
  const pane = q(paneSel);
  if(pane){
    pane.addEventListener('wheel', () => { if(!programmaticScroll) suspendAutoFollowBriefly(); }, {passive:true});
    pane.addEventListener('touchstart', () => { if(!programmaticScroll) suspendAutoFollowBriefly(); }, {passive:true});
  }
}

/* ---- 6. drawer (burger) ---- */
function setDrawer(open){
  document.body.classList.toggle('drawer-closed', !open);
  const button = q('#drawer-toggle');
  if(button) button.classList.toggle('active', open);
}

/* ---- 7. floating video window ---- */
const FKEY='l2n_float';
function loadFloatState(){ try{return JSON.parse(localStorage.getItem(FKEY))||{};}catch(e){return {};} }
function saveFloatState(s){ try{localStorage.setItem(FKEY, JSON.stringify(s));}catch(e){} }
function clampFloat(){
  const vp=q('#video-pane');
  if(!document.body.classList.contains('video-floating')) return;
  const r=vp.getBoundingClientRect();
  let left=parseFloat(vp.style.left), top=parseFloat(vp.style.top);
  if(!isNaN(left)){ left=Math.min(Math.max(0,left), Math.max(0,window.innerWidth-r.width)); vp.style.left=left+'px'; }
  if(!isNaN(top)){ top=Math.min(Math.max(0,top), Math.max(0,window.innerHeight-r.height)); vp.style.top=top+'px'; }
}
function setVideoFloating(on, persist){
  document.body.classList.toggle('video-floating', on);
  const ft=q('#video-float-toggle'); if(ft) ft.textContent = on ? '固定' : '浮動';  // single label = the action (B)
  if(!on){ document.body.classList.remove('video-min'); const mb=q('#video-min-toggle'); if(mb) mb.textContent='－'; }
  const vp=q('#video-pane');
  if(on){
    const fs=loadFloatState();
    if(fs.w) document.body.style.setProperty('--float-w', fs.w+'px');
    if(fs.h) document.body.style.setProperty('--float-h', fs.h+'px');
    if(fs.x!=null && fs.y!=null){ vp.style.left=fs.x+'px'; vp.style.top=fs.y+'px'; vp.style.right='auto'; vp.style.bottom='auto'; }
    else { vp.style.left=vp.style.top=vp.style.right=vp.style.bottom=''; }
    clampFloat();
  }else{
    vp.style.left=vp.style.top=vp.style.right=vp.style.bottom='';
  }
  if(persist){ const fs=loadFloatState(); fs.on=on; saveFloatState(fs); }
}
function installFloat(){
  const vp=q('#video-pane'), handle=q('#drag-handle');
  if(handle){
    handle.addEventListener('pointerdown', e=>{
      if(!document.body.classList.contains('video-floating')) return;
      e.preventDefault(); try{handle.setPointerCapture(e.pointerId);}catch(_){}
      const r=vp.getBoundingClientRect(); const ox=e.clientX-r.left, oy=e.clientY-r.top;
      const move=m=>{ let x=m.clientX-ox, y=m.clientY-oy;
        x=Math.min(Math.max(0,x), Math.max(0,window.innerWidth-r.width));
        y=Math.min(Math.max(0,y), Math.max(0,window.innerHeight-r.height));
        vp.style.left=x+'px'; vp.style.top=y+'px'; vp.style.right='auto'; vp.style.bottom='auto'; };
      const up=()=>{ cleanup(); const fs=loadFloatState(); fs.x=parseFloat(vp.style.left)||0; fs.y=parseFloat(vp.style.top)||0; saveFloatState(fs); };
      const cleanup=()=>{ try{handle.releasePointerCapture(e.pointerId);}catch(_){}
        handle.removeEventListener('pointermove',move); handle.removeEventListener('pointerup',up); handle.removeEventListener('pointercancel',up); };
      handle.addEventListener('pointermove',move); handle.addEventListener('pointerup',up); handle.addEventListener('pointercancel',up);
    });
  }
  // four corners: each resizes from its own corner (W/N edges also move left/top) (#1)
  qa('.rz').forEach(rz=>{
    rz.addEventListener('pointerdown', e=>{
      if(!document.body.classList.contains('video-floating')) return;
      e.preventDefault(); e.stopPropagation(); try{rz.setPointerCapture(e.pointerId);}catch(_){}
      const c=rz.dataset.corner||'se'; const r=vp.getBoundingClientRect();
      const x0=e.clientX, y0=e.clientY, w0=r.width, h0=r.height, l0=r.left, t0=r.top;
      vp.style.left=l0+'px'; vp.style.top=t0+'px'; vp.style.right='auto'; vp.style.bottom='auto';
      const move=m=>{ let dx=m.clientX-x0, dy=m.clientY-y0, w=w0, h=h0, l=l0, t=t0;
        if(c.includes('e')) w=w0+dx;
        if(c.includes('s')) h=h0+dy;
        if(c.includes('w')){ w=w0-dx; l=l0+dx; }
        if(c.includes('n')){ h=h0-dy; t=t0+dy; }
        if(w<220){ if(c.includes('w')) l=l0+(w0-220); w=220; }
        if(h<150){ if(c.includes('n')) t=t0+(h0-150); h=150; }
        l=Math.min(Math.max(0,l), Math.max(0,window.innerWidth-w));
        t=Math.min(Math.max(0,t), Math.max(0,window.innerHeight-h));
        vp.style.left=l+'px'; vp.style.top=t+'px';
        document.body.style.setProperty('--float-w', w+'px'); document.body.style.setProperty('--float-h', h+'px'); };
      const up=()=>{ cleanup(); const fs=loadFloatState();
        fs.w=parseFloat(getComputedStyle(document.body).getPropertyValue('--float-w'))||380;
        fs.h=parseFloat(getComputedStyle(document.body).getPropertyValue('--float-h'))||290;
        fs.x=parseFloat(vp.style.left)||0; fs.y=parseFloat(vp.style.top)||0; saveFloatState(fs); };
      const cleanup=()=>{ try{rz.releasePointerCapture(e.pointerId);}catch(_){}
        rz.removeEventListener('pointermove',move); rz.removeEventListener('pointerup',up); rz.removeEventListener('pointercancel',up); };
      rz.addEventListener('pointermove',move); rz.addEventListener('pointerup',up); rz.addEventListener('pointercancel',up);
    });
  });
}

/* ---- 8. responsive tier defaults ---- */
const mqlNarrow = window.matchMedia('(max-width:720px)');
const mqlMid = window.matchMedia('(max-width:1180px)');
function currentTier(){ return mqlNarrow.matches ? 'narrow' : (mqlMid.matches ? 'mid' : 'wide'); }
let lastTier = null;
function applyTier(tier){
  if(tier==='narrow'){ setDrawer(false); setVideoFloating(true,false); }
  else if(tier==='mid'){ setDrawer(true); setVideoFloating(true,false); }
  else { setDrawer(true); const fs=loadFloatState(); setVideoFloating(!!fs.on,false); }   // wide: honor saved pref
}
function onTierChange(){ const t=currentTier(); if(t!==lastTier){ lastTier=t; applyTier(t); } clampFloat(); }
mqlNarrow.addEventListener('change', onTierChange);
mqlMid.addEventListener('change', onTierChange);
window.addEventListener('resize', clampFloat);

/* ---- 9. pane resizers ---- */
function installResizers(){
  const app = q('.app-v3');
  const split = q('.split-notes');
  const col = q('#col-resizer');
  const row = q('#split-resizer');
  if(col){
    col.addEventListener('pointerdown', event => {
      event.preventDefault();
      try{col.setPointerCapture(event.pointerId);}catch(e){}
      col.classList.add('dragging');
      const onMove = move => {
        const rect = app.getBoundingClientRect();
        const width = Math.min(Math.max(move.clientX - rect.left, 360), Math.max(420, rect.width - 720));
        app.style.setProperty('--video-col', width + 'px');
      };
      const onUp = up => {
        try{col.releasePointerCapture(up.pointerId);}catch(e){}
        col.classList.remove('dragging');
        col.removeEventListener('pointermove', onMove);
        col.removeEventListener('pointerup', onUp);
      };
      col.addEventListener('pointermove', onMove);
      col.addEventListener('pointerup', onUp);
    });
  }
  if(row){
    row.addEventListener('pointerdown', event => {
      event.preventDefault();
      try{row.setPointerCapture(event.pointerId);}catch(e){}
      row.classList.add('dragging');
      const onMove = move => {
        const rect = split.getBoundingClientRect();
        const top = Math.min(Math.max(move.clientY - rect.top, 120), Math.max(160, rect.height - 160));
        split.style.setProperty('--summary-row', top + 'px');
      };
      const onUp = up => {
        try{row.releasePointerCapture(up.pointerId);}catch(e){}
        row.classList.remove('dragging');
        row.removeEventListener('pointermove', onMove);
        row.removeEventListener('pointerup', onUp);
      };
      row.addEventListener('pointermove', onMove);
      row.addEventListener('pointerup', onUp);
    });
  }
}

/* ---- 9b. font scale (老花 zoom: header A− / A＋, scales rem-based UI, persisted) ---- */
const ZKEY='l2n_fontscale';
let fontScale=1; try{const _z=parseFloat(localStorage.getItem(ZKEY)); if(_z) fontScale=_z;}catch(e){}
function applyFontScale(s){
  fontScale=Math.min(1.7, Math.max(0.85, Math.round(s*100)/100));
  document.documentElement.style.fontSize=(16*fontScale)+'px';
  try{localStorage.setItem(ZKEY, String(fontScale));}catch(e){}
}

/* ---- 9c. in-viewer full-text search (dependency-free) ----
   Filters ALL note_blocks (summary + transcript, every segment) PLUS the slide
   layer (slide_blocks: OCR text + VLM summary per canonical slide — terms that
   appear only ON a slide, never spoken) by AND-of-terms, renders a results
   dropdown, and jumps on click. Note hits jump to the bullet; slide hits jump
   the player to the slide's display window (the slide is on screen in the video
   itself — no copied image needed). */
const slideBlocks = TIMELINE.slide_blocks || [];
const searchIndex = blocks.map(b => ({b, hay: (b.text || '').toLowerCase()}))
  .concat(slideBlocks.map((s, i) => ({slide: s, slideIdx: i, hay: (s.text || '').toLowerCase()})));
function escHtml(s){ return String(s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function snippet(text, terms){
  const t = text || '';
  let idx = -1;
  for(const term of terms){ const p = t.toLowerCase().indexOf(term); if(p>=0 && (idx<0 || p<idx)) idx=p; }
  let start = Math.max(0, idx - 24);
  let frag = t.slice(start, start + 90);
  let html = escHtml((start>0?'…':'') + frag + (start+90 < t.length ? '…' : ''));
  for(const term of terms){
    if(!term) continue;
    const re = new RegExp('(' + term.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + ')', 'ig');
    html = html.replace(re, '<mark>$1</mark>');
  }
  return html;
}
function runSearch(qstr){
  const panel = q('#search-results'); if(!panel) return;
  const terms = (qstr||'').trim().toLowerCase().split(/\s+/).filter(Boolean);
  if(!terms.length){ panel.innerHTML=''; panel.classList.remove('open'); return; }
  const hits = [];
  for(const item of searchIndex){
    if(terms.every(t=>item.hay.includes(t))){ hits.push(item); if(hits.length>=80) break; }
  }
  if(!hits.length){ panel.innerHTML='<div class="sr-empty">無符合結果</div>'; panel.classList.add('open'); return; }
  const rows = hits.map(item=>{
    if(item.slide){
      const s = item.slide;
      return '<button class="sr-item" type="button" data-slide="'+item.slideIdx+'">'
        + '<span class="sr-seg">投影片</span>'
        + '<span class="sr-text">'+snippet(s.text, terms)+'</span>'
        + '<span class="sr-ts">'+fmt(s.start_sec)+'</span></button>';
    }
    const b = item.b;
    const seg = segmentById(b.segment_id);
    const segNo = seg ? seg.segment_number : '';
    const kind = b.section_kind==='summary_note' ? '總' : '逐';
    return '<button class="sr-item" type="button" data-block="'+escHtml(String(b.block_id))+'">'
      + '<span class="sr-seg">'+escHtml(segNo)+' · '+kind+'</span>'
      + '<span class="sr-text">'+snippet(b.text, terms)+'</span>'
      + '<span class="sr-ts">'+fmt(b.start_sec)+'</span></button>';
  });
  panel.innerHTML = '<div class="sr-count">'+hits.length+(hits.length>=80?'+':'')+' 筆</div>' + rows.join('');
  panel.classList.add('open');
}
function closeSearch(){ const p=q('#search-results'); if(p) p.classList.remove('open'); }
function jumpToBlock(blockId){
  const b = blocks.find(x=>String(x.block_id)===String(blockId)); if(!b) return;
  closeSearch();
  // make sure the pane holding this block is visible
  if(b.section_kind==='summary_note' && document.body.classList.contains('mode-replay')) setMode('split');
  if(b.section_kind==='transcript_index' && document.body.classList.contains('mode-summary')) setMode('split');
  showSegment(b.segment_id, false);
  setVideo(b.media_file, Number(b.start_sec)||0, false);
  activateForTime(b.media_file, Number(b.start_sec)||0, true, true);
  window.setTimeout(()=>{
    for(const paneSel of ['#summary-pane','#replay-pane']){
      const pane=q(paneSel);
      if(pane && q('[data-sync-block="'+CSS.escape(b.block_id)+'"]', pane)) scrollBlockInsidePane(b, paneSel);
    }
  }, 60);
}
/* Cue player to (file, t) and land on the owning segment's notes. Shared by slide
   search hits and ?f=&t= deep links. When no note block covers that instant
   (slide shown during an untimestamped stretch), just cue the video. */
function seekTo(file, t, autoplay){
  const cands = matchingBlocks(file, t);
  if(cands.length){
    const b = cands.find(x=>x.section_kind==='summary_note') || cands[0];
    showSegment(b.segment_id, false);
  }else{
    const seg = segments.find(s=>segFiles(s).includes(file));
    if(seg) showSegment(seg.segment_id, false);
  }
  setVideo(file, t, autoplay);
  activateForTime(file, t, true, true);
}
function jumpToSlide(i){
  const s = slideBlocks[i]; if(!s) return;
  closeSearch();
  seekTo(s.media_file, Number(s.start_sec)||0, false);
}
/* ---- 9d. deep links (?f=<media file>&t=<sec>) + copy-link button ----
   Works on file:// too (query survives). No autoplay: browsers block
   sound-on autoplay, and a shared link should land quietly on the spot. */
function applyDeepLink(){
  let params;
  try{ params = new URLSearchParams(location.search); }catch(e){ return false; }
  const t = parseFloat(params.get('t'));
  if(isNaN(t) || t < 0) return false;
  let file = params.get('f') || '';
  const known = new Set((TIMELINE.media_parts||[]).map(p=>p.file));
  if(file && !known.has(file)){
    // tolerate an encoded/renamed link: match by basename before giving up
    const hit = [...known].find(k=>k.toLowerCase()===file.toLowerCase());
    if(hit) file = hit; else file = '';
  }
  if(!file) file = segments[0] ? segments[0].media_file : '';
  if(!file) return false;
  seekTo(file, t, false);
  return true;
}
function copyCurrentLink(btn){
  const v = q('#player');
  const file = v.dataset.file || (segments[0] && segments[0].media_file) || '';
  if(!file) return;
  const t = Math.floor(v.currentTime || 0);
  const base = location.href.split('#')[0].split('?')[0];
  const url = base + '?f=' + encodeURIComponent(file) + '&t=' + t;
  const done = ()=>{ if(btn){ const old=btn.textContent; btn.textContent='✓'; window.setTimeout(()=>{btn.textContent=old;}, 1200); } };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(url).then(done, ()=>window.prompt('複製這個連結：', url));
  }else{
    window.prompt('複製這個連結：', url);
  }
}

/* ---- 10. event bindings + init ---- */
document.addEventListener('click', e=>{
  const fdec=e.target.closest('#font-dec'); if(fdec){e.preventDefault(); applyFontScale(fontScale-0.1); return;}
  const finc=e.target.closest('#font-inc'); if(finc){e.preventDefault(); applyFontScale(fontScale+0.1); return;}
  const ts=e.target.closest('a.ts');
  if(ts){e.preventDefault(); const t=Number(ts.dataset.t||0); setVideo(ts.dataset.file,t); activateForTime(ts.dataset.file,t,true,true); return;}
  const card=e.target.closest('.segment-card, .home-card');
  if(card){e.preventDefault(); const t=Number(card.dataset.t||0); showSegment(card.dataset.segmentId,true); setVideo(card.dataset.file,t,false); activateForTime(card.dataset.file,t,false,true); return;}
  const goHome=e.target.closest('[data-go-home]');
  if(goHome){e.preventDefault(); showHome(); return;}
  const fl=e.target.closest('[data-toggle-float]');
  if(fl){e.preventDefault(); setVideoFloating(!document.body.classList.contains('video-floating'), true); return;}
  const mn=e.target.closest('[data-toggle-min]');
  if(mn){e.preventDefault(); const on=document.body.classList.toggle('video-min'); mn.textContent=on?'＋':'－'; clampFloat(); return;}
  const mode=e.target.closest('[data-mode]');
  if(mode){e.preventDefault(); setMode(mode.dataset.mode); return;}
  const toggle=e.target.closest('[data-toggle-drawer]');
  if(toggle){e.preventDefault(); setDrawer(document.body.classList.contains('drawer-closed')); return;}
  const sr=e.target.closest('.sr-item');
  if(sr){e.preventDefault();
    if(sr.dataset.slide!==undefined){ jumpToSlide(Number(sr.dataset.slide)); }
    else jumpToBlock(sr.dataset.block);
    return;}
  const cl=e.target.closest('#copy-link');
  if(cl){e.preventDefault(); copyCurrentLink(cl); return;}
  // click outside the search box closes the results dropdown
  if(!e.target.closest('.ph-search')) closeSearch();
});
(function bindSearch(){
  const inp=q('#search-input'); if(!inp) return;
  let timer=null;
  inp.addEventListener('input', ()=>{ window.clearTimeout(timer); timer=window.setTimeout(()=>runSearch(inp.value), 120); });
  inp.addEventListener('keydown', e=>{
    if(e.key==='Escape'){ inp.value=''; closeSearch(); inp.blur(); }
    else if(e.key==='Enter'){ const first=q('#search-results .sr-item'); if(first){ e.preventDefault(); jumpToBlock(first.dataset.block); } }
  });
  inp.addEventListener('focus', ()=>{ if(inp.value.trim()) runSearch(inp.value); });
})();
document.addEventListener('keydown', e=>{
  if(e.key==='/' && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName||''))){
    const inp=q('#search-input'); if(inp){ e.preventDefault(); inp.focus(); }
  }
});
q('#player').addEventListener('timeupdate', e=>{const v=e.currentTarget; if(v.dataset.file && !v.paused) activateForTime(v.dataset.file, v.currentTime, true);});
q('#player').addEventListener('play',()=>setAutoScroll(autoScroll));
q('#player').addEventListener('pause',()=>setAutoScroll(autoScroll));
q('#player').addEventListener('ended', playNextClip);
q('#toggle-scroll').addEventListener('click',()=>setAutoScroll(!autoScroll));

setMode('split');
applyFontScale(fontScale);
installResizers();
installFloat();
setAutoScroll(true);
lastTier = currentTier();
applyTier(lastTier);
// #14: a single-segment lecture has nothing to overview → skip the home page, go straight in
const singleSeg = segments.length <= 1;
if(singleSeg){ document.body.classList.add('no-home'); if(segments[0]) showSegment(segments[0].segment_id, true); }
else showHome();
// a ?f=&t= deep link wins over the default first-segment cue
if(!applyDeepLink() && segments[0]){ const s0=segments[0]; setVideo(s0.media_file, Number(s0.start_sec||0), false); }
