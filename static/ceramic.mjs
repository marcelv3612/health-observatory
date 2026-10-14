import {createHealthOnion,installOnionTap} from './onion-health.mjs';
import {mountCeramicOnion} from './ceramic-onion.mjs';
const health=createHealthOnion({doc:document,win:window});
const scene=mountCeramicOnion({doc:document,win:window,health,installTap:installOnionTap});
window.addEventListener('pagehide',event=>{if(!event.persisted){scene.dispose();health.dispose();}},{once:false});
