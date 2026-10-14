// Actual app.js exercised in a minimal DOM harness with invented API replies.
// This is behavioral verification, not a browser/layout/keyboard audit.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const markup = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));
const summary = {nights:0, excluded:0, average_minutes:null, target_met:0,
  bedtime_spread:null, mean_bedtime:null, zones:[], gap_minutes:null,
  stage_averages:{deep:null, core:null, rem:null}, stage_sessions:0, stage_incomplete:0};
function sleep() {return {mode:'demo', summary:copy(summary), nights:[], sources:[], token:'synthetic-token',
  date_bounds:{start:'2031-03-01',end:'2031-03-28'}, import_stats:{exact_duplicates:0},
  settings:{target_hours:8,session_gap_hours:4,timezone_mode:'fixed',timezone:'UTC',primary_owner:'You',protected_windows:[],source_order:[],annotations:{},
    widgets:{duration:true,consistency:true,stages:true,gaps:true}}};}
function watch() {return {mode:'synthetic', metrics:[], read_states:[], catalog:[
  {type:'sleep_analysis',label:'Sleep stages',unit:'category'},
  {type:'resting_heart_rate',label:'Resting heart rate',unit:'count/min'},
  {type:'step_count',label:'Steps',unit:'count'},
  {type:'oxygen_saturation',label:'Oxygen saturation',unit:'fraction'}],
  sleep:{summary:copy(summary),settings:{timezone:'UTC'}},
  sync:{last_success:null,last_attempt:null,last_error:null,pending_outbox:0,retention_days:90,destination:{enabled:false}}};}
