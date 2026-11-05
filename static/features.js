(() => {
  'use strict';
  let loading=false,pending=false;
  async function refresh(){
    if(loading){pending=true;return;}loading=true;
    try{
      const response=await fetch('/api/features');if(!response.ok)return;
      const {features}=await response.json();
      const current=features.filter(item=>location.pathname===item.home.replace(/\/$/,'')||location.pathname===item.home||location.pathname.startsWith(item.home.replace(/\/$/,'')+'/')).sort((a,b)=>b.home.length-a.home.length)[0];
      if(current&&(!current.enabled)!==(document.body.dataset.featureDisabled==='true'))location.reload();
      for(const link of document.querySelectorAll('#workspace-navigation a[href]')){
        const path=link.getAttribute('href'),feature=features.find(item=>path===item.home||item.id==='lingxi'&&path==='/lingxi/tasks'||item.id==='recharge'&&path.startsWith('/recharge/'));
        if(!feature)continue;
        link.classList.toggle('workspace-feature-disabled',!feature.enabled);
        link.title=feature.enabled?'':feature.title+'已关闭，可在平台管理重新开启';
      }
    }catch{/* Keep the last confirmed state while the local service restarts. */}
    finally{loading=false;if(pending){pending=false;refresh();}}
  }
  window.addEventListener('features-changed',refresh);
  window.addEventListener('workspace-navigation-changed',refresh);
  window.addEventListener('focus',refresh);
  refresh();setInterval(()=>{if(!document.hidden)refresh();},15000);
})();
