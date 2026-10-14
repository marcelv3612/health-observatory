// Geometry motion is independent of the Health data and shared selection text.
export function createOnionMotion({media, document: doc, button, requestFrame, cancelFrame, render, duration=250, onError=()=>{}}) {
  let available=true, disposed=false, inView=true, animate=false;
  let frame=null, lastDraw=null, lastTime=null, phase=0, tweenStart=null;
  let current={lifts:[1,0,0,0,0],separation:0,turn:0};
  let from=copy(current), target=copy(current), moving=false;
  const canDraw=()=>available&&!disposed&&inView&&doc.visibilityState==='visible';
  const copyState=()=>({...copy(current),phase});
  function copy(state){return {lifts:[...state.lifts],separation:state.separation,turn:state.turn};}
  function stop(){if(frame!==null)cancelFrame(frame);frame=null;lastDraw=null;lastTime=null;tweenStart=null;from=copy(current);}
  function updateButton(){
    button.disabled=!available||disposed||media.matches;
    button.textContent=!available||disposed?'Static view':media.matches?'Reduced motion':animate?'Pause view':'Animate view';
    button.setAttribute('aria-pressed',String(animate));
  }
  function draw(){
    if(!canDraw())return;
    try{render(copyState());}catch(error){available=false;stop();updateButton();onError(error);}
  }
  function settle(){current=copy(target);moving=false;tweenStart=null;from=copy(current);}
  function tick(time){
    frame=null;if(!canDraw()||media.matches)return;
    const delta=lastTime===null?0:Math.max(0,Math.min((time-lastTime)/1000,.1));lastTime=time;
    if(animate)phase+=delta;
    let finished=false;
    if(moving){
      if(tweenStart===null)tweenStart=time;
      const progress=Math.min(1,Math.max(0,(time-tweenStart)/Math.max(1,duration)));
      const amount=1-(1-progress)**3;
      current={lifts:from.lifts.map((value,index)=>value+(target.lifts[index]-value)*amount),
        separation:from.separation+(target.separation-from.separation)*amount,
        turn:from.turn+(target.turn-from.turn)*amount};
      if(progress===1){settle();finished=true;}
    }
    if(finished||lastDraw===null||time-lastDraw>=1000/30){draw();lastDraw=time;}
    if(canDraw()&&!media.matches&&(animate||moving))frame=requestFrame(tick);
  }
  function reconcile(){
    if(!canDraw()||media.matches||(!animate&&!moving))stop();
    if(media.matches)settle();
    updateButton();draw();
    if(canDraw()&&!media.matches&&(animate||moving)&&frame===null)frame=requestFrame(tick);
  }
  function change(next){
    target={...target,...next};from=copy(current);tweenStart=null;
    moving=JSON.stringify(current)!==JSON.stringify(target);
    if(media.matches)settle();reconcile();
  }
  const onClick=()=>{if(!button.disabled){animate=!animate;reconcile();}};
  button.addEventListener('click',onClick);media.addEventListener('change',reconcile);doc.addEventListener('visibilitychange',reconcile);
  reconcile();
  return {draw,
    select(index){if(Number.isInteger(index)&&index>=0&&index<5)change({lifts:Array.from({length:5},(_,item)=>item===index?1:0)});},
    separate(value){change({separation:value?1:0});},
    turn(value){change({turn:value});},
    setInView(value){inView=Boolean(value);reconcile();},
    setAvailable(value){available=Boolean(value);reconcile();},
    dispose(){if(disposed)return;disposed=true;stop();updateButton();button.removeEventListener('click',onClick);media.removeEventListener('change',reconcile);doc.removeEventListener('visibilitychange',reconcile);}
  };
}
