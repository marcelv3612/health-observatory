// All dimensions are fixed application-layer geometry, never medical values.
export const CERAMIC_LAYERS=['sessions','measurements','context','preferences','boundary'];
export const SHELL_RADII=[.36,.62,.88,1.14,1.4];
export function createCeramicScene(THREE,{canvas}) {
  const resources=new Set(),own=value=>{resources.add(value);return value;};
  let renderer;
  try {
    renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true,powerPreference:'low-power'});
    renderer.setClearColor(0xe6edff,1);renderer.outputColorSpace=THREE.SRGBColorSpace;
    renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.1;
    const scene=new THREE.Scene(),camera=new THREE.OrthographicCamera(-2,2,1.8,-1.8,.1,30);
    camera.position.set(.1,.1,6);camera.lookAt(0,0,0);
    scene.add(new THREE.HemisphereLight(0xffffff,0x9aadd2,2.6));
    const key=new THREE.DirectionalLight(0xffffff,3.7);key.position.set(-3,5,5);scene.add(key);
    const fill=new THREE.DirectionalLight(0xcbd8ff,1.8);fill.position.set(3,1,-3);scene.add(fill);
    const group=new THREE.Group();scene.add(group);group.rotation.set(.16,0,-.24);
    const colors=[0xd1eb54,0xffffff,0xb8c9f0,0x2845b7,0xdce4fa];
    const shells=SHELL_RADII.map((radius,index)=>{
      const layer=new THREE.Group();group.add(layer);
      // The rear hemisphere has an open front: every nested shell can be picked.
      const geometry=own(new THREE.SphereGeometry(radius,48,24,Math.PI,Math.PI,0,Math.PI));
      const material=own(new THREE.MeshPhysicalMaterial({color:colors[index],roughness:.28,metalness:0,clearcoat:.8,clearcoatRoughness:.2,side:THREE.DoubleSide}));
      const shell=new THREE.Mesh(geometry,material);shell.userData.onionLayer=CERAMIC_LAYERS[index];layer.add(shell);
      const rimMaterial=own(new THREE.MeshBasicMaterial({color:index===3?0x142875:0x7488bb}));
      const rim=new THREE.Mesh(own(new THREE.TorusGeometry(radius,.014,8,64)),rimMaterial);layer.add(rim);
      return {layer,shell,material,rim};
    });
    const raycaster=new THREE.Raycaster(),point=new THREE.Vector2();
    let selected='sessions',disposed=false;
    function resize(width,height,dpr){
      if(disposed)return;
      const boundedWidth=Math.max(1,Math.min(1600,width)),boundedHeight=Math.max(1,height);
      renderer.setPixelRatio(Math.min(dpr||1,boundedWidth<600?1.25:1.5));
      renderer.setSize(boundedWidth,boundedHeight*boundedWidth/Math.max(1,width),false);
      const half=1.8,aspect=Math.max(1,width)/boundedHeight;
      camera.left=-half*aspect;camera.right=half*aspect;camera.top=half;camera.bottom=-half;camera.updateProjectionMatrix();
    }
    function render({lifts,separation,turn,phase}){
      if(disposed)return;
      group.rotation.y=turn+Math.sin(phase*.35)*.035;
      shells.forEach(({layer,material},index)=>{
        layer.position.set((index-2)*-.11*separation, (index-2)*.04*separation, (4-index)*.12*separation+.1*lifts[index]);
        material.emissive.setHex(CERAMIC_LAYERS[index]===selected?0x172d84:0x000000);
        material.emissiveIntensity=CERAMIC_LAYERS[index]===selected?.14:0;
      });
      renderer.render(scene,camera);
    }
    function pick({x,y,width,height}){
      if(disposed||width<=0||height<=0)return null;
      point.set(x/width*2-1,1-y/height*2);scene.updateMatrixWorld(true);camera.updateMatrixWorld(true);
      raycaster.setFromCamera(point,camera);
      return raycaster.intersectObjects(shells.map(item=>item.shell),false)[0]?.object.userData.onionLayer||null;
    }
    function dispose(){if(disposed)return;disposed=true;resources.forEach(value=>value.dispose());resources.clear();renderer.dispose();}
    return {resize,render,pick,select(id){if(CERAMIC_LAYERS.includes(id))selected=id;},dispose};
  }catch(error){resources.forEach(value=>value.dispose());renderer?.dispose();throw error;}
}
