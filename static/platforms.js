(() => {
  'use strict';
  const $=id=>document.getElementById(id), dialog=$('platforms-delete-dialog');
  const labels={running:'运行中',expired:'已到期 · 已停止',deleted:'已移除'};
  let selected=null,working=false,loading=false,refreshPending=false,draggingId=null,rowsData=[],defaultOrder=[];
  const arrow=direction=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${direction==='up'?'m6 14 6-6 6 6':'m6 10 6 6 6-6'}"/></svg>`;
  const date=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
  function element(tag,className,text){const node=document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;}
  function feedback(message,error=false){$('platforms-feedback').textContent=message;$('platforms-feedback').hidden=!message;$('platforms-feedback').classList.toggle('error',error);}
  function render(platforms){
    platforms=platforms.filter(platform=>platform.state!=='deleted'||platform.removal_pending);
    rowsData=platforms;
    const sortable=platforms.filter(platform=>defaultOrder.includes(platform.id)&&platform.state!=='deleted').map(platform=>platform.id);
    $('platforms-order-reset').disabled=working||sortable.length===defaultOrder.length&&sortable.every((id,index)=>id===defaultOrder[index]);
    const rows=platforms.map(platform=>{
      const row=document.createElement('tr'),name=document.createElement('td');
      name.append(element('strong','',platform.title));
      if(platform.navigation==='checkin')name.append(element('small','','入口位于“签到有礼”内'));
      if(platform.state==='deleted'&&platform.deleted_at)name.append(element('small','',`移除于 ${date(platform.deleted_at)}`));
      const state=document.createElement('td');state.append(element('span',`platforms-badge ${platform.enabled===false?'disabled':platform.state}`,platform.removal_pending?'已停止 · 待清理':platform.state==='running'&&platform.enabled===false?'已关闭':labels[platform.state]));
      const switchCell=element('td','platforms-switch-cell');
      if(platform.state==='running'){
        const label=element('label','platforms-feature-switch'),toggle=element('input');toggle.type='checkbox';toggle.checked=platform.enabled!==false;toggle.disabled=working;toggle.setAttribute('role','switch');toggle.setAttribute('aria-label',`启用${platform.title}`);
        label.append(toggle,element('span','platforms-switch-track'));switchCell.append(label);
        toggle.addEventListener('change',()=>switchFeature(platform,toggle));
      }else switchCell.append(element('span','platforms-muted','—'));
      const actions=element('td','platforms-actions');
      if(platform.active){const link=element('a','platforms-open','打开活动');link.href=platform.home;actions.append(link);}
      if(platform.can_delete!==false&&(platform.state!=='deleted'||platform.removal_pending)){const remove=element('button','platforms-remove',platform.removal_pending?'继续清理':'永久移除');remove.type='button';remove.disabled=working;remove.addEventListener('click',()=>openDelete(platform));actions.append(remove);}
      else if(platform.state==='deleted')actions.append(element('span','platforms-muted','已清理'));
      const sortCell=element('td','platforms-order-cell'),position=sortable.indexOf(platform.id);
      if(position>=0){
        const controls=element('div','platforms-order-controls'),handle=element('button','platforms-order-handle');
        handle.type='button';handle.id='platforms-order-'+platform.id;handle.draggable=!working;handle.disabled=working;
        handle.setAttribute('aria-label',`拖动排序：${platform.title}`);handle.title='拖动调整顺序，也可使用右侧上下按钮';
        handle.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M8 5h0 M16 5h0 M8 12h0 M16 12h0 M8 19h0 M16 19h0"/></svg>';
        handle.addEventListener('dragstart',event=>{if(working){event.preventDefault();return;}draggingId=platform.id;event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',platform.id);event.dataTransfer.setDragImage(row,24,20);row.classList.toggle('platforms-dragging',true);});
        handle.addEventListener('dragend',()=>{draggingId=null;render(rowsData);if(refreshPending&&!working)void refresh();});
        row.addEventListener('dragover',event=>{if(!draggingId||draggingId===platform.id||working)return;event.preventDefault();event.dataTransfer.dropEffect='move';row.classList.toggle('platforms-drop-target',true);});
        row.addEventListener('dragleave',event=>{if(!row.contains(event.relatedTarget))row.classList.toggle('platforms-drop-target',false);});
        row.addEventListener('drop',event=>{
          if(!draggingId||draggingId===platform.id||working)return;event.preventDefault();
          const moving=draggingId,ids=sortable.filter(id=>id!==moving),bounds=row.getBoundingClientRect();
          ids.splice(ids.indexOf(platform.id)+(event.clientY>bounds.top+bounds.height/2?1:0),0,moving);
          draggingId=null;void saveOrder(ids,moving);
        });
        controls.append(handle,element('span','platforms-order-number',String(position+1)));
        for(const [direction,offset,label] of [['up',-1,'上移'],['down',1,'下移']]){
          const move=element('button','platforms-order-move');move.type='button';move.innerHTML=arrow(direction);
          move.setAttribute('aria-label',`${label}${platform.title}`);move.title=label;
          move.disabled=working||position+offset<0||position+offset>=sortable.length;
          move.addEventListener('click',()=>{const ids=[...sortable];[ids[position],ids[position+offset]]=[ids[position+offset],ids[position]];return saveOrder(ids,platform.id);});
          controls.append(move);
        }
        sortCell.append(controls);
      }else sortCell.append(element('span','platforms-muted','—'));
      row.append(name,element('td','',platform.temporary?'短期活动':platform.can_delete===false?'常驻功能':'长期活动'),state,element('td','platforms-time',date(platform.end_at)),switchCell,sortCell,actions);return row;
    });
    if(!rows.length){const row=document.createElement('tr'),cell=element('td','platforms-empty','暂无功能');cell.colSpan=7;row.append(cell);rows.push(row);}
    $('platforms-rows').replaceChildren(...rows);$('platforms-count').textContent=platforms.filter(item=>item.state!=='deleted').length;
  }
  async function refresh(){
    if(loading||working||draggingId){refreshPending=true;return;}loading=true;refreshPending=false;$('platforms-refresh').disabled=true;
    try{const [response,featureResponse]=await Promise.all([fetch('/api/platforms'),fetch('/api/features')]);const result=await response.json(),featureResult=await featureResponse.json();if(!response.ok||!featureResponse.ok)throw new Error(result.error||featureResult.error||'暂时无法读取功能状态');if(working||draggingId){refreshPending=true;return;}defaultOrder=featureResult.default_order||featureResult.features.filter(row=>row.state!=='deleted').map(row=>row.id);const byId=new Map(result.platforms.map(row=>[row.id,row]));render([...featureResult.features.map(row=>({...byId.get(row.id),...row})),...result.platforms.filter(row=>!featureResult.features.some(feature=>feature.id===row.id))]);feedback(result.warning||'');}
    catch(error){feedback(error.message,true);}
    finally{loading=false;$('platforms-refresh').disabled=false;if(refreshPending&&!working&&!draggingId){refreshPending=false;void refresh();}}
  }
  async function saveOrder(ids,focusId){
    if(working)return;working=true;render(rowsData);
    let message='',failed=false;
    try{
      const response=await fetch('/api/features/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({feature_ids:ids})});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'排序保存失败');
      message=result.message;window.dispatchEvent(new Event('features-changed'));window.dispatchEvent(new Event('platforms-changed'));
    }catch(error){message=error.message;failed=true;}
    finally{working=false;await refresh();feedback(message,failed);if(focusId)$('platforms-order-'+focusId)?.focus();}
  }
  async function switchFeature(platform,toggle){
    if(working){toggle.checked=platform.enabled!==false;return;}working=true;toggle.disabled=true;
    try{const response=await fetch('/api/features/'+encodeURIComponent(platform.id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:toggle.checked})});const result=await response.json();if(!response.ok)throw new Error(result.error||'开关保存失败');platform.enabled=toggle.checked;feedback(result.message);window.dispatchEvent(new Event('features-changed'));window.dispatchEvent(new Event('platforms-changed'));}
    catch(error){toggle.checked=platform.enabled!==false;feedback(error.message,true);}
    finally{working=false;toggle.disabled=false;await refresh();}
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
  $('platforms-order-reset').addEventListener('click',()=>saveOrder([...defaultOrder]));
  $('platforms-refresh').addEventListener('click',refresh);window.addEventListener('focus',refresh);refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);
})();
