(() => {
  'use strict';
  let loading=false,pending=false;
  let orderedFeatures=[];
  function orderNavigation(){
    const marker=document.getElementById('workspace-activity-navigation');
    if(!marker||!orderedFeatures.length)return;
    const navigation=marker.parentElement;
    const roots=[...navigation.children].filter(node=>node.classList.contains('workspace-nav-group')||node.classList.contains('workspace-nav-item')&&node.getAttribute('href')!=='/');
    const position=node=>{
      const home=node.classList.contains('workspace-nav-group')?node.querySelector('.workspace-subnav-item')?.getAttribute('href'):node.getAttribute('href');
      const index=orderedFeatures.findIndex(feature=>feature.home===home);
      return index<0?orderedFeatures.length:index;
    };
    roots.sort((a,b)=>position(a)-position(b));
    const expected=[...navigation.children].filter(node=>roots.includes(node));
    if(roots.some((node,index)=>node!==expected[index]))for(const node of roots)navigation.insertBefore(node,marker);
  }
  async function refresh(){
    if(loading){pending=true;return;}loading=true;
    try{
      const response=await fetch('/api/features');if(!response.ok)return;
      const {features}=await response.json();
      orderedFeatures=features;orderNavigation();
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
  window.addEventListener('workspace-navigation-changed',()=>{orderNavigation();void refresh();});
  window.addEventListener('focus',refresh);
  refresh();setInterval(()=>{if(!document.hidden)refresh();},15000);
})();
