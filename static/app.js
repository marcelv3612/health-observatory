'use strict';
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const colors = {deep:'#364f84',core:'#527f91',rem:'#8b5b92',unspecified:'#6e7d86',awake:'#a56f29'};
let state, selectedId, page = 0, draftOrder = [], busy = false;
const pageSize = 12;
let sleepRequest = 0, periodRequest = 0;
const duration = (minutes, compact=false) => {
  if (minutes == null) return '—';
  const m = Math.round(minutes), h = Math.floor(m / 60), remainder = m % 60;
  return h ? `${h}h ${String(remainder).padStart(2,'0')}m` : `${m}m`;
};
const metricDuration = (minutes) => {
  if (minutes == null) return '—';
  const m = Math.round(minutes);
  return `${Math.floor(m/60)}<small>h</small> ${String(m%60).padStart(2,'0')}<small>m</small>`;
};
const clock = (iso) => iso ? iso.slice(11,16) : '—';
const clockMinutes = (m) => m == null ? '—' : `${String(Math.floor(Math.round(m)%1440/60)).padStart(2,'0')}:${String(Math.round(m)%60).padStart(2,'0')}`;
const dateLabel = (date) => new Intl.DateTimeFormat('en-GB',{month:'short',day:'numeric',timeZone:'UTC'}).format(new Date(`${date}T12:00:00Z`));
function message(text, error=false) { $('#status').textContent=text; $('#status').classList.toggle('error',error); }
async function api(path, data, raw=false) {
  const options = data === undefined ? {} : {method:'POST',headers:{'X-Local-Token':state?.token || '','Content-Type':raw?'application/octet-stream':'application/json'},body:raw?data:JSON.stringify(data)};
  const response = await fetch(path,options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The local request failed.');
  return result;
}
async function refresh({background=false}={}) {
  const q = new URLSearchParams();
  if ($('#startDate').value) q.set('start',$('#startDate').value);
  if ($('#endDate').value) q.set('end',$('#endDate').value);
  const request = ++sleepRequest;
  $('.trend-panel').setAttribute('aria-busy','true');
  try {
    const incoming = await api(`/api/state?${q}`);
    if (request !== sleepRequest) return false;
    if (background && ($('#customizeDialog').open || document.activeElement?.closest('.annotation-form'))) return false;
    state = incoming;
    if (!state.nights.some(n=>n.id===selectedId)) selectedId=state.nights[0]?.id;
    render();
    return true;
  } catch(error) {
    if (request !== sleepRequest) return false;
    throw error;
  } finally {
    if (request === sleepRequest) $('.trend-panel').setAttribute('aria-busy','false');
  }
}
function syncRangeButtons(selected) {
  document.querySelectorAll('[data-range]').forEach(button => {
    const active = button.dataset.range === selected;
    button.classList.toggle('selected',active);
    button.setAttribute('aria-pressed',String(active));
  });
}

function render() {
  const s=state.summary;
  $('#demoBanner').hidden=state.mode!=='demo';
  $('#sleepDataMode').textContent = state.mode==='demo' ? 'Synthetic sleep history' : 'Local sleep history';
  const imported = state.imported_at ? new Date(state.imported_at).toLocaleString() : state.mode==='imported' ? 'timestamp unavailable' : 'no actual export yet';
  const sync = state.feed?.last_success ? `Local feed last succeeded: ${new Date(state.feed.last_success).toLocaleString()}. Phone-side export must be configured separately.` : `Daily phone sync is not configured. Last imported export: ${imported}.`;
  $('#syncStatus').textContent = sync + (state.feed?.last_error ? ` Latest feed error: ${state.feed.last_error}` : '');
  $('#scopeLabel').textContent=`${s.nights} included · ${s.excluded} excluded`;
  const from=$('#startDate').value || state.date_bounds.start, to=$('#endDate').value || state.date_bounds.end;
  $('#periodLabel').textContent = from && to ? `${dateLabel(from)} ${from.slice(0,4)} – ${dateLabel(to)} ${to.slice(0,4)}` : 'No recorded dates';
  $('#avgDuration').innerHTML=metricDuration(s.average_minutes);
  $('#durationCaption').textContent=`Average of ${s.nights} included sleep session${s.nights===1?'':'s'}`;
  $('#targetCaption').textContent=`${s.target_met} of ${s.nights} meet your ${state.settings.target_hours}h target`;
  $('#bedtimeSpread').innerHTML=s.bedtime_spread==null?'—':`± ${Math.round(s.bedtime_spread)}<small> min</small>`;
  $('#bedtimeCaption').textContent=`Typical bedtime ${clockMinutes(s.mean_bedtime)} · ${s.zones.length>1?'mixed local clocks':s.zones[0]||'no nights'}`;
  $('#stageMini').innerHTML=['deep','core','rem'].map(stage=>`<div><strong style="color:${colors[stage]}">${s.stage_averages[stage]==null?'—':Math.round(s.stage_averages[stage])}<small>m</small></strong><em>${stage[0].toUpperCase()+stage.slice(1)}</em></div>`).join('');
  $('#stageCaption').textContent = `Recorded stage averages · ${s.stage_sessions ?? '—'} sessions with stages`;
  $('#stageCoverage').textContent = `${s.stage_incomplete ?? '—'} have unclassified intervals · gaps stay unknown`;
  $('#gapValue').innerHTML=metricDuration(s.gap_minutes);
  $('#excludedCaption').textContent=`${s.excluded} excluded · borrowed nights never enter averages`;
  $('#trendTarget').textContent=`Your target · ${state.settings.target_hours}h`;
  $('#historyCount').textContent=`${state.nights.length} sessions`;
  document.querySelectorAll('[data-widget]').forEach(el=>{el.hidden=!state.settings.widgets[el.dataset.widget];});
  renderTrend(); renderHistory(); renderDetail();
}
function renderTrend() {
  const byDate = new Map();
  state.nights.forEach(n=>{if(!byDate.has(n.date))byDate.set(n.date,[]);byDate.get(n.date).push(n);});
  const dates=[...byDate.keys()].sort();
  if(!dates.length){$('#trendChart').innerHTML='<div class="empty-state">No sessions in this date range.</div>';return;}
  let first=new Date(`${dates[0]}T12:00:00Z`), last=new Date(`${dates.at(-1)}T12:00:00Z`);
  const limited=(last-first)/86400000>89;
  if(limited)first=new Date(last.getTime()-89*86400000);
  const items=[];
  for(let day=new Date(first);day<=last;day.setUTCDate(day.getUTCDate()+1)) {
    const key=day.toISOString().slice(0,10), nights=byDate.get(key);
    if(nights)nights.slice().sort((a,b)=>a.start-b.start).forEach(n=>items.push({date:key,n}));
    else items.push({date:key,n:null});
  }
  const width=Math.max(680,($('#trendChart').clientWidth || 680)-30,items.length*15+70), height=230, left=44, right=18, top=20, bottom=195;
  const max=Math.max(600,state.settings.target_hours*60,...items.map(i=>i.n?.total_minutes||0));
  const y=m=>bottom-(bottom-top)*m/max, slot=(width-left-right)/items.length, barWidth=Math.min(28,slot*.67);
  let svg=`<svg class="trend-svg" style="min-width:${width}px" viewBox="0 0 ${width} ${height}" role="img" aria-label="Recorded sleep by session. Excluded sessions are grey. Gaps in dates remain blank."><title>Recorded sleep over time</title>`;
  for(let m=0;m<=max;m+=120)svg+=`<line class="grid" x1="${left}" y1="${y(m)}" x2="${width-right}" y2="${y(m)}"/><text x="${left-12}" y="${y(m)+3}" text-anchor="end">${m/60}h</text>`;
  svg+=`<line class="target-line" x1="${left}" y1="${y(state.settings.target_hours*60)}" x2="${width-right}" y2="${y(state.settings.target_hours*60)}"/>`;
  items.forEach((item,index)=>{
    const x=left+slot*index+(slot-barWidth)/2, n=item.n;
    if(n) {
      svg+=`<g class="chart-bar" data-night="${n.id}" tabindex="0" role="button" aria-label="${esc(dateLabel(n.date))}, ${duration(n.total_minutes)}, ${n.excluded?'excluded':'included'}"><title>${esc(n.date)} · ${duration(n.total_minutes)} · ${esc(n.excluded?n.owner+' · excluded':n.sources.join(', '))}</title>`;
      let total=0;
      for(const stage of ['deep','core','rem','unspecified']){
        const minutes=n.minutes[stage];
        if(minutes>0)svg+=`<rect x="${x}" y="${y(total+minutes)}" width="${barWidth}" height="${(bottom-top)*minutes/max}" fill="${n.excluded?'#80929c':colors[stage]}" opacity="${n.excluded?.7:1}"/>`;
        total+=minutes;
      }
      if(n.id===selectedId)svg+=`<circle cx="${x+barWidth/2}" cy="${bottom+6}" r="2.5" fill="#176b63"/>`;
      svg+='</g>';
    }else svg+=`<text x="${x+barWidth/2}" y="${bottom-5}" text-anchor="middle"><title>No recording for ${item.date}</title>·</text>`;
    const step=Math.max(1,Math.ceil(items.length/9));
    if(index%step===0||index===items.length-1)svg+=`<text x="${x+barWidth/2}" y="${bottom+24}" text-anchor="middle">${esc(dateLabel(item.date))}</text>`;
  });
  svg+='</svg>';
  $('#trendChart').innerHTML=svg+(limited?'<p class="chart-note">Chart shows the latest 90 calendar dates. All sessions remain in the history below.</p>':'');
  $('#trendChart').querySelectorAll('[data-night]').forEach(el=>{
    el.addEventListener('click',()=>selectNight(el.dataset.night));
    el.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectNight(el.dataset.night);}});
  });
}
function selectNight(id) {selectedId=id;const idx=state.nights.findIndex(n=>n.id===id);if(idx>=0)page=Math.floor(idx/pageSize);renderTrend();renderHistory();renderDetail();}
function renderHistory() {
  const pages=Math.max(1,Math.ceil(state.nights.length/pageSize));page=Math.min(page,pages-1);
  const nights=state.nights.slice(page*pageSize,(page+1)*pageSize);
  $('#historyRows').innerHTML=nights.map(n=>`<tr class="${n.id===selectedId?'selected-row':''}"><td><button class="night-link" data-id="${n.id}">${esc(dateLabel(n.date))}</button><small>${esc(n.date.slice(0,4))} · ${esc(n.owner)}</small></td><td><strong>${duration(n.total_minutes)}</strong><small>${n.minutes.unspecified?'Includes unspecified sleep':'Estimated stages'}</small></td><td><strong>${clock(n.bedtime_local)} → ${clock(n.wake_local)}</strong><small>${esc(n.timezone)}</small></td><td>${n.excluded?`<span class="tiny-badge borrowed">${n.protected?'Borrowed watch':'Excluded'}</span>`:''}${n.gap_minutes||n.reported_gap?'<span class="tiny-badge gap">Gap</span>':''}${n.travel?'<span class="tiny-badge">Offset change</span>':''}${!n.excluded&&!n.gap_minutes&&!n.reported_gap?'<span class="tiny-badge">Included</span>':''}</td></tr>`).join('')||'<tr><td colspan="4" class="empty-state">No sessions match these dates.</td></tr>';
  $('#historyRows').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>selectNight(b.dataset.id)));
  $('#historyPaging').innerHTML=`<span>${state.nights.length?`${page*pageSize+1}–${Math.min((page+1)*pageSize,state.nights.length)} of ${state.nights.length}`:'0 sessions'}</span><div><button id="prevPage" ${page===0?'disabled':''} aria-label="Previous history page">←</button><button id="nextPage" ${page===pages-1?'disabled':''} aria-label="Next history page">→</button></div>`;
  $('#prevPage').addEventListener('click',()=>{page--;renderHistory();});$('#nextPage').addEventListener('click',()=>{page++;renderHistory();});
}
const reportedGaps = n => n.reported_gaps || (n.reported_gap ? [n.reported_gap] : []);
function hypnogram(n) {
  const width=450,left=68,right=10,top=10, row=17;
  const reported=reportedGaps(n),start=Math.min(n.start,...reported.map(g=>g.start)),end=Math.max(n.end,...reported.map(g=>g.end)), x=t=>left+(width-left-right)*(t-start)/(end-start);
  const stages=['awake','rem','core','deep','unspecified'];
  let svg=`<svg class="hypnogram" viewBox="0 0 ${width} 120" role="img" aria-label="Full recorded sleep stages, with unclassified intervals shaded. No stages are estimated for gaps."><title>Full-night stage timeline</title><defs><pattern id="gapPattern" width="5" height="5" patternUnits="userSpaceOnUse"><path d="M0 5L5 0" stroke="#62727b" stroke-width="1"/></pattern></defs>`;
  for(const gap of n.gaps)svg+=`<rect x="${x(gap.start)}" y="${top}" width="${Math.max(1,x(gap.end)-x(gap.start))}" height="83" fill="url(#gapPattern)"><title>${duration(gap.minutes)} unclassified</title></rect>`;
  for(const gap of reported)svg+=`<rect x="${x(gap.start)}" y="${top}" width="${x(gap.end)-x(gap.start)}" height="83" fill="url(#gapPattern)"><title>${esc(gap.label)} · no stage data</title></rect>`;
  stages.forEach((stage,i)=>{const y=top+i*row;svg+=`<text x="${left-10}" y="${y+10}" text-anchor="end">${stage==='unspecified'?'Unspec.':stage[0].toUpperCase()+stage.slice(1)}</text><line x1="${left}" y1="${y+7}" x2="${width-right}" y2="${y+7}"/>`;});
  for(const seg of n.segments)svg+=`<rect x="${x(seg.start)}" y="${top+stages.indexOf(seg.stage)*row}" width="${Math.max(.7,x(seg.end)-x(seg.start))}" height="13" fill="${colors[seg.stage]}"><title>${esc(seg.stage)} · ${duration((seg.end-seg.start)/60)} · ${esc(seg.source)}</title></rect>`;
  const clockEpoch=t=>new Date((t+n.offset)*1000).toISOString().slice(11,16);
  for(let i=0;i<=4;i++){const t=start+(end-start)*i/4;svg+=`<text x="${x(t)}" y="113" text-anchor="${i===0?'start':i===4?'end':'middle'}">${clockEpoch(t)}</text>`;}
  return svg+'</svg>';
}
function renderDetail() {
  const n=state.nights.find(n=>n.id===selectedId);
  if(!n){$('#nightDetail').innerHTML='<div class="empty-state">Choose a date range with sleep records.</div>';return;}
  const annotation=n.annotation||{};
  $('#nightDetail').innerHTML=`<div class="detail-heading"><div><span class="eyebrow">Selected night</span><h2>${esc(dateLabel(n.date))}, ${esc(n.date.slice(0,4))}</h2></div><span class="subtle-pill">${n.excluded?'Excluded':'Included'}</span></div><div class="detail-total"><strong>${duration(n.total_minutes)}</strong><span>recorded sleep${state.mode==='demo'?' · demo':''}</span></div><div class="night-clock"><div><small>First recorded sleep</small><strong>${clock(n.bedtime_local)}</strong></div><div><small>Last recorded sleep</small><strong>${clock(n.wake_local)}</strong></div><div><small>Local clock</small><strong style="font-size:12px">${esc(n.timezone)}</strong></div></div><div class="timeline-wrap">${hypnogram(n)}</div><div class="stage-breakdown">${['deep','core','rem','awake'].map(stage=>`<div><strong style="color:${colors[stage]}">${duration(n.minutes[stage])}</strong><span>${stage[0].toUpperCase()+stage.slice(1)}</span></div>`).join('')}</div>${n.minutes.unspecified?`<div class="context-note">${duration(n.minutes.unspecified)} recorded as <strong>unspecified sleep</strong>. These minutes are not assigned to deep, core, or REM.</div>`:''}${n.protected?`<div class="context-note loan-note"><strong>Protected borrowed-watch session.</strong><br>This session overlaps a protected window for ${esc(n.owner)} and stays excluded from summaries for ${esc(state.settings.primary_owner || 'You')}. Its wearer and exclusion cannot be changed by a night note.${n.note?`<br>${esc(n.note)}`:''}</div>`:''}${reportedGaps(n).map(g=>`<div class="context-note"><strong>${duration(g.minutes)} user-reported recording gap.</strong> ${esc(g.label)}. Missing stages remain unknown and add no sleep time.</div>`).join('')}${n.gap_minutes?`<div class="context-note"><strong>${duration(n.gap_minutes)} unclassified inside the recorded span.</strong> A recording gap is not evidence of awake time or a missing stage.</div>`:''}${n.travel?'<div class="context-note">UTC offsets change in this session. Duration uses elapsed UTC time. Timeline labels use the first interval’s local offset; endpoint clocks use the selected timezone.</div>':''}<div class="context-note"><strong>${esc(n.sources.join(' · '))}</strong><br>${n.records} records · ${Math.round(n.overlap_minutes)}m overlapping evidence resolved by source priority${state.import_stats.exact_duplicates?` · ${state.import_stats.exact_duplicates} exact duplicates removed from export`:''}</div><form class="annotation-form" id="annotationForm"><label class="exclude-label"><input id="excludeNight" type="checkbox" ${n.excluded?'checked':''} ${n.protected?'disabled':''}> Exclude this session from summaries for ${esc(state.settings.primary_owner || 'You')}${n.protected?' (protected loan)':''}</label><label>Who wore the watch?<input id="nightOwner" type="text" maxlength="1000" value="${esc(n.owner)}" ${n.protected?'disabled':''}></label><label>Context / note<textarea id="nightNote" maxlength="1000" placeholder="Travel, watch removed, illness, unusual schedule…">${esc(annotation.note||'')}</textarea></label><button class="button secondary" type="submit">Save night note</button><div class="annotation-error" id="annotationError" role="alert"></div></form>`;
  $('#annotationForm').addEventListener('submit',async e=>{
    e.preventDefault();
    const form=e.currentTarget;if(form.dataset.saving)return;
    const submit=form.querySelector('button[type=submit]'), errorNode=$('#annotationError');
    const owner=n.protected?n.owner:$('#nightOwner').value.trim()||(state.settings.primary_owner || 'You');
    const newAnnotation={excluded:n.protected||$('#excludeNight').checked,owner,note:$('#nightNote').value.trim(),start:n.start,end:n.end};
    form.dataset.saving='true';form.setAttribute('aria-busy','true');submit.disabled=true;
    try{await api('/api/settings',{annotations:{...state.settings.annotations,[n.id]:newAnnotation}});await refresh();message('Night context saved privately. Summaries have been updated.');}
    catch(error){if(errorNode.isConnected)errorNode.textContent=error.message;else message(error.message,true);}
    finally{delete form.dataset.saving;form.setAttribute('aria-busy','false');submit.disabled=false;}
  });
}
function renderSources() {
  $('#sourceOrder').innerHTML=draftOrder.map((sid,index)=>{
    const source=state.sources.find(s=>s.id===sid);
    return `<div class="source-row"><span>${index+1}</span><span class="source-name">${esc(source.name)}<small>${source.records} records · ${esc(source.device||'device not supplied')} · ${esc(source.id.slice(0,6))}</small></span><button type="button" data-move="${index}" data-dir="-1" aria-label="Move ${esc(source.name)} higher" ${index===0?'disabled':''}>↑</button><button type="button" data-move="${index}" data-dir="1" aria-label="Move ${esc(source.name)} lower" ${index===draftOrder.length-1?'disabled':''}>↓</button></div>`;
  }).join('');
  $('#sourceOrder').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{const i=Number(b.dataset.move),j=i+Number(b.dataset.dir);[draftOrder[i],draftOrder[j]]=[draftOrder[j],draftOrder[i]];renderSources();}));
}
let modalCloseTimer, modalOpener, pendingModalFocus=false, customizeSaving=false;
function motionMilliseconds(name) {
  const value=getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value.endsWith('ms') ? parseFloat(value) : value.endsWith('s') ? parseFloat(value)*1000 : 0;
}
function fillCustomize() {
  $('#primaryOwnerInput').value=state.settings.primary_owner || 'You';
  $('#targetInput').value=state.settings.target_hours;$('#groupingInput').value=state.settings.session_gap_hours;
  $('#timezoneMode').value=state.settings.timezone_mode;$('#timezoneInput').value=state.settings.timezone;
  $('#settingsError').textContent='';$('#settingsProgress').textContent='';
  document.querySelectorAll('[data-card]').forEach(el=>{el.checked=state.settings.widgets[el.dataset.card];});
  draftOrder=[...state.settings.source_order];renderSources();
}
function openCustomize() {
  if(!state || customizeSaving)return;
  const modal=$('#customizeDialog');
  clearTimeout(modalCloseTimer);
  modal.classList.remove('is-closing');
  if (!modal.open) {
    modalOpener=document.activeElement;
    pendingModalFocus=false;
    fillCustomize();
    modal.showModal();
  }
  void modal.offsetWidth;
  modal.classList.add('is-open');
}
function closeCustomize() {
  const modal=$('#customizeDialog');
  if(!modal.open || modal.classList.contains('is-closing'))return;
  clearTimeout(modalCloseTimer);
  modal.classList.remove('is-open');modal.classList.add('is-closing');
  const finish=()=>{
    modal.classList.remove('is-closing');modal.close();
    if (modalOpener?.isConnected && !modalOpener.disabled) modalOpener.focus();
    else if (modalOpener?.isConnected && modalOpener.disabled) {
      pendingModalFocus=true;$('#overview').focus();
    }
  };
  const delay=matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : motionMilliseconds('--modal-close-dur');
  if(delay)modalCloseTimer=setTimeout(finish,delay);else finish();
}
$('#customizeBtn').addEventListener('click',openCustomize);$('#customizeNav').addEventListener('click',openCustomize);
$('#closeCustomize').addEventListener('click',closeCustomize);
$('#customizeDialog').addEventListener('cancel',event=>{event.preventDefault();closeCustomize();});
$('#customizeDialog').addEventListener('close',()=>{
  if($('#customizeDialog').open)return;
  clearTimeout(modalCloseTimer);$('#customizeDialog').classList.remove('is-open','is-closing');
});
$('#customizeForm').addEventListener('submit',async e=>{
  e.preventDefault();if(customizeSaving)return;
  const widgets={};document.querySelectorAll('[data-card]').forEach(el=>{widgets[el.dataset.card]=el.checked;});
  const changes={primary_owner:$('#primaryOwnerInput').value.trim(),target_hours:Number($('#targetInput').value),session_gap_hours:Number($('#groupingInput').value),timezone_mode:$('#timezoneMode').value,timezone:$('#timezoneInput').value.trim(),source_order:[...draftOrder],widgets};
  customizeSaving=true;
  const controls=[...$('#customizeForm').querySelectorAll('input,select,textarea,button')].filter(el=>el.id!=='closeCustomize');
  const disabledBefore=new Map(controls.map(el=>[el,el.disabled]));
  controls.forEach(el=>el.disabled=true);
  for(const id of ['saveCustomize','customizeBtn','customizeNav'])$('#'+id).disabled=true;
  $('#customizeForm').setAttribute('aria-busy','true');$('#settingsProgress').textContent='Saving preferences…';$('#settingsError').textContent='';
  try {
    await api('/api/settings',changes);await refresh();
    closeCustomize();message('Dashboard preferences saved on this Mac.');
  }catch(error){
    if($('#customizeDialog').open)$('#settingsError').textContent=error.message;else message(error.message,true);
  }finally{
    customizeSaving=false;disabledBefore.forEach((disabled,el)=>{el.disabled=disabled;});for(const id of ['saveCustomize','customizeBtn','customizeNav'])$('#'+id).disabled=false;
    $('#customizeForm').setAttribute('aria-busy','false');$('#settingsProgress').textContent='';
    if(pendingModalFocus){
      pendingModalFocus=false;
      const active=document.activeElement;
      if(modalOpener?.isConnected && (!active || active===document.body || active===$('#overview') || active.closest?.('#customizeDialog')))modalOpener.focus();
    }
  }
});
$('#importFile').addEventListener('change',async e=>{
  const file=e.target.files[0];if(!file||busy)return;
  if(file.size>256*1024*1024){message('Choose an export smaller than 256 MiB.',true);e.target.value='';return;}
  busy=true;$('#importFile').disabled=true;$('.import-label').setAttribute('aria-disabled','true');
  message('Reading your export locally… Only sleep records will be retained.');
  try{const imported=await api('/api/import',file,true);$('#startDate').value='';$('#endDate').value='';page=0;selectedId=null;syncRangeButtons('all');periodRequest++;await refresh();message(`Imported ${imported.records.toLocaleString()} sleep intervals locally. ${imported.stats.exact_duplicates} exact duplicates removed. Temporary export copy discarded.${imported.stats.unsupported_sleep_values?` ${imported.stats.unsupported_sleep_values} unsupported sleep values skipped.`:''}`);}catch(error){message(error.message+' Your previous dataset is still available.',true);}finally{busy=false;$('#importFile').disabled=false;$('.import-label').removeAttribute('aria-disabled');e.target.value='';}
});
document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',async()=>{
  if(!state)return;
  const intent=++periodRequest;
  try{
    const all=await api('/api/state');
    if(intent!==periodRequest)return;
    if(button.dataset.range==='all'){$('#startDate').value='';$('#endDate').value='';}
    else{const end=all.date_bounds.end;if(!end)return;const start=new Date(`${end}T12:00:00Z`);start.setUTCDate(start.getUTCDate()-Number(button.dataset.range)+1);$('#startDate').value=start.toISOString().slice(0,10);$('#endDate').value=end;}
    page=0;syncRangeButtons(button.dataset.range);
    if(await refresh())message('');
  }catch(error){if(intent===periodRequest)message(error.message,true);}
}));
for(const id of ['startDate','endDate'])$('#'+id).addEventListener('change',async()=>{
  const intent=++periodRequest;page=0;syncRangeButtons(null);
  try{if(await refresh())message('');}catch(error){if(intent===periodRequest)message(error.message,true);}
});
refresh().catch(e=>message(`Could not load the local dashboard: ${e.message}`,true));

