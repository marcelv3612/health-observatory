import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createOnionMotion} from '../static/ceramic-motion.mjs';
class Events {
  listeners=new Map();attributes={};
  addEventListener(name,callback){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(callback);}
  removeEventListener(name,callback){this.listeners.get(name)?.delete(callback);}
  emit(name,event={}){for(const callback of [...(this.listeners.get(name)||[])])callback(event);}
  setAttribute(name,value){this.attributes[name]=value;}
  get listenerCount(){return [...this.listeners.values()].reduce((sum,set)=>sum+set.size,0);}
}
function harness({reduced=false,render}={}){
  const media=new Events();media.matches=reduced;const doc=new Events();doc.visibilityState='visible';const button=new Events();
  const frames=new Map(),renders=[];let next=0,errors=0;
  const motion=createOnionMotion({media,document:doc,button,requestFrame:callback=>{const id=++next;frames.set(id,callback);return id;},cancelFrame:id=>frames.delete(id),render:state=>{renders.push(state);render?.(state);},onError:()=>errors++});
  return {media,doc,button,frames,renders,motion,get errors(){return errors;},tick(time){const callbacks=[...frames.values()];frames.clear();callbacks.forEach(callback=>callback(time));}};
}
test('view is still on entry; selecting and separating settle without a persistent frame',()=>{
  const h=harness();assert.equal(h.frames.size,0);assert.equal(h.button.textContent,'Animate view');
  h.motion.select(3);h.motion.separate(true);h.motion.turn(.12);assert.equal(h.frames.size,1);
  for(let t=0;t<=260;t+=10)h.tick(t);
  assert.equal(h.frames.size,0);assert.deepEqual(h.renders.at(-1).lifts,[0,0,0,1,0]);assert.equal(h.renders.at(-1).separation,1);assert.equal(h.renders.at(-1).turn,.12);
  h.motion.dispose();
});
test('reduced motion applies selection and separation immediately without animation',()=>{
  const h=harness({reduced:true});h.motion.select(2);h.motion.separate(true);
  assert.equal(h.frames.size,0);assert.deepEqual(h.renders.at(-1).lifts,[0,0,1,0,0]);assert.equal(h.renders.at(-1).separation,1);
  assert.equal(h.button.disabled,true);h.motion.dispose();
});
test('animation is opt-in and a deliberate stop survives preference changes',()=>{
  const h=harness();h.button.emit('click');assert.equal(h.frames.size,1);assert.equal(h.button.textContent,'Pause view');
  h.tick(0);h.tick(50);assert.ok(h.renders.at(-1).phase>0);
  h.button.emit('click');assert.equal(h.frames.size,0);
  h.media.matches=true;h.media.emit('change');h.media.matches=false;h.media.emit('change');assert.equal(h.frames.size,0);assert.equal(h.button.textContent,'Animate view');h.motion.dispose();
});
test('hidden/offscreen views stop motion and resume without catch-up',()=>{
  const h=harness();h.button.emit('click');h.tick(0);h.tick(50);const phase=h.renders.at(-1).phase;
  h.doc.visibilityState='hidden';h.doc.emit('visibilitychange');assert.equal(h.frames.size,0);const count=h.renders.length;h.motion.draw();assert.equal(h.renders.length,count);
  h.doc.visibilityState='visible';h.doc.emit('visibilitychange');h.tick(100000);assert.equal(h.renders.at(-1).phase,phase);
  h.motion.setInView(false);assert.equal(h.frames.size,0);h.motion.setInView(true);assert.equal(h.frames.size,1);h.motion.dispose();
});
test('continuous renders stay at 30fps; live reduction settles a current transition',()=>{
  const h=harness();h.button.emit('click');const count=h.renders.length;
  for(let t=0;t<=100;t+=10)h.tick(t);assert.equal(h.renders.length-count,3);
  h.motion.select(4);h.media.matches=true;h.media.emit('change');assert.equal(h.frames.size,0);assert.deepEqual(h.renders.at(-1).lifts,[0,0,0,0,1]);h.motion.dispose();
});
test('disposal and rendering failure remove or stop every frame',()=>{
  const h=harness();h.button.emit('click');h.motion.dispose();h.motion.dispose();assert.equal(h.frames.size,0);assert.equal(h.media.listenerCount,0);assert.equal(h.doc.listenerCount,0);assert.equal(h.button.listenerCount,0);
  const broken=harness({render:()=>{throw Error('Synthetic graphics unavailable');}});assert.equal(broken.errors,1);assert.equal(broken.frames.size,0);assert.equal(broken.button.disabled,true);broken.motion.select(1);assert.equal(broken.errors,1);broken.motion.dispose();
});
