(() => {
  'use strict';
  const css=document.createElement('link');css.rel='stylesheet';css.href='/static/updates.css';document.head.append(css);
  const create=(tag,className,text)=>{const node=document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;};
  const button=(text,handler,className='update-secondary')=>{const node=create('button',className,text);node.type='button';node.addEventListener('click',handler);return node;};
  const closeIcon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>';
  let state=null,loading=false,timer,countdownTimer,retryDeadline=0;
  function openDetails(){notice.hidden=true;if(state?.available)localStorage.setItem('workspace-update-dismissed',state.release.version);dialog.showModal();}
  const versionButton=button('版本',openDetails,'workspace-version');versionButton.setAttribute('aria-label','版本信息与更新');
  document.querySelector('.workspace-topbar-actions').prepend(versionButton);
  const notice=create('aside','update-notice');notice.hidden=true;notice.setAttribute('role','status');
  const noticeTitle=create('strong',''),noticeText=create('p','','新版本已发布，可查看更新内容并下载安装。');
  const noticeClose=button('',()=>{notice.hidden=true;localStorage.setItem('workspace-update-dismissed',state.release.version);},'update-icon-close');noticeClose.innerHTML=closeIcon;noticeClose.setAttribute('aria-label','稍后更新');
  notice.append(noticeClose,noticeTitle,noticeText,button('查看更新',openDetails,'update-primary'));document.body.append(notice);
  const dialog=create('dialog','update-dialog');dialog.setAttribute('aria-labelledby','update-dialog-title');
  const heading=create('div','update-dialog-heading'),title=create('h2','','版本与更新');title.id='update-dialog-title';
  const close=button('',()=>dialog.close(),'update-icon-close');close.innerHTML=closeIcon;close.setAttribute('aria-label','关闭版本信息');heading.append(title,close);
  const versions=create('p','update-versions'),message=create('p','update-message'),guide=create('p','update-guide'),links=create('div','update-downloads');
  const notes=create('div','update-notes'),actions=create('div','update-actions'),check=button('检查更新',()=>load(true));
  const releaseLink=create('a','update-secondary','GitHub 发布页');releaseLink.href='https://github.com/pppyl/lingxi-workspace/releases';releaseLink.target='_blank';releaseLink.rel='noopener noreferrer';
  actions.append(check,releaseLink);dialog.append(heading,versions,message,guide,links,notes,actions);document.body.append(dialog);
  const panel=document.getElementById('updates-settings');
  let panelVersions,panelMessage,panelLinks,panelCheck;
  if(panel){
    const head=create('div','platforms-panel-heading');head.append(create('h2','','版本更新'));panelCheck=button('检查更新',()=>load(true));head.append(panelCheck);
    const body=create('div','update-settings-body');panelVersions=create('p','update-versions');panelMessage=create('p','update-message');panelLinks=create('div','update-downloads');
    body.append(panelVersions,panelMessage,panelLinks);panel.append(head,body);
  }
  function downloadLink(asset,primary=false){const link=create('a',primary?'update-primary':'update-asset',primary?'下载更新 · '+({windows:'Windows EXE',android:'Android APK',source:'源代码 ZIP'}[asset.platform]||asset.name):asset.name);link.href=asset.url;link.target=state.platform==='android'?'_self':'_blank';link.rel='noopener noreferrer';return link;}
  function updateButtons(){
    clearTimeout(countdownTimer);const remaining=Math.max(0,Math.ceil((retryDeadline-Date.now())/1000));
    for(const node of [check,panelCheck].filter(Boolean)){node.disabled=loading||Boolean(state?.checking)||remaining>0;node.textContent=state?.checking?'正在检查…':remaining?`检查更新（${remaining} 秒）`:'检查更新';}
    if(remaining)countdownTimer=setTimeout(updateButtons,1000);
  }
  function render(){
    const release=state.release;
    versionButton.textContent='v'+state.current_version;versionButton.classList.toggle('available',state.available);
    versionButton.setAttribute('aria-label',state.available?'发现新版 v'+release.version:'版本信息 v'+state.current_version);
    const summary=`当前 v${state.current_version}${release?' · 最新 v'+release.version:''}`;
    const checked=state.checked_at?' · 上次检查 '+new Date(state.checked_at*1000).toLocaleString('zh-CN',{hour12:false}):'';
    const text=state.checking?'正在检查 GitHub 正式发布…':state.error||(state.available?'发现新版本 v'+release.version:release?'已是最新正式版本':state.checked_at?'暂未发布正式版本':'等待自动检查');
    versions.textContent=summary;message.textContent=text+checked;message.classList.toggle('error',Boolean(state.error));
    guide.textContent={windows:'下载新版后，先从托盘退出旧客户端，再运行新版。账号和任务会保留。',android:'下载新版 APK 后覆盖安装，无需卸载，保留本机账号和任务。',source:'当前运行源码服务。下载新版源码后更新程序文件，保留本地数据目录。'}[state.platform];
    links.replaceChildren();
    if(state.available){if(state.recommended)links.append(downloadLink(state.recommended,true));for(const asset of release.assets)if(asset.url!==state.recommended?.url)links.append(downloadLink(asset));}
    guide.hidden=!state.available;notes.textContent=release?.notes||'';notes.hidden=!notes.textContent;
    releaseLink.href=release?.url||state.releases_url;
    if(panel){panelVersions.textContent=summary;panelMessage.textContent=text+checked;panelMessage.classList.toggle('error',Boolean(state.error));panelLinks.replaceChildren();if(state.available&&state.recommended)panelLinks.append(downloadLink(state.recommended,true));}
    if(state.available&&localStorage.getItem('workspace-update-dismissed')!==release.version){noticeTitle.textContent='发现新版本 v'+release.version;notice.hidden=false;}
    else notice.hidden=true;
    retryDeadline=Date.now()+state.retry_after*1000;updateButtons();
  }
  async function load(force=false){
    if(loading)return;loading=true;clearTimeout(timer);updateButtons();
    try{
      const response=await fetch(force?'/api/updates/check':'/api/updates',force?{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}:undefined);
      const result=await response.json();if(!response.ok)throw new Error(result.error||'无法读取更新信息');state=result;render();
    }catch(error){message.textContent=error.message;if(panelMessage)panelMessage.textContent=error.message;}
    finally{loading=false;updateButtons();timer=setTimeout(()=>{if(!document.hidden||dialog.open)load();else timer=setTimeout(()=>load(),60000);},state?.checking?1000:60000);}
  }
  load();
})();
