import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mountCeramicOnion} from '../static/ceramic-onion.mjs';
import {installOnionTap} from '../static/onion-health.mjs';
const settle=()=>new Promise(resolve=>setImmediate(resolve));
class Events {
  listeners=new Map();attrs=new Map();dataset={};hidden=false;disabled=false;
  addEventListener(name,callback){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(callback);}
  removeEventListener(name,callback){this.listeners.get(name)?.delete(callback);}
  emit(name,event={}){for(const callback of [...(this.listeners.get(name)||[])])callback(event);}
  setAttribute(name,value){this.attrs.set(name,String(value));}removeAttribute(name){this.attrs.delete(name);}
  getBoundingClientRect(){return {left:0,top:0,width:390,height:250};}
  get listenerCount(){return [...this.listeners.values()].reduce((sum,set)=>sum+set.size,0);}
}
function harness({reduced=false,unavailable=false,load,renderFailure=false}={}){
  const ids={};for(const id of ['onionViewport','onionCanvas','onionFallback','onionMotion','onionSeparate','onionTurn'])ids[id]=new Events();
  const shells=Object.fromEntries(['sessions','measurements','context','preferences','boundary'].map(id=>{const shell=new Events();shell.dataset.onionShell=id;return [id,shell];}));ids.onionFallback.querySelectorAll=()=>Object.values(shells);
  ids.onionViewport.clientWidth=390;ids.onionViewport.clientHeight=250;
  const media=new Events();media.matches=reduced;const doc=new Events();doc.visibilityState='visible';doc.getElementById=id=>ids[id];doc.documentElement={};
  const win=new Events();win.devicePixelRatio=3;win.matchMedia=()=>media;win.getComputedStyle=()=>({getPropertyValue:()=> '250ms'});
  const observers=[];class Intersection{constructor(callback){this.callback=callback;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}emit(value){this.callback([{isIntersecting:value}]);}}
  class Resize{constructor(callback){this.callback=callback;}observe(){}disconnect(){this.disconnected=true;}}
  win.IntersectionObserver=Intersection;win.ResizeObserver=Resize;
  const frames=new Map();let next=0;win.requestAnimationFrame=callback=>{const id=++next;frames.set(id,callback);return id;};win.cancelAnimationFrame=id=>frames.delete(id);
  const subscribers=new Set(),selections=[];let selected='sessions';
  const health={getSelected:()=>selected,subscribe(callback){subscribers.add(callback);callback(selected);return ()=>subscribers.delete(callback);},select(id,options){selected=id;selections.push({id,options});for(const callback of subscribers)callback(id);}};
  const stats={imports:0,constructs:0,disposed:0,tapDisposed:0,fallbackTapDisposed:0,renders:[],selected:[],resizes:[]};let pick;
  const lifecycle=mountCeramicOnion({doc,win,health,loadThree:()=>{stats.imports++;return load?load():Promise.resolve({});},
    makeScene(){stats.constructs++;if(unavailable)throw Error('Synthetic unavailable WebGL');return {select:id=>stats.selected.push(id),resize:(...args)=>stats.resizes.push(args),render:state=>{if(renderFailure)throw Error('Synthetic context failure');stats.renders.push(state);},pick:()=> 'context',dispose:()=>stats.disposed++};},
    installTap({element,onPick}){const remove=installOnionTap({element,onPick,win});if(element===ids.onionCanvas)pick=onPick;return ()=>{remove();if(element===ids.onionCanvas){stats.tapDisposed++;pick=undefined;}else stats.fallbackTapDisposed++;};}});
  return {ids,shells,media,doc,win,frames,health,subscribers,selections,stats,lifecycle,observer:observers[0],pick(point={x:10,y:10,width:390,height:250}){pick?.(point);}};
}
test('reduced-motion entry keeps complete SVG and native selection without loading Three',async()=>{
  const h=harness({reduced:true});h.observer.emit(true);await settle();h.health.select('context');h.ids.onionSeparate.emit('click');h.ids.onionTurn.emit('click');
  assert.equal(h.stats.imports,0);assert.equal(h.ids.onionCanvas.hidden,true);assert.equal(h.ids.onionFallback.attrs.has('hidden'),false);
  assert.equal(h.ids.onionFallback.dataset.separated,'true');assert.equal(h.ids.onionFallback.dataset.turn,'1');assert.equal(h.frames.size,0);
  h.media.matches=false;h.media.emit('change');await settle();assert.equal(h.stats.constructs,1);assert.equal(h.ids.onionCanvas.hidden,false);assert.equal(h.stats.selected.at(-1),'context');
  h.lifecycle.dispose();assert.equal(h.stats.disposed,1);
});
test('canvas picking delegates to exact shared selection and deliberate focus',async()=>{
  const h=harness();h.observer.emit(true);await settle();h.pick();assert.deepEqual(h.selections.at(-1),{id:'context',options:{focus:true,announce:true}});assert.equal(h.stats.selected.at(-1),'context');h.lifecycle.dispose();
});
test('no-WebGL fallback remains usable with no GPU frame or canvas picking',async()=>{
  const h=harness({unavailable:true});h.observer.emit(true);await settle();assert.equal(h.stats.constructs,1);assert.equal(h.ids.onionCanvas.hidden,true);assert.equal(h.ids.onionMotion.textContent,'Static view');assert.equal(h.frames.size,0);h.ids.onionSeparate.emit('click');assert.equal(h.ids.onionFallback.dataset.separated,'true');h.lifecycle.dispose();
});
test('an import resolving after final pagehide cannot construct a renderer',async()=>{
  let resolve;const pending=new Promise(done=>resolve=done);const h=harness({load:()=>pending});h.observer.emit(true);h.win.emit('pagehide',{persisted:false});resolve({});await settle();assert.equal(h.stats.constructs,0);assert.equal(h.observer.disconnected,true);assert.equal(h.subscribers.size,0);assert.equal(h.frames.size,0);
});
test('hidden import waits for visible restoration and retains fallback',async()=>{
  let resolve;const pending=new Promise(done=>resolve=done);const h=harness({load:()=>pending});h.observer.emit(true);h.doc.visibilityState='hidden';h.doc.emit('visibilitychange');resolve({});await settle();assert.equal(h.stats.constructs,0);assert.equal(h.ids.onionCanvas.hidden,true);
  h.doc.visibilityState='visible';h.doc.emit('visibilitychange');await settle();assert.equal(h.stats.constructs,1);assert.equal(h.stats.imports,2);h.lifecycle.dispose();
});
test('BFCache and queued intersections never resume hidden animation or duplicate renderer ownership',async()=>{
  const h=harness();h.observer.emit(true);await settle();h.ids.onionMotion.emit('click');assert.equal(h.frames.size,1);
  h.win.emit('pagehide',{persisted:true});h.observer.emit(true);assert.equal(h.frames.size,0);h.doc.visibilityState='hidden';h.win.emit('pageshow',{persisted:true});assert.equal(h.frames.size,0);
  h.doc.visibilityState='visible';h.doc.emit('visibilitychange');assert.equal(h.frames.size,1);assert.equal(h.stats.constructs,1);h.lifecycle.dispose();
});
test('BFCache during import waits for visible pageshow',async()=>{
  let resolve;const pending=new Promise(done=>resolve=done);const h=harness({load:()=>pending});h.observer.emit(true);h.win.emit('pagehide',{persisted:true});resolve({});await settle();assert.equal(h.stats.constructs,0);
  h.doc.visibilityState='hidden';h.win.emit('pageshow',{persisted:true});await settle();assert.equal(h.stats.constructs,0);h.doc.visibilityState='visible';h.doc.emit('visibilitychange');await settle();assert.equal(h.stats.constructs,1);h.lifecycle.dispose();
});
test('context loss releases renderer and tap exactly once; controls stay on fallback',async()=>{
  const h=harness();h.observer.emit(true);await settle();h.ids.onionMotion.emit('click');let prevented=false;h.ids.onionCanvas.emit('webglcontextlost',{preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(h.stats.disposed,1);assert.equal(h.stats.tapDisposed,1);assert.equal(h.frames.size,0);assert.equal(h.ids.onionFallback.attrs.has('hidden'),false);h.ids.onionSeparate.emit('click');assert.equal(h.ids.onionFallback.dataset.separated,'true');h.lifecycle.dispose();assert.equal(h.stats.disposed,1);assert.equal(h.stats.tapDisposed,1);
});
test('render failure preserves fallback without retry loops and final cleanup removes listeners',async()=>{
  const h=harness({renderFailure:true});h.observer.emit(true);await settle();assert.equal(h.ids.onionCanvas.hidden,true);assert.equal(h.stats.disposed,1);assert.equal(h.frames.size,0);h.observer.emit(true);await settle();assert.equal(h.stats.constructs,1);h.lifecycle.dispose();assert.equal(h.win.listenerCount,0);assert.equal(h.doc.listenerCount,0);assert.equal(h.media.listenerCount,0);assert.equal(h.ids.onionSeparate.listenerCount,0);assert.equal(h.ids.onionCanvas.listenerCount,0);
});

function tap(shell,{move=false,cancel=false}={}){
  const event={pointerId:1,button:0,isPrimary:true,clientX:30,clientY:40,timeStamp:0};shell.emit('pointerdown',event);if(move)shell.emit('pointermove',{...event,clientY:60,timeStamp:20});if(cancel)shell.emit('pointercancel',event);shell.emit('pointerup',{...event,timeStamp:100});
}
test('SVG shell taps use shared selection in reduced, unavailable and lost-context states',async()=>{
  for(const configuration of [{reduced:true},{unavailable:true},{}]){
    const h=harness(configuration);h.observer.emit(true);await settle();if(!configuration.reduced&&!configuration.unavailable)h.ids.onionCanvas.emit('webglcontextlost',{preventDefault(){}});
    tap(h.shells.preferences);assert.deepEqual(h.selections.at(-1),{id:'preferences',options:{focus:true,announce:true}});
    const count=h.selections.length;tap(h.shells.boundary,{move:true});tap(h.shells.boundary,{cancel:true});assert.equal(h.selections.length,count);
    h.lifecycle.dispose();assert.equal(h.stats.fallbackTapDisposed,5);tap(h.shells.sessions);assert.equal(h.selections.length,count);for(const shell of Object.values(h.shells))assert.equal(shell.listenerCount,0);
  }
});
