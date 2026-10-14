// Read-only bridge to the existing dashboard. No API, storage, notes or owner data.
export const LAYER_IDS = Object.freeze(['sessions', 'measurements', 'context', 'preferences', 'boundary']);
export const LAYER_META = Object.freeze({
  sessions: {title:'Sessions', relation:'Included sleep sessions feed the recorded sleep summary. Excluded sessions stay outside averages.', action:'Open sleep history'},
  measurements: {title:'Measurements', relation:'Sleep and Watch views have separate sources and data modes. The selected Watch metric describes recorded samples, not a clinical state.', action:'Open Watch measurements'},
  context: {title:'Context', relation:'Coverage and read visibility limit what a recording can tell you. Missing values and unclassified intervals remain unknown.', action:'Open read-state details'},
  preferences: {title:'Preferences', relation:'Your target and clock/grouping choices organize this local view. They do not change the underlying records.', action:'Open settings'},
  boundary: {title:'Local boundary', relation:'Local storage contains records, imports and preferences. Layer depth is an application relationship, not anatomy or biological causality.', action:'Open local data details'}
});
const WATCHED_IDS = Object.freeze(['sleepDataMode','scopeLabel','periodLabel','avgDuration',
  'durationCaption','targetCaption','bedtimeCaption','stageCaption','stageCoverage','gapValue',
  'excludedCaption','watchMode','watchCoverage','watchSync','watchTrendType','watchTrendSource',
  'watchTrend','customizeBtn']);
const HASH_LAYERS = {'#overview':'sessions','#history':'sessions','#watch':'measurements',
  '#watchCatalog':'context','#integration':'boundary'};
const text = node => (node?.textContent ?? '').replace(/\s+/g,' ').trim();
const shown = (doc,id,fallback='—') => text(doc.getElementById(id)) || fallback;
function selectedLabel(doc,id) {
  const node=doc.getElementById(id);
  return text(node?.selectedOptions?.[0]) || 'No selection';
}

// Only formatted aggregate/provenance leaves are read. Tables, notes and source IDs
// are deliberately outside this whitelist. A plotted value is not a latest-day claim.
export function readHealthLayers(doc) {
  const trend=doc.getElementById('watchTrend');
  const points=trend?.querySelectorAll('.value-point title') ?? [];
  const plotted=text(points[points.length-1]);
  const empty=text(trend?.querySelector('.empty-state'));
  const provenance=text(trend?.querySelector('.chart-note'));
  const destination=shown(doc,'watchSync','');
  return {
    sessions: `${shown(doc,'sleepDataMode')}. ${shown(doc,'periodLabel')}. ${shown(doc,'scopeLabel')}. Recorded average: ${shown(doc,'avgDuration')}. ${shown(doc,'durationCaption')}.`,
    measurements: `Sleep: ${shown(doc,'sleepDataMode')}; recorded average ${shown(doc,'avgDuration')}. Watch: ${shown(doc,'watchMode')}. Metric: ${selectedLabel(doc,'watchTrendType')}. Source: ${selectedLabel(doc,'watchTrendSource')}. ${plotted ? `Last plotted value: ${plotted}. ${provenance}` : empty || 'No plotted measurements yet; missing values remain unknown.'}`,
    context: `${shown(doc,'stageCaption')}. ${shown(doc,'stageCoverage')}. Unclassified within included spans: ${shown(doc,'gapValue')}. ${shown(doc,'excludedCaption')}. Watch: ${shown(doc,'watchCoverage','Read visibility is loading; inspect details below')}.`,
    preferences: `Applied sleep target: ${shown(doc,'targetCaption')}. Clock context: ${shown(doc,'bedtimeCaption')}. ${shown(doc,'watchCoverage','Daily grouping context is loading')}. Sleep grouping and clock controls are in Settings.`,
    boundary: `Data is stored on this Mac. ${/Destination disabled/i.test(destination) ? 'Upload destination disabled.' : 'Destination state: inspect local synchronization details.'} Sleep: ${shown(doc,'sleepDataMode')}. Watch: ${shown(doc,'watchMode')}. Imports are handled by the existing local controls.`
  };
}

