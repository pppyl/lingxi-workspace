(() => {
  'use strict';
  const $=id=>document.getElementById(id), dialog=$('platforms-delete-dialog');
  const labels={running:'运行中',expired:'已到期 · 已停止',deleted:'已移除'};
  let selected=null,working=false,loading=false;
  const date=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
  function element(tag,className,text){const node=document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;}
  function feedback(message,error=false){$('platforms-feedback').textContent=message;$('platforms-feedback').hidden=!message;$('platforms-feedback').classList.toggle('error',error);}
  function render(platforms){
    const rows=platforms.map(platform=>{
      const row=document.createElement('tr'),name=document.createElement('td');
      name.append(element('strong','',platform.title));
      if(platform.state==='deleted'&&platform.deleted_at)name.append(element('small','',`移除于 ${date(platform.deleted_at)}`));
      const state=document.createElement('td');state.append(element('span',`platforms-badge ${platform.state}`,platform.removal_pending?'已停止 · 待清理':labels[platform.state]));
      const actions=element('td','platforms-actions');
      if(platform.active){const link=element('a','platforms-open','打开活动');link.href=platform.home;actions.append(link);}
      if(platform.state!=='deleted'||platform.removal_pending){const remove=element('button','platforms-remove',platform.removal_pending?'继续清理':'永久移除');remove.type='button';remove.disabled=working;remove.addEventListener('click',()=>openDelete(platform));actions.append(remove);}
      else actions.append(element('span','platforms-muted','已清理'));
      row.append(name,element('td','',platform.temporary?'短期活动':'长期活动'),state,element('td','platforms-time',date(platform.end_at)),actions);return row;
    });
    if(!rows.length){const row=document.createElement('tr'),cell=element('td','platforms-empty','暂无独立活动平台');cell.colSpan=5;row.append(cell);rows.push(row);}
    $('platforms-rows').replaceChildren(...rows);$('platforms-count').textContent=platforms.filter(item=>item.state!=='deleted').length;
  }
  async function refresh(){
    if(loading||working)return;loading=true;$('platforms-refresh').disabled=true;
    try{const response=await fetch('/api/platforms');const result=await response.json();if(!response.ok)throw new Error(result.error||'暂时无法读取活动平台');render(result.platforms);feedback(result.warning||'');}
    catch(error){feedback(error.message,true);}
    finally{loading=false;$('platforms-refresh').disabled=false;}
  }
  function openDelete(platform){selected=platform;$('platforms-delete-name').textContent=platform.title;$('platforms-delete-id').textContent=platform.id;$('platforms-confirmation').value='';$('platforms-delete-submit').disabled=true;$('platforms-delete-error').hidden=true;dialog.showModal();$('platforms-confirmation').focus();}
  const close=()=>{if(!working)dialog.close();};
  $('platforms-delete-close').addEventListener('click',close);$('platforms-delete-cancel').addEventListener('click',close);
  dialog.addEventListener('cancel',event=>{if(working)event.preventDefault();});
  $('platforms-confirmation').addEventListener('input',()=>{$('platforms-delete-submit').disabled=working||$('platforms-confirmation').value!==selected.id;});
  $('platforms-delete-form').addEventListener('submit',async event=>{
    event.preventDefault();if(working||!selected||$('platforms-confirmation').value!==selected.id)return;
    working=true;for(const button of dialog.querySelectorAll('button'))button.disabled=true;$('platforms-delete-submit').textContent='正在移除…';$('platforms-delete-error').hidden=true;
    try{const response=await fetch(`/api/platforms/${encodeURIComponent(selected.id)}/delete`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation:$('platforms-confirmation').value})});const result=await response.json();if(!response.ok)throw new Error(result.error||'活动移除未完成');render(result.platforms);dialog.close();feedback(result.message);window.dispatchEvent(new Event('platforms-changed'));}
    catch(error){$('platforms-delete-error').textContent=error.message;$('platforms-delete-error').hidden=false;}
    finally{working=false;for(const button of dialog.querySelectorAll('button'))button.disabled=false;$('platforms-delete-submit').textContent='永久移除';await refresh();}
  });
  $('platforms-refresh').addEventListener('click',refresh);window.addEventListener('focus',refresh);refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);
})();
