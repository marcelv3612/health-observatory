import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as RealThree from '../static/vendor/three/three.module.js';
import {createCeramicScene,CERAMIC_LAYERS} from '../static/ceramic-onion-scene.mjs';
class Renderer {
  renders=0;disposed=0;
  constructor(){Renderer.last=this;}
  setClearColor(){}setPixelRatio(value){this.dpr=value;}setSize(width,height){this.size=[width,height];}render(scene,camera){this.renders++;this.scene=scene;this.camera=camera;}
  dispose(){this.disposed++;}
}
const THREE={...RealThree,WebGLRenderer:Renderer};
const state={lifts:[1,0,0,0,0],separation:0,turn:0,phase:0};
test('actual nested Three geometry exposes a raycast hit for every layer',()=>{
  const scene=createCeramicScene(THREE,{canvas:{}});scene.resize(520,330,3);scene.render(state);
  const hits=new Set();
  for(let x=80;x<=440;x+=5)for(let y=20;y<=310;y+=5){const id=scene.pick({x,y,width:520,height:330});if(id)hits.add(id);}
  assert.deepEqual([...hits].sort(),[...CERAMIC_LAYERS].sort());
  assert.equal(scene.pick({x:0,y:0,width:520,height:330}),null);scene.dispose();
});
test('turning and separating retain all genuine shell picks and fixed dimensions',()=>{
  const scene=createCeramicScene(THREE,{canvas:{}});scene.resize(390,250,3);
  for(const turn of [0,.155,-.155])for(const separation of [0,1])for(let selected=0;selected<5;selected++){
    scene.select(CERAMIC_LAYERS[selected]);scene.render({...state,lifts:Array.from({length:5},(_,index)=>index===selected?1:0),turn,separation});
    const hits=new Set();for(let x=20;x<390;x+=6)for(let y=5;y<250;y+=6){const id=scene.pick({x,y,width:390,height:250});if(id)hits.add(id);}
    assert.deepEqual([...hits].sort(),[...CERAMIC_LAYERS].sort(),`turn ${turn}, separation ${separation}, selected ${selected}`);
  }
  scene.dispose();
});
test('scene bounds mobile DPR/render width and disposes each geometry/material once',()=>{
  const scene=createCeramicScene(THREE,{canvas:{}});scene.resize(390,250,3);assert.equal(Renderer.last.dpr,1.25);
  scene.resize(2000,400,3);assert.equal(Renderer.last.dpr,1.5);assert.deepEqual(Renderer.last.size,[1600,320]);scene.render(state);
  let released=0;Renderer.last.scene.traverse(node=>{node.geometry?.addEventListener('dispose',()=>released++);node.material?.addEventListener('dispose',()=>released++);});
  scene.dispose();scene.dispose();assert.equal(released,20);assert.equal(Renderer.last.disposed,1);assert.equal(scene.pick({x:1,y:1,width:2,height:2}),null);
});