export function createHealthOnion({doc=document,win=window}={}) {
  const root=doc.getElementById('healthOnion');
  const group=doc.getElementById('onionLayers');
  const open=doc.getElementById('onionOpen');
  const buttons=[...(group?.querySelectorAll('[data-onion-layer]') ?? [])];
  let selected='sessions', disposed=false, queued=false;
  const subscribers=new Set(), listeners=[];
  const listen=(node,type,fn) => {if(node){node.addEventListener(type,fn);listeners.push(()=>node.removeEventListener(type,fn));}};
  const set=(id,value) => {const node=doc.getElementById(id);if(node && node.textContent!==value)node.textContent=value;};
  const buttonFor=id => buttons.find(button=>button.dataset.onionLayer===id);
  const render=() => {
    if(disposed)return;
    const meta=LAYER_META[selected];
    if(root)root.dataset.selected=selected;
    for(const button of buttons)button.setAttribute('aria-pressed',String(button.dataset.onionLayer===selected));
    for(const shell of root?.querySelectorAll('[data-onion-shell]') ?? [])shell.dataset.selected=String(shell.dataset.onionShell===selected);
    set('onionTitle',meta.title);set('onionSummary',readHealthLayers(doc)[selected]);
    set('onionRelation',meta.relation);set('onionOpen',meta.action);
    if(open)open.disabled=selected==='preferences' && !!doc.getElementById('customizeBtn')?.disabled;
  };
  const select=(id,{focus=false,announce=true}={}) => {
    if(disposed || !LAYER_IDS.includes(id))return false;
    const changed=selected!==id;selected=id;render();
    if(focus)buttonFor(id)?.focus({preventScroll:true});
    if(announce && changed)set('onionSelectionStatus',`${LAYER_META[id].title} selected. ${LAYER_META[id].action} is available.`);
    if(changed)for(const callback of subscribers)callback(selected);
    return true;
  };
  const queue=() => {
    if(queued || disposed)return;
    queued=true;const flush=()=>{queued=false;render();};
    if(win.queueMicrotask)win.queueMicrotask(flush);else queueMicrotask(flush);
  };
  const focusTarget=node => {
    if(!node)return false;
    const temporary=!node.hasAttribute('tabindex') && !/^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(node.tagName);
    if(temporary){node.setAttribute('tabindex','-1');node.addEventListener('blur',()=>node.removeAttribute('tabindex'),{once:true});}
    node.scrollIntoView?.({block:'start',behavior:'auto'});node.focus({preventScroll:true});return true;
  };
  const openSection=() => {
    if(selected==='preferences') {
      const trigger=doc.getElementById('customizeBtn');
      if(trigger && !trigger.disabled){trigger.click();if(doc.getElementById('customizeDialog')?.open)return;}
      set('onionSelectionStatus','Settings are unavailable while the local view loads or saves.');return;
    }
    const targetId={sessions:'history',measurements:'watch',context:'watchCatalog',boundary:'integration'}[selected];
    const section=doc.getElementById(targetId);
    let focus=section?.querySelector('h2') || section;
    if(selected==='context') {
      const details=section?.closest('details');
      if(details){details.open=true;focus=details.querySelector('summary') || section;}
    }
    if(!focusTarget(focus)){set('onionSelectionStatus','This section is unavailable. Use the dashboard navigation.');return;}
    if(win.location?.hash!==`#${targetId}`)win.history?.pushState(null,'',`#${targetId}`);
  };
  for(const button of buttons)listen(button,'click',()=>select(button.dataset.onionLayer));
  listen(group,'keydown',event=>{
    const index=buttons.indexOf(event.target);
    if(index<0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey)return;
    const key=event.key;
    let next=index;
    if(key==='ArrowRight' || key==='ArrowDown')next=(index+1)%buttons.length;
    else if(key==='ArrowLeft' || key==='ArrowUp')next=(index+buttons.length-1)%buttons.length;
    else if(key==='Home')next=0;
    else if(key==='End')next=buttons.length-1;
    else return;
    event.preventDefault();buttons[next].focus({preventScroll:true});
  });
  listen(open,'click',openSection);
  listen(doc,'click',event=>{
    const trigger=event.target?.closest?.('#customizeBtn, #customizeNav');
    if(trigger && !trigger.disabled){select('preferences',{announce:false});return;}
    const anchor=event.target?.closest?.('a[href^="#"]');
    const layer=HASH_LAYERS[anchor?.getAttribute('href')];
    if(layer)select(layer,{announce:false});
  });
  const fromHash=() => {const layer=HASH_LAYERS[win.location?.hash];if(layer)select(layer,{announce:false});};
  listen(win,'hashchange',fromHash);listen(win,'popstate',fromHash);
  for(const id of ['watchTrendType','watchTrendSource'])for(const event of ['input','change'])listen(doc.getElementById(id),event,queue);
  const observer=win.MutationObserver ? new win.MutationObserver(queue) : null;
  for(const id of WATCHED_IDS){const node=doc.getElementById(id);if(node)observer?.observe(node,{childList:true,characterData:true,subtree:true,attributes:true,attributeFilter:['disabled']});}
  const dispose=() => {if(disposed)return;disposed=true;observer?.disconnect();listeners.forEach(remove=>remove());subscribers.clear();};
  listen(win,'pagehide',event=>{if(!event.persisted)dispose();});listen(win,'pageshow',queue);
  render();fromHash();
  // Programmatic control updates can explicitly refresh without fetching or
  // observing the whole application. Existing app refreshes mutate watched leaves.
  return {select,getSelected:()=>selected,refresh:queue,subscribe(callback){if(disposed)return ()=>{};subscribers.add(callback);callback(selected);return ()=>subscribers.delete(callback);},dispose};
}