// Refresh on-file updates only while visible and outside an active edit. No external polling.
setInterval(async () => {
  if (!state || busy || document.visibilityState !== 'visible' || $('#customizeDialog').open || document.activeElement?.closest('.annotation-form')) return;
  try { await refresh({background:true}); } catch (error) { message(error.message,true); }
},60000);

// Watch view is independent of legacy sleep demos/imports. Every request stays loopback.
let watchState, watchBusy = false, watchRequest = 0;
function watchNotice(text, error=false) {
  $('#watchNotice').textContent = text;
  $('#watchNotice').classList.toggle('error', error);
}
async function refreshWatch() {
  const params = new URLSearchParams({timezone: $('#watchTimezone').value.trim() || 'UTC'});
  const request = ++watchRequest;
  const incoming = await api(`/api/watch?${params}`);
  if (request !== watchRequest) return;
  watchState = incoming;
  renderWatch();
}
const watchUnits = {
  heart_rate:'bpm', resting_heart_rate:'bpm', hrv_sdnn:'ms', step_count:'steps',
  active_energy:'kcal', apple_exercise_time:'min', respiratory_rate:'breaths/min',
  oxygen_saturation:'%', sleeping_wrist_temperature:'°C', vo2_max:'mL/kg/min'
};
function displayedWatchValue(row) {
  if(row.value==null)return null;
  return row.type==='oxygen_saturation' ? row.value*100 : row.value;
}
function watchValueText(row) {
  const value=displayedWatchValue(row);
  return value==null ? 'Unknown' : Number(value).toLocaleString(undefined,{maximumFractionDigits:2});
}
function renderWatchTrend() {
  const w=watchState, typeSelect=$('#watchTrendType'), sourceSelect=$('#watchTrendSource');
  if(!w)return;
  const metrics=w.catalog.filter(item=>item.type!=='sleep_analysis');
  const previousType=typeSelect.value;
  typeSelect.innerHTML=metrics.map(item=>`<option value="${esc(item.type)}">${esc(item.label)}</option>`).join('');
  typeSelect.value=metrics.some(m=>m.type===previousType) ? previousType : (w.metrics.find(r=>r.type==='resting_heart_rate')?.type || w.metrics[0]?.type || metrics[0]?.type || '');
  const type=typeSelect.value, rows=w.metrics.filter(row=>row.type===type);
  const sources=new Map();
  rows.forEach(row=>sources.set(row.source_id,row.source));
  const previousSource=sourceSelect.value;
  sourceSelect.innerHTML=[...sources].map(([id,source])=>`<option value="${esc(id)}">${esc(source?.name || 'Recorded source')} · ${esc(id.slice(0,6))}</option>`).join('') || '<option value="">No source with records</option>';
  sourceSelect.value=sources.has(previousSource) ? previousSource : ([...sources.keys()][0] || '');
  sourceSelect.disabled=!sources.size;
  const selected=rows.filter(row=>row.source_id===sourceSelect.value).sort((a,b)=>a.date.localeCompare(b.date));
  const metric=metrics.find(item=>item.type===type);
  $('#watchTrendTitle').textContent=metric ? `${metric.label} over time` : 'Recorded measurements over time';
  if(!selected.length){
    $('#watchTrend').innerHTML='<p class="empty-state">No visible records for this metric. Choose another metric or import a companion batch. Empty reads and missing days remain unknown.</p>';
    return;
  }
  const epoch=day=>new Date(`${day}T12:00:00Z`).getTime()/86400000;
  const first=epoch(selected[0].date), last=epoch(selected.at(-1).date);
  const valid=selected.filter(row=>displayedWatchValue(row)!=null);
  if(!valid.length){
    $('#watchTrend').innerHTML='<p class="empty-state">These daily values are unknown because their samples overlap or cross a day boundary. No trend is inferred.</p>';
    return;
  }
  const values=valid.map(displayedWatchValue), min=Math.min(...values), max=Math.max(...values);
  const pad=Math.max((max-min)*.15,max===min ? Math.max(Math.abs(max)*.05,1) : .5);
  const lower=type==='sleeping_wrist_temperature' ? min-pad : Math.max(0,min-pad), upper=max+pad;
  const width=Math.max(700,($('#watchTrend').clientWidth || 700)-20),height=230,left=58,right=20,top=20,bottom=174;
  const x=row=>left+(width-left-right)*(epoch(row.date)-first)/Math.max(1,last-first);
  const y=value=>bottom-(bottom-top)*(value-lower)/Math.max(.001,upper-lower);
  const unit=watchUnits[type] || valid[0].unit;
  let svg=`<svg style="min-width:${width}px" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(metric?.label || type)} in ${esc(unit)}, one source. Missing or unknown dates break the line."><title>${esc(metric?.label || type)} · ${esc(unit)}</title>`;
  for(let i=0;i<4;i++){
    const value=lower+(upper-lower)*i/3, position=y(value);
    svg+=`<line class="grid" x1="${left}" y1="${position}" x2="${width-right}" y2="${position}"/><text x="${left-10}" y="${position+4}" text-anchor="end">${esc(value.toLocaleString(undefined,{maximumFractionDigits:1}))}</text>`;
  }
  let previous=null;
  selected.forEach(row=>{
    const value=displayedWatchValue(row);
    if(value==null){previous=null;return;}
    if(previous && epoch(row.date)-epoch(previous.date)===1){
      svg+=`<path class="value-line" d="M${x(previous)},${y(displayedWatchValue(previous))} L${x(row)},${y(value)}"/>`;
    }
    svg+=`<circle class="value-point" cx="${x(row)}" cy="${y(value)}" r="4"><title>${esc(row.date)} · ${esc(watchValueText(row))} ${esc(unit)} · ${row.sample_count} samples</title></circle>`;
    previous=row;
  });
  const dates=[...new Set(selected.map(r=>r.date))], step=Math.max(1,Math.ceil(dates.length/7));
  dates.forEach((date,index)=>{
    if(index%step===0 || index===dates.length-1){
      const position=x({date});svg+=`<text x="${position}" y="${bottom+26}" text-anchor="${index===0?'start':index===dates.length-1?'end':'middle'}">${esc(dateLabel(date))}</text>`;
    }
  });
  svg+='</svg>';
  $('#watchTrend').innerHTML=svg+`<p class="chart-note">${esc(selected[0].date)} to ${esc(selected.at(-1).date)} · ${esc(unit)} · ${esc(selected[0].source?.name || 'Recorded source')}. ${valid[0].aggregation==='sample_mean'?'Recorded sample means':'Recorded nonoverlapping sample sums'}; day coverage remains unknown. The labeled axis is fitted to these values. Missing days are not joined.</p>`;
}
$('#watchTrendType').addEventListener('change',()=>{ $('#watchTrendSource').value='';renderWatchTrend(); });
$('#watchTrendSource').addEventListener('change',renderWatchTrend);
function renderWatch() {
  const w = watchState, sync = w.sync;
  const labels = new Map(w.catalog.map(item => [item.type,item.label]));
  $('#watchMode').textContent = w.mode === 'synthetic' ? 'Synthetic Watch data' : w.mode === 'device' ? 'Local companion data' : 'No Watch data';
  const stamp = value => value ? new Date(value).toLocaleString() : 'never';
  $('#watchSync').textContent = `Last successful local batch: ${stamp(sync.last_success)}. Last attempt: ${stamp(sync.last_attempt)}. Destination disabled · ${sync.pending_outbox ?? 0} pending change manifests.` + (sync.last_error ? ` Latest attempt: ${sync.last_error}. Previous data remains available.` : '');
  const s = w.sleep?.summary;
  const unavailable = w.read_states.filter(r => r.read_state === 'error' || r.read_state === 'unknown').length;
  $('#watchSleep').innerHTML = s ? `<article><small>Watch sleep · average per included session</small><strong>${esc(duration(s.average_minutes))}</strong><small>${s.nights} included sessions</small></article><article><small>Bedtime regularity · selected clock spread</small><strong>${s.bedtime_spread == null ? '—' : `± ${Math.round(s.bedtime_spread)} min`}</strong><small>Descriptive; missing sessions stay unknown</small></article><article><small>Average estimated stages</small><strong>${['deep','core','rem'].map(k => `${k[0].toUpperCase()+k.slice(1)} ${s.stage_averages[k] == null ? '—' : `${Math.round(s.stage_averages[k])}m`}`).join(' · ')}</strong><small>${s.stage_sessions} sessions with specific stages</small></article>` : '';
  $('#watchRows').innerHTML = w.metrics.length ? w.metrics.map(row => `<tr><td>${esc(row.date)}</td><td>${esc(labels.get(row.type) || row.type)}</td><td><strong>${esc(watchValueText(row))} ${esc(watchUnits[row.type] || row.unit)}</strong><small>${row.overlap ? 'Overlapping samples: total unknown' : row.boundary_ambiguous ? 'Crosses midnight: daily allocation unknown' : row.aggregation === 'sample_mean' ? 'Recorded sample mean; day coverage unknown' : 'Nonoverlapping sample sum; day coverage unknown'}</small></td><td>${esc(row.source?.name || row.source_name || row.source_bundle_id)}<small>Source ${esc((row.source_id || '').slice(0,6))}</small></td><td>${esc(row.sample_count)}</td></tr>`).join('') : '<tr><td colspan="5">No visible Watch measurements. A missing or empty read is not evidence of a healthy value.</td></tr>';
  $('#watchCoverage').textContent = `Daily grouping: ${w.sleep.settings.timezone}. Retained history: ${sync.retention_days} days. Day coverage remains unknown.` + (w.metrics_truncated || w.sleep?.truncated || w.read_states_truncated ? ' The view is bounded; some rows are omitted.' : '') + (unavailable ? ` ${w.read_states_truncated ? 'At least ' : ''}${unavailable} saved metric queries have failed or unknown read visibility; inspect their state below.` : '') + (w.historical_timezone_unknown_samples ? ` Historical local timezone is unavailable for ${w.historical_timezone_unknown_samples} samples; their timestamp reference is UTC.` : '');
  renderWatchTrend();
  $('#watchCatalog').innerHTML = `<div class="watch-catalog">${w.catalog.map(item => `<div><strong>${esc(item.label || item.type)}</strong><small>${esc(watchUnits[item.type] || item.unit)} · availability depends on device, region and settings</small><small>${w.read_states.filter(r => r.type === item.type).map(r => `${esc(r.source_bundle_id)}: ${esc(r.read_state)} as of ${esc(stamp(r.last_attempt))}${r.error ? ` (${esc(r.error)})` : ''}`).join(' · ') || 'Not queried'} · coverage unknown</small></div>`).join('')}</div>`;
}
async function watchAction(action) {
  if (watchBusy) return;
  watchBusy = true;$('#watchFile').disabled=true;
  for (const id of ['watchRefresh','watchRetry']) $('#'+id).disabled = true;
  $('label[for="watchFile"]').setAttribute('aria-disabled','true');
  try { await action(); } catch (error) { watchNotice(error.message, true); }
  finally { watchBusy = false;$('#watchFile').disabled=false; for (const id of ['watchRefresh','watchRetry']) $('#'+id).disabled = false; $('label[for="watchFile"]').removeAttribute('aria-disabled'); }
}
$('#watchRefresh').addEventListener('click', () => watchAction(async () => { await refreshWatch(); watchNotice('Local view refreshed. No phone access or upload was performed.'); }));
$('#watchRetry').addEventListener('click', () => watchAction(async () => { await api('/api/watch/retry',{}); await refreshWatch(); watchNotice('Local retry request saved. The phone bridge and upload destination are not configured; no data was transmitted.'); }));
$('#watchFile').addEventListener('change', event => {
  const file = event.target.files[0];
  if (!file || watchBusy) { event.target.value = ''; return; }
  watchAction(async () => {
    if (file.size > 1024 * 1024) throw new Error('Choose a companion batch smaller than 1 MiB.');
    if (!state?.token) throw new Error('Reload the local dashboard before importing a companion batch.');
    await api('/api/watch/import',file,true);
    await refreshWatch();
    watchNotice('Companion batch committed locally. The badge identifies synthetic or device data. No upload was performed.');
  }).finally(() => { event.target.value = ''; });
});
refreshWatch().catch(error => watchNotice(`Could not load Watch modules: ${error.message}`,true));
setInterval(() => {
  if (!watchBusy && document.visibilityState === 'visible' && document.activeElement !== $('#watchTimezone')) refreshWatch().catch(error => watchNotice(error.message,true));
},60000);

// Recompute SVG coordinate space on resize; no data reload or metric animation.
let chartResizeFrame;
window.addEventListener('resize',()=>{
  cancelAnimationFrame(chartResizeFrame);
  chartResizeFrame=requestAnimationFrame(()=>{
    if(state)renderTrend();
    if(watchState)renderWatchTrend();
  });
});
