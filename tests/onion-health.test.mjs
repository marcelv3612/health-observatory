import {test} from 'node:test';
import assert from 'node:assert/strict';
import {LAYER_IDS,createHealthOnion,readHealthLayers,installOnionTap} from '../static/onion-health.mjs';

// Invented DOM leaves only. This exercises the production bridge; layout/GPU and
// browser-native keyboard semantics remain separate browser acceptance work.
class Events {
  listeners=new Map();
  addEventListener(type,fn){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn);}
  removeEventListener(type,fn){this.listeners.get(type)?.delete(fn);}
  emit(type,extra={}){const event={target:this,preventDefault(){this.prevented=true;},...extra};for(const fn of this.listeners.get(type)??[])fn(event);return event;}
}
class Node extends Events {
  constructor(doc,id,tag='DIV'){super();this.doc=doc;this.id=id;this.tagName=tag;this.textContent='';this.dataset={};this.attrs={};this.disabled=false;this.queries=new Map();}
  setAttribute(name,value){this.attrs[name]=String(value);}
  getAttribute(name){return this.attrs[name]??null;}
  hasAttribute(name){return name in this.attrs;}
  removeAttribute(name){delete this.attrs[name];}
  querySelector(query){return this.queries.get(query)??null;}
  querySelectorAll(query){return this.queries.get(query)??[];}
  closest(query){return this.queries.get('closest:'+query)??null;}
  focus(){this.doc.activeElement=this;}
  scrollIntoView(){this.scrolled=true;}
  click(){this.emit('click');this.doc.emit('click',{target:this});}
}
function harness(){
  const doc=new Events();doc.nodes=new Map();doc.reads=[];
  doc.getElementById=id=>{doc.reads.push(id);return doc.nodes.get(id)??null;};
  const node=(id,content='',tag='DIV')=>{const n=new Node(doc,id,tag);n.textContent=content;doc.nodes.set(id,n);return n;};
  const win=new Events();win.location={hash:''};win.queued=[];win.queueMicrotask=fn=>win.queued.push(fn);
  win.history={pushState(a,b,url){win.location.hash=url;}};
  win.MutationObserver=class {constructor(fn){this.fn=fn;this.observed=[];win.observer=this;}observe(n,options){this.observed.push({n,options});}disconnect(){this.disconnected=true;}};
  const leaves={sleepDataMode:'Synthetic sleep history',periodLabel:'Invented period',scopeLabel:'25 included · 0 excluded',
    avgDuration:'7h 01m',durationCaption:'Average of 25 included sleep sessions',targetCaption:'7 of 25 meet your 8h target',
    bedtimeCaption:'Typical bedtime 22:50 · UTC',stageCaption:'Recorded stage averages · 23 sessions with stages',
    stageCoverage:'2 have unclassified intervals · gaps stay unknown',gapValue:'9h 35m',excludedCaption:'0 excluded · borrowed nights never enter averages',
    watchMode:'Synthetic Watch data',watchCoverage:'Daily grouping: UTC. Day coverage remains unknown.',watchSync:'Destination disabled · 0 pending change manifests.'};
  for(const [id,content]of Object.entries(leaves))node(id,content);
  node('watchTrendType','', 'SELECT').selectedOptions=[{textContent:'Steps'}];
  node('watchTrendSource','', 'SELECT').selectedOptions=[{textContent:'Invented Watch · fakeid'}];
  const trend=node('watchTrend');trend.queries.set('.value-point title',[{textContent:'2031-03-01 · 0 steps · 1 samples'}]);
  trend.queries.set('.chart-note',{textContent:'steps · Invented Watch. Nonoverlapping sums; coverage unknown.'});
  const root=node('healthOnion'),group=node('onionLayers');
  const buttons=LAYER_IDS.map(id=>{const b=node('button-'+id,'','BUTTON');b.dataset.onionLayer=id;return b;});
  const shells=LAYER_IDS.map(id=>{const shell=node('shell-'+id);shell.dataset.onionShell=id;return shell;});
  group.queries.set('[data-onion-layer]',buttons);root.queries.set('[data-onion-shell]',shells);
  for(const id of ['onionTitle','onionSummary','onionRelation','onionSelectionStatus'])node(id);
  node('onionOpen','','BUTTON');const settings=node('customizeBtn','','BUTTON'),dialog=node('customizeDialog');dialog.open=false;
  settings.queries.set('closest:#customizeBtn, #customizeNav',settings);
  settings.addEventListener('click',()=>{dialog.opener=doc.activeElement;dialog.open=true;});
  for(const id of ['history','watch','integration']){const section=node(id);section.queries.set('h2',node(id+'Title','','H2'));}
  const catalog=node('watchCatalog'),details=node('readDetails','', 'DETAILS'),summary=node('readSummary','','SUMMARY');
  catalog.queries.set('closest:details',details);details.queries.set('summary',summary);
  const flush=()=>{for(const fn of win.queued.splice(0))fn();};
  const bridge=createHealthOnion({doc,win});
  return {doc,win,node,root,group,buttons,shells,bridge,flush,get:id=>doc.nodes.get(id)};
}
test('aggregate binding preserves independent data modes, units, measured zero and uncertainty',()=>{
  const h=harness(),state=readHealthLayers(h.doc);
  assert.match(state.sessions,/25 included · 0 excluded.*7h 01m/);
  assert.match(state.measurements,/Sleep: Synthetic sleep history.*Watch: Synthetic Watch data/);
  assert.match(state.measurements,/Metric: Steps.*Source: Invented Watch.*Last plotted value: 2031-03-01 · 0 steps/);
  assert.match(state.measurements,/coverage unknown/);assert.match(state.context,/2 have unclassified intervals.*9h 35m/);
  assert.match(state.boundary,/Upload destination disabled/);
  assert(!h.doc.reads.some(id=>['historyRows','nightDetail','sourceOrder','targetInput','watchRows','watchCatalog'].includes(id)));
  h.bridge.dispose();
});
test('empty and unknown modes never become a measured zero or retain an old plotted value',()=>{
  const h=harness();h.get('scopeLabel').textContent='0 included · 3 excluded';h.get('avgDuration').textContent='—';
  h.get('watchMode').textContent='No Watch data';h.get('watchTrend').queries.set('.value-point title',[]);
  h.get('watchTrend').queries.set('.empty-state',{textContent:'These daily values are unknown. No trend is inferred.'});
  const state=readHealthLayers(h.doc);assert.match(state.sessions,/0 included · 3 excluded.*average: —/);
  assert.match(state.measurements,/No Watch data.*unknown/);assert(!state.measurements.includes('0 steps'));
  h.get('watchSync').textContent='Loading local synchronization state…';assert(!readHealthLayers(h.doc).boundary.includes('Upload destination disabled'));
  h.bridge.dispose();
});
test('selection is shared by native buttons and picking; inspection never navigates',()=>{
  const h=harness(),seen=[];const off=h.bridge.subscribe(id=>seen.push(id));
  h.buttons[2].click();assert.equal(h.bridge.getSelected(),'context');assert.equal(h.win.location.hash,'');
  assert.equal(h.get('readDetails').open,undefined);assert.equal(h.root.dataset.selected,'context');
  assert.equal(h.buttons[2].getAttribute('aria-pressed'),'true');assert.equal(h.shells[2].dataset.selected,'true');
  assert.equal(h.shells[0].dataset.selected,'false');assert.match(h.get('onionRelation').textContent,/Missing values/);
  h.bridge.select('boundary',{focus:true});assert.equal(h.doc.activeElement,h.buttons[4]);assert.deepEqual(seen,['sessions','context','boundary']);
  assert.equal(h.bridge.select('invalid'),false);off();h.bridge.select('sessions');assert.equal(seen.length,3);
  h.bridge.dispose();
});
test('Open focuses real history, Watch, read-state and privacy targets; settings uses native modal trigger',()=>{
  const h=harness();
  for(const [layer,hash,target]of [['sessions','#history','historyTitle'],['measurements','#watch','watchTitle'],['context','#watchCatalog','readSummary'],['boundary','#integration','integrationTitle']]){
    h.bridge.select(layer);h.get('onionOpen').click();assert.equal(h.win.location.hash,hash);assert.equal(h.doc.activeElement,h.get(target));assert.equal(h.get(target).scrolled,true);
  }
  assert.equal(h.get('readDetails').open,true);
  h.bridge.select('preferences');h.get('onionOpen').focus();h.get('onionOpen').click();
  assert.equal(h.get('customizeDialog').open,true);assert.equal(h.get('customizeDialog').opener,h.get('onionOpen'));
  h.get('customizeBtn').disabled=true;h.get('customizeDialog').open=false;h.win.observer.fn();h.flush();
  assert.equal(h.get('onionOpen').disabled,true);h.get('onionOpen').click();assert.equal(h.get('customizeDialog').open,false);
  h.bridge.dispose();
});
test('arrow keys stay inside layer group and move focus without selecting or overriding native activation',()=>{
  const h=harness();const right=h.group.emit('keydown',{target:h.buttons[0],key:'ArrowRight'});
  assert.equal(right.prevented,true);assert.equal(h.doc.activeElement,h.buttons[1]);assert.equal(h.bridge.getSelected(),'sessions');
  h.group.emit('keydown',{target:h.buttons[1],key:'End'});assert.equal(h.doc.activeElement,h.buttons[4]);
  h.group.emit('keydown',{target:h.buttons[4],key:'Home'});assert.equal(h.doc.activeElement,h.buttons[0]);
  for(const key of ['Enter',' ','Tab'])assert(!h.group.emit('keydown',{target:h.buttons[0],key}).prevented);
  assert(!h.group.emit('keydown',{target:h.get('watchTrendType'),key:'ArrowRight'}).prevented);
  assert(!h.group.emit('keydown',{target:h.buttons[0],key:'ArrowRight',altKey:true}).prevented);h.bridge.dispose();
});
test('existing navigation, history changes and settings synchronize selection without taking focus',()=>{
  const h=harness();const anchor=h.node('nav','','A');anchor.setAttribute('href','#watch');anchor.queries.set('closest:a[href^="#"]',anchor);
  h.get('onionOpen').focus();h.doc.emit('click',{target:anchor});assert.equal(h.bridge.getSelected(),'measurements');assert.equal(h.doc.activeElement,h.get('onionOpen'));
  h.win.location.hash='#integration';h.win.emit('hashchange');assert.equal(h.bridge.getSelected(),'boundary');
  h.win.location.hash='#watchCatalog';h.win.emit('popstate');assert.equal(h.bridge.getSelected(),'context');
  h.get('customizeBtn').click();assert.equal(h.bridge.getSelected(),'preferences');h.bridge.dispose();
});
test('rendered refresh mutations are batched, include period/metric/source and never observe their own output',()=>{
  const h=harness();assert(!h.win.observer.observed.some(({n})=>n.id.startsWith('onion')));
  h.bridge.select('sessions');h.get('scopeLabel').textContent='4 included · 2 excluded';h.get('avgDuration').textContent='6h 12m';
  h.win.observer.fn();h.win.observer.fn();assert.equal(h.win.queued.length,1);h.flush();assert.match(h.get('onionSummary').textContent,/4 included · 2 excluded.*6h 12m/);
  h.bridge.select('measurements');h.get('watchTrendType').selectedOptions=[{textContent:'HRV (SDNN)'}];h.get('watchTrendSource').selectedOptions=[{textContent:'Other invented source'}];
  h.get('watchTrend').queries.set('.value-point title',[{textContent:'2031-03-02 · 35 ms · 2 samples'}]);h.get('watchTrendType').emit('change');h.get('watchTrendSource').emit('change');
  assert.equal(h.win.queued.length,1);h.flush();assert.match(h.get('onionSummary').textContent,/HRV \(SDNN\).*Other invented source.*35 ms/);h.bridge.dispose();
});
test('preferences read applied context and ignore edited unsaved settings; final disposal prevents queued updates',()=>{
  const h=harness();h.node('targetInput','','INPUT').value='12';h.bridge.select('preferences');
  assert.match(h.get('onionSummary').textContent,/8h target/);assert(!h.get('onionSummary').textContent.includes('12'));
  h.win.emit('pagehide',{persisted:true});h.get('targetCaption').textContent='Applied 9h target';h.win.emit('pageshow');h.flush();assert.match(h.get('onionSummary').textContent,/9h target/);
  h.win.observer.fn();const before=h.get('onionSummary').textContent;h.win.emit('pagehide',{persisted:false});h.get('targetCaption').textContent='Applied 10h target';h.flush();
  assert.equal(h.get('onionSummary').textContent,before);assert.equal(h.win.observer.disconnected,true);assert.equal(h.bridge.select('sessions'),false);
});
test('property-only metric selection supports input/change and explicit programmatic refresh',()=>{
  const h=harness();h.bridge.select('measurements');
  h.get('watchTrendType').selectedOptions=[{textContent:'Oxygen saturation'}];h.get('watchTrendType').emit('input');h.flush();
  assert.match(h.get('onionSummary').textContent,/Metric: Oxygen saturation/);
  h.get('watchTrendSource').selectedOptions=[{textContent:'Programmatically selected invented source'}];h.bridge.refresh();h.flush();
  assert.match(h.get('onionSummary').textContent,/Programmatically selected invented source/);h.bridge.dispose();
});
test('tap coordinates use CSS bounds and moved/canceled/scrolled/long/secondary gestures do not pick',()=>{
  const doc=new Events(),element=new Node(doc,'canvas'),win=new Events(),picks=[];
  element.getBoundingClientRect=()=>({left:100,top:50,width:300,height:200});win.scrollY=0;
  const dispose=installOnionTap({element,win,onPick:value=>picks.push(value)});
  const down={pointerId:1,clientX:150,clientY:100,button:0,isPrimary:true,timeStamp:10},up={...down,timeStamp:90};
  element.emit('pointerdown',down);element.emit('pointerup',up);assert.deepEqual(picks,[{x:50,y:50,width:300,height:200}]);
  element.emit('pointerdown',down);element.emit('pointermove',{...down,clientY:115});element.emit('pointerup',up);
  element.emit('pointerdown',down);element.emit('pointercancel');element.emit('pointerup',up);
  element.emit('pointerdown',down);win.scrollY=20;element.emit('pointerup',up);win.scrollY=0;
  element.emit('pointerdown',down);element.emit('pointerup',{...up,timeStamp:900});
  element.emit('pointerdown',{...down,button:2});element.emit('pointerup',up);
  element.emit('pointerdown',down);win.emit('blur');element.emit('pointerup',up);assert.equal(picks.length,1);
  dispose();element.emit('pointerdown',down);element.emit('pointerup',up);assert.equal(picks.length,1);
});