// No pointer capture or preventDefault: vertical touch scrolling remains native.
export function installOnionTap({element,onPick,win=window}) {
  let start=null;
  const down=event => {
    if(event.isPrimary===false || (event.button!==undefined && event.button!==0)){start=null;return;}
    start={id:event.pointerId,x:event.clientX,y:event.clientY,time:event.timeStamp,
      scrollX:win.scrollX||0,scrollY:win.scrollY||0};
  };
  const moved=event => {if(start && event.pointerId===start.id && Math.hypot(event.clientX-start.x,event.clientY-start.y)>8)start=null;};
  const cancel=() => {start=null;};
  const up=event => {
    const first=start;start=null;
    if(!first || event.pointerId!==first.id || Math.hypot(event.clientX-first.x,event.clientY-first.y)>8 || event.timeStamp-first.time>500 || Math.abs((win.scrollY||0)-first.scrollY)>2 || Math.abs((win.scrollX||0)-first.scrollX)>2)return;
    const bounds=element.getBoundingClientRect();
    if(bounds.width<=0 || bounds.height<=0)return;
    const x=event.clientX-bounds.left,y=event.clientY-bounds.top;
    if(x<0 || y<0 || x>bounds.width || y>bounds.height)return;
    onPick({x,y,width:bounds.width,height:bounds.height});
  };
  element.addEventListener('pointerdown',down);element.addEventListener('pointermove',moved);
  element.addEventListener('pointerup',up);element.addEventListener('pointercancel',cancel);
  element.addEventListener('pointerleave',cancel);win.addEventListener('blur',cancel);
  return ()=>{cancel();element.removeEventListener('pointerdown',down);element.removeEventListener('pointermove',moved);element.removeEventListener('pointerup',up);element.removeEventListener('pointercancel',cancel);element.removeEventListener('pointerleave',cancel);win.removeEventListener('blur',cancel);};
}