function deferred() {let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const response = value => ({ok:true,json:async()=>copy(value)});
const settle = async () => {await new Promise(resolve=>setImmediate(resolve));};

class Classes {
  constructor(value=''){this.values=new Set(value.split(/\s+/).filter(Boolean));}
  add(...names){names.forEach(n=>this.values.add(n));}
  remove(...names){names.forEach(n=>this.values.delete(n));}
  contains(name){return this.values.has(name);}
  toggle(name,force){const enable=force??!this.contains(name);enable?this.add(name):this.remove(name);return enable;}
}
class Element {
  constructor(h,id,tag='div',classes=''){this.h=h;this.id=id;this.tag=tag;this.classList=new Classes(classes);
    this.attrs={};this.dataset={};this.listeners={};this.disabled=false;this.checked=false;this.hidden=false;
    this.isConnected=true;this.open=false;this.options=[];this._value='';this.style={};this.offsetWidth=100;}
  set value(value){this._value=String(value);}
  get value(){return this._value;}
  set innerHTML(value){for(const m of (this._html??'').matchAll(/\bid="([^"]+)"/g)){const old=this.h.nodes.get(m[1]);if(old)old.isConnected=false;}this._html=String(value);
    if(this.tag==='select') {this.options=[...this._html.matchAll(/<option value="([^"]*)"/g)].map(m=>m[1]);this._value=this.options[0]??'';}
    for(const m of this._html.matchAll(/\bid="([^"]+)"/g))this.h.nodes.set(m[1],new Element(this.h,m[1]));
  }
  get innerHTML(){return this._html??'';}
  setAttribute(name,value){this.attrs[name]=String(value);}
  getAttribute(name){return this.attrs[name]??null;}
  removeAttribute(name){delete this.attrs[name];}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  async emit(name,extra={}){const event={currentTarget:this,target:this,preventDefault(){},...extra};
    await Promise.all((this.listeners[name]??[]).map(fn=>fn(event)));}
  querySelectorAll(selector){if(this.id==='customizeForm' && selector==='input,select,textarea,button')return ['primaryOwnerInput','targetInput','groupingInput','timezoneMode','timezoneInput','saveCustomize','closeCustomize'].map(id=>this.h.nodes.get(id)).concat(this.h.cards);return [];}
  querySelector(){return new Element(this.h,'submit','button');}
  closest(){return null;}
  focus(){if(!this.disabled)this.h.document.activeElement=this;}
  showModal(){if(this.open)throw new Error('Already open');this.open=true;this.h.nodes.get('targetInput').focus();}
  close(){this.open=false;this.h.document.activeElement=this.h.document.body;this.h.closeEvents.push(()=>this.emit('close'));}
}
function harness({reduced=false,initialSleep,initialWatch}={}) {
  const h={nodes:new Map(),requests:[],queue:[],timers:new Map(),closeEvents:[],nextTimer:0,sleep:initialSleep || sleep(),watch:initialWatch || watch()};
  for(const m of markup.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
    const el=new Element(h,m[3],m[1],m[2].match(/class="([^"]*)"/)?.[1]??'');
    el.value=m[2].match(/value="([^"]*)"/)?.[1]??'';h.nodes.set(m[3],el);
  }
  h.range=['7','30','all'].map(value=>{const el=new Element(h,'range'+value,'button',value==='all'?'selected':'');el.dataset.range=value;return el;});
  h.cards=['duration','consistency','stages','gaps'].map(value=>{const el=new Element(h,'card'+value,'input');el.dataset.card=value;return el;});
  h.widgets=h.cards.map(el=>{const widget=new Element(h,'widget'+el.dataset.card);widget.dataset.widget=el.dataset.card;return widget;});
  h.trend=new Element(h,'trend-panel');h.importLabel=new Element(h,'sleep-import','label');h.watchLabel=new Element(h,'watch-import','label');
  h.document={visibilityState:'visible',activeElement:null,body:new Element(h,'body','body'),querySelector(selector){
    if(selector.startsWith('#'))return h.nodes.get(selector.slice(1));
    if(selector==='.trend-panel')return h.trend;
    if(selector==='.import-label')return h.importLabel;
    if(selector==='label[for="watchFile"]')return h.watchLabel;
    throw new Error('Unsupported synthetic selector '+selector);
  },querySelectorAll(selector){return selector==='[data-range]'?h.range:selector==='[data-card]'?h.cards:selector==='[data-widget]'?h.widgets:[];}};
  h.context=vm.createContext({document:h.document,console,URLSearchParams,Intl,Date,Map,Set,
    matchMedia:()=>({matches:reduced}),getComputedStyle:()=>({getPropertyValue:()=> '150ms'}),
    setTimeout(fn){const id=++h.nextTimer;h.timers.set(id,fn);return id;},clearTimeout(id){h.timers.delete(id);},setInterval(){},window:{addEventListener(){}},cancelAnimationFrame(){},requestAnimationFrame(){},
    fetch:async(url,options)=>{h.requests.push({url,options});const next=h.queue.shift();
      if(next)return next(url,options);
      return response(url.startsWith('/api/watch')?h.watch:h.sleep);}});
  vm.runInContext(app,h.context);
  h.run=code=>vm.runInContext(code,h.context);
  h.flushTimers=()=>{const pending=[...h.timers.values()];h.timers.clear();pending.forEach(fn=>fn());};
  h.flushCloseEvents=async()=>{for(const fn of h.closeEvents.splice(0))await fn();};
  return h;
}

test('rapid period requests keep the last choice and pressed state',async()=>{
  const h=harness();await settle();const first=deferred(),second=deferred();
  h.queue.push(()=>first.promise,()=>second.promise);
  const seven=h.range[0].emit('click'),thirty=h.range[1].emit('click');
  second.resolve(response(h.sleep));await thirty;
  first.resolve(response(h.sleep));await seven;
  assert.equal(h.nodes.get('startDate').value,'2031-02-27');
  assert.equal(h.range[1].getAttribute('aria-pressed'),'true');
  assert.equal(h.range[0].getAttribute('aria-pressed'),'false');
});
test('manual dates invalidate an older pending preset',async()=>{
  const h=harness();await settle();const old=deferred();h.queue.push(()=>old.promise);
  const preset=h.range[0].emit('click');h.nodes.get('startDate').value='2031-03-20';
  await h.nodes.get('startDate').emit('change');old.resolve(response(h.sleep));await preset;
  assert.equal(h.nodes.get('startDate').value,'2031-03-20');
  assert.ok(h.range.every(el=>el.getAttribute('aria-pressed')==='false'));
});
test('stale failed refresh cannot overwrite a newer successful request',async()=>{
  const h=harness();await settle();const old=deferred();h.queue.push(()=>old.promise);
  const pending=h.run('refresh()');assert.equal(await h.run('refresh()'),true);
  old.resolve({ok:false,json:async()=>({error:'invented stale failure'})});
  assert.equal(await pending,false);assert.equal(h.trend.getAttribute('aria-busy'),'false');
});
test('interrupted close and queued old close event cannot hide a reopened dialog',async()=>{
  const h=harness();await settle();const opener=h.nodes.get('customizeBtn');opener.focus();
  h.run('openCustomize();closeCustomize();openCustomize()');h.flushTimers();
  const modal=h.nodes.get('customizeDialog');assert.equal(modal.open,true);assert.ok(modal.classList.contains('is-open'));
  h.run('closeCustomize();closeCustomize()');assert.equal(h.timers.size,1);h.flushTimers();
  assert.equal(modal.open,false);assert.equal(h.document.activeElement,opener);
  h.run('openCustomize()');await h.flushCloseEvents();assert.ok(modal.classList.contains('is-open'));
});
test('reduced motion closes immediately without a delayed timer',async()=>{
  const h=harness({reduced:true});await settle();h.run('openCustomize();closeCustomize()');
  assert.equal(h.nodes.get('customizeDialog').open,false);assert.equal(h.timers.size,0);
});
test('pending settings save is single-flight and cannot reopen before settling',async()=>{
  const h=harness();await settle();h.run('openCustomize()');const post=deferred();h.queue.push(()=>post.promise);
  const save=h.nodes.get('customizeForm').emit('submit');await h.nodes.get('customizeForm').emit('submit');
  assert.equal(h.requests.filter(r=>r.options?.method==='POST').length,1);
  assert.equal(h.nodes.get('customizeBtn').disabled,true);assert.equal(h.nodes.get('targetInput').disabled,true);assert.equal(h.nodes.get('closeCustomize').disabled,false);
  h.run('closeCustomize()');h.flushTimers();h.run('openCustomize()');assert.equal(h.nodes.get('customizeDialog').open,false);
  post.resolve(response({ok:true}));await save;
  assert.equal(h.nodes.get('customizeBtn').disabled,false);assert.equal(h.nodes.get('targetInput').disabled,false);assert.equal(h.nodes.get('customizeForm').getAttribute('aria-busy'),'false');
});
test('successful reduced-motion save restores focus after reenabling its opener',async()=>{
  const h=harness({reduced:true});await settle();const opener=h.nodes.get('customizeBtn');opener.focus();h.run('openCustomize()');
  await h.nodes.get('customizeForm').emit('submit');
  assert.equal(h.nodes.get('customizeDialog').open,false);assert.equal(opener.disabled,false);
  assert.equal(h.document.activeElement,opener);assert.equal(h.timers.size,0);
});
test('closing during a pending save defers opener focus until the save settles',async()=>{
  const h=harness();await settle();const opener=h.nodes.get('customizeNav');opener.focus();h.run('openCustomize()');
  const post=deferred();h.queue.push(()=>post.promise);const save=h.nodes.get('customizeForm').emit('submit');
  h.run('closeCustomize()');h.flushTimers();
  assert.equal(opener.disabled,true);assert.equal(h.document.activeElement,h.nodes.get('overview'));
  post.resolve(response({ok:true}));await save;assert.equal(h.document.activeElement,opener);
});
test('settled save does not steal focus after the user leaves the closed dialog',async()=>{
  const h=harness({reduced:true});await settle();const opener=h.nodes.get('customizeBtn');opener.focus();h.run('openCustomize()');
  const post=deferred();h.queue.push(()=>post.promise);const save=h.nodes.get('customizeForm').emit('submit');h.run('closeCustomize()');
  const chosen=h.nodes.get('startDate');chosen.focus();
  post.resolve({ok:false,json:async()=>({error:'Invented save failure'})});await save;
  assert.equal(h.document.activeElement,chosen);assert.equal(opener.disabled,false);
  assert.equal(h.nodes.get('status').textContent,'Invented save failure');
});
test('Watch chart preserves measured zero and source isolation without bridging missing days',async()=>{
  const h=harness();await settle();const row=(date,value,source_id='sourceA')=>({date,value,source_id,type:'step_count',unit:'count',sample_count:1,aggregation:'individual_sample_sum',source:{name:'Invented source'}});
  h.watch.metrics=[row('2031-03-01',0),row('2031-03-03',100),row('2031-03-04',null),row('2031-03-05',200),row('2031-03-01',9000,'sourceB')];
  h.context.invented=h.watch;h.run('watchState=invented;renderWatch()');h.nodes.get('watchTrendType').value='step_count';await h.nodes.get('watchTrendType').emit('change');const chart=h.nodes.get('watchTrend').innerHTML;
  assert.match(chart,/0 steps/);assert.doesNotMatch(chart,/9000/);assert.doesNotMatch(chart,/class="value-line"/);
  assert.equal((chart.match(/class="value-point"/g)||[]).length,3);
});
test('oxygen display conversion leaves the wire value unchanged',async()=>{
  const h=harness();await settle();h.context.row={type:'oxygen_saturation',value:.97};
  assert.equal(h.run('displayedWatchValue(row)'),97);assert.equal(h.context.row.value,.97);
});

test('full generated synthetic response boots all sleep and Watch surfaces',async()=>{
  const invented=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/ui-synthetic-response.json'),'utf8'));
  const h=harness({initialSleep:invented.sleep,initialWatch:invented.watch});await settle();
  assert.ok(!h.nodes.get('status').classList.contains('error'));
  assert.ok(!h.nodes.get('watchNotice').classList.contains('error'));
  assert.match(h.nodes.get('trendChart').innerHTML,/chart-bar/);
  assert.match(h.nodes.get('nightDetail').innerHTML,/annotationForm/);
  assert.match(h.nodes.get('watchTrend').innerHTML,/value-point/);
  assert.equal(h.nodes.get('sleepDataMode').textContent,'Synthetic sleep history');
  assert.equal(h.nodes.get('watchMode').textContent,'Synthetic Watch data');
  h.run('openCustomize()');assert.equal(h.nodes.get('customizeDialog').open,true);
});

test('background response does not erase a note editor entered while fetch was pending',async()=>{
  const h=harness();await settle();const pending=deferred();h.queue.push(()=>pending.promise);
  const refresh=h.run('refresh({background:true})');
  h.document.activeElement={closest:()=>({})};pending.resolve(response({...h.sleep,summary:{...h.sleep.summary,average_minutes:999}}));
  assert.equal(await refresh,false);assert.equal(h.run('state.summary.average_minutes'),null);
});

test('repeated Watch actions stay single-flight and restore import controls on failure',async()=>{
  const h=harness();await settle();const pending=deferred();h.queue.push(()=>pending.promise);
  const action=h.nodes.get('watchRefresh').emit('click');await h.nodes.get('watchRetry').emit('click');
  assert.equal(h.nodes.get('watchFile').disabled,true);
  assert.equal(h.requests.filter(r=>r.options?.method==='POST').length,0);
  pending.resolve({ok:false,json:async()=>({error:'invented unavailable preview'})});await action;
  assert.equal(h.nodes.get('watchFile').disabled,false);assert.equal(h.nodes.get('watchRetry').disabled,false);
  assert.match(h.nodes.get('watchNotice').textContent,/invented unavailable/);
});

test('night-note failure after selection changes cannot enter the new note form',async()=>{
  const invented=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/ui-synthetic-response.json'),'utf8'));
  const h=harness({initialSleep:invented.sleep,initialWatch:invented.watch});await settle();
  const form=h.nodes.get('annotationForm'),pending=deferred();h.queue.push(()=>pending.promise);
  h.nodes.get('nightNote').value='Invented note';const save=form.emit('submit');await form.emit('submit');
  assert.equal(h.requests.filter(r=>r.options?.method==='POST').length,1);
  h.context.other=invented.sleep.nights[1].id;h.run('selectNight(other)');const newError=h.nodes.get('annotationError');
  pending.resolve({ok:false,json:async()=>({error:'invented note failure'})});await save;
  assert.equal(newError.textContent,undefined);assert.match(h.nodes.get('status').textContent,/invented note failure/);
});


test('primary wearer settings fill and save without exposing technical protection fields',async()=>{
  const initial=sleep();initial.settings.primary_owner='Invented wearer';
  initial.settings.protected_windows=[{start:1928448000,end:1928534400,owner:'Guest'}];
  const h=harness({initialSleep:initial});await settle();h.run('openCustomize()');
  assert.equal(h.nodes.get('primaryOwnerInput').value,'Invented wearer');
  h.nodes.get('primaryOwnerInput').value='  Current wearer  ';
  await h.nodes.get('customizeForm').emit('submit');
  const sent=JSON.parse(h.requests.find(r=>r.options?.method==='POST').options.body);
  assert.equal(sent.primary_owner,'Current wearer');
  assert.equal('protected_windows' in sent,false);
});

test('protected detail and note save use server owner with dynamic reported evidence',async()=>{
  const invented=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/ui-synthetic-response.json'),'utf8'));
  const n=invented.sleep.nights[0];
  invented.sleep.settings.primary_owner='You';
  n.owner='Invented guest';n.protected=true;n.excluded=true;n.note='Invented loan';
  n.reported_gaps=[{start:n.end,end:n.end+17*60,minutes:17,label:'Invented missing recording'}];
  n.reported_gap=n.reported_gaps[0];
  const h=harness({initialSleep:invented.sleep,initialWatch:invented.watch});await settle();
  const html=h.nodes.get('nightDetail').innerHTML;
  assert.match(html,/Protected borrowed-watch session/);assert.match(html,/Invented guest/);
  assert.match(html,/17m user-reported recording gap/);assert.match(html,/summaries for You/);
  h.nodes.get('nightOwner').value='Forged wearer';h.nodes.get('excludeNight').checked=false;
  h.nodes.get('nightNote').value='New invented context';await h.nodes.get('annotationForm').emit('submit');
  const sent=JSON.parse(h.requests.find(r=>r.options?.method==='POST').options.body);
  assert.equal(sent.annotations[n.id].owner,'Invented guest');
  assert.equal(sent.annotations[n.id].excluded,true);
  assert.equal(sent.annotations[n.id].note,'New invented context');
});

test('ordinary note save leaves owner comparison to the authoritative renderer',async()=>{
  const invented=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/ui-synthetic-response.json'),'utf8'));
  const n=invented.sleep.nights[0];n.protected=false;n.excluded=false;n.owner='You';
  invented.sleep.settings.primary_owner='You';
  const h=harness({initialSleep:invented.sleep,initialWatch:invented.watch});await settle();
  h.nodes.get('nightOwner').value='Guest';h.nodes.get('excludeNight').checked=false;
  await h.nodes.get('annotationForm').emit('submit');
  const sent=JSON.parse(h.requests.find(r=>r.options?.method==='POST').options.body);
  assert.equal(sent.annotations[n.id].owner,'Guest');assert.equal(sent.annotations[n.id].excluded,false);
});
