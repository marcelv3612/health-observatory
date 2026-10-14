import {createOnionMotion} from './ceramic-motion.mjs';
import {createCeramicScene,CERAMIC_LAYERS} from './ceramic-onion-scene.mjs';

export function mountCeramicOnion({doc=document,win=window,health,installTap,loadThree=()=>import('./vendor/three/three.module.js'),makeScene=createCeramicScene}) {
  const viewport=doc.getElementById('onionViewport'),canvas=doc.getElementById('onionCanvas');
  const fallback=doc.getElementById('onionFallback'),button=doc.getElementById('onionMotion');
  const separate=doc.getElementById('onionSeparate'),turn=doc.getElementById('onionTurn');
  const media=win.matchMedia('(prefers-reduced-motion: reduce)');
  let disposed=false,suspended=false,loading=false,initialized=false,inView=false;
  let scene,motion,resizeObserver,intersection,removeTap,separated=false,turnStep=0;
  let selected=health.getSelected();
  const canInitialize=()=>!disposed&&!suspended&&doc.visibilityState==='visible'&&inView&&!media.matches;
  function showFallback(){canvas.hidden=true;fallback.removeAttribute('hidden');button.disabled=true;button.textContent=media.matches?'Reduced motion':'Static view';}
  function release(){motion?.dispose();motion=undefined;scene?.dispose();scene=undefined;}
  function resize(){if(!scene||disposed||suspended||doc.visibilityState!=='visible'||!inView)return;scene.resize(viewport.clientWidth,viewport.clientHeight,win.devicePixelRatio||1);motion?.draw();}
  function fail(){initialized=true;showFallback();release();removeTap?.();removeTap=undefined;}
  const unsubscribe=health.subscribe(id=>{selected=id;scene?.select(id);motion?.select(CERAMIC_LAYERS.indexOf(id));});
  const fallbackTaps=[...fallback.querySelectorAll('[data-onion-shell]')].map(shell=>installTap({element:shell,win,onPick:()=>health.select(shell.dataset.onionShell,{focus:true,announce:true})}));
  const onSeparate=()=>{separated=!separated;separate.setAttribute('aria-pressed',String(separated));separate.textContent=separated?'Nest layers':'Separate layers';fallback.dataset.separated=String(separated);motion?.separate(separated);};
  const onTurn=()=>{turnStep=(turnStep+1)%3;fallback.dataset.turn=String(turnStep);motion?.turn([0,.12,-.12][turnStep]);};
  separate.addEventListener('click',onSeparate);turn.addEventListener('click',onTurn);
  async function initialize(){
    if(initialized||loading||!canInitialize())return;loading=true;
    try{
      const THREE=await loadThree();if(!canInitialize())return;
      scene=makeScene(THREE,{canvas});scene.select(selected);resize();
      motion=createOnionMotion({media,document:doc,button,
        requestFrame:callback=>win.requestAnimationFrame(callback),cancelFrame:id=>win.cancelAnimationFrame(id),
        duration:parseFloat(win.getComputedStyle?.(doc.documentElement).getPropertyValue('--duration-fast'))||250,
        render:state=>scene.render(state),onError:fail});
      // apply choices made on the SVG while the optional renderer was loading.
      motion.setInView(inView&&!suspended);motion.select(CERAMIC_LAYERS.indexOf(selected));motion.separate(separated);motion.turn([0,.12,-.12][turnStep]);
      if(!scene){motion?.dispose();motion=undefined;return;}
      initialized=true;canvas.hidden=false;fallback.setAttribute('hidden','');
      removeTap=installTap({element:canvas,win,onPick:point=>{const id=scene?.pick(point);if(id)health.select(id,{focus:true,announce:true});}});
      if('ResizeObserver' in win){resizeObserver=new win.ResizeObserver(resize);resizeObserver.observe(viewport);}else win.addEventListener('resize',resize);
    }catch{initialized=true;fail();}finally{loading=false;}
  }
  function dispose(){
    if(disposed)return;disposed=true;release();unsubscribe();removeTap?.();fallbackTaps.forEach(remove=>remove());intersection?.disconnect();resizeObserver?.disconnect();
    separate.removeEventListener('click',onSeparate);turn.removeEventListener('click',onTurn);
    media.removeEventListener('change',onPreference);doc.removeEventListener('visibilitychange',onVisibility);
    win.removeEventListener('resize',resize);win.removeEventListener('pagehide',onPageHide);win.removeEventListener('pageshow',onPageShow);canvas.removeEventListener('webglcontextlost',onContextLost);
  }
  function onContextLost(event){event.preventDefault();fail();}
  function onPreference(){if(!initialized){showFallback();initialize();}}
  function onVisibility(){if(doc.visibilityState==='visible'&&!suspended){initialize();resize();}}
  function onPageHide(event){if(event.persisted){suspended=true;motion?.setInView(false);}else dispose();}
  function onPageShow(event){if(event.persisted){suspended=false;motion?.setInView(inView);initialize();resize();}}
  media.addEventListener('change',onPreference);doc.addEventListener('visibilitychange',onVisibility);
  canvas.addEventListener('webglcontextlost',onContextLost);win.addEventListener('pagehide',onPageHide);win.addEventListener('pageshow',onPageShow);
  if('IntersectionObserver' in win){intersection=new win.IntersectionObserver(entries=>{inView=entries.some(entry=>entry.isIntersecting);motion?.setInView(inView&&!suspended);initialize();if(inView)resize();},{rootMargin:'40px'});intersection.observe(viewport);}else{inView=true;initialize();}
  showFallback();
  return {dispose};
}
