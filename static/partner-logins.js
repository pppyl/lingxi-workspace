(() => {
  'use strict';
  const dialog=$('partner-logins'),prefix='/api/checkin/partners';
  let model=null,busy=false,flow=null,timer=null,revision=0;
  const current=()=>window.CheckinAccount.current();
  const time=value=>value?dateFormat(value*1000):'尚未检查';
  function feedback(text,error=false){$('partner-feedback').textContent=text;$('partner-feedback').classList.toggle('error',error);}
  function controls(){
    for(const button of dialog.querySelectorAll('[data-partner-action]'))button.disabled=busy||!!flow||button.dataset.unavailable==='true';
    $('partner-import-submit').disabled=busy||!!flow||!current();
    $('partner-import-service').disabled=busy||!!flow;
    $('partner-import-text').disabled=busy||!!flow;
    $('partner-import-file').disabled=busy||!!flow;
  }
  function render(){
    const account=current();
    $('partner-current-account').textContent=account?`${account.name||account.phone_masked} · ${account.phone_masked}`:'请先选择共享账号';
    $('partner-services').replaceChildren(...(model?.services||[]).map(service=>{
      const card=node('section','partner-service');
      card.dataset.service=service.service;
      const head=node('div','partner-service-heading');head.append(node('h3','',service.title),node('span','partner-state '+service.state,
        {saved:'已保存',needs_update:'需要更新',verification_required:'需官方验证'}[service.state]));
      card.append(head,node('p','',service.message));
      const details=node('dl','partner-service-details');
      for(const [label,text] of [['最近验证',time(service.checked_at)],['官方到期',service.expires_at?time(service.expires_at):'未提供明确期限'],['绑定账号',service.uid_hint||'验证后显示']]){
        const row=node('div');row.append(node('dt','',label),node('dd','',text));details.append(row);
      }
      const actions=node('div','partner-service-actions');
      for(const [label,kind] of [['检查并更新','check'],['扫码更新','qr'],['手动更新','import']]){
        const button=node('button',kind==='qr'?'primary':'secondary',label);button.type='button';button.dataset.partnerAction=kind;
        if(kind==='qr'&&!service.qr_available){button.dataset.unavailable='true';button.title=service.service==='kuaishou'?'请使用手动更新导入本人完整 JSON 登录资料。':'首次需先导入本人百度地图设备资料，后续可扫码更新。';}
        button.addEventListener('click',()=>kind==='check'?update(service.service):kind==='qr'?startQr(service.service):openImport(service.service));
        actions.append(button);
      }
      card.append(details,actions);return card;
    }));
    $('partner-data-directory').textContent=model?.data_directory||'正在读取统一数据目录…';controls();
  }
  async function load(){
    const id=current()?.id,token=++revision;
    if(!id){model=null;render();feedback('请先在签到页面选择账号。',true);return;}
    try{const value=await api(prefix+'?login_id='+encodeURIComponent(id));if(token!==revision||id!==current()?.id)return;model=value;render();}
    catch(error){if(token===revision)feedback(error.message,true);}
  }
  async function update(service,text){
    if(busy||flow)return;
    const id=current()?.id,token=++revision;if(!id)return;
    busy=true;controls();feedback(text===undefined?'正在核对官方身份和会话轮换…':'正在验证新资料，验证通过后更新…');
    try{
      const result=await api(prefix+(text===undefined?'/check':'/import'),{login_id:id,service,...(text===undefined?{}:{text})});
      if(token===revision&&id===current()?.id&&dialog.open){model=result;render();feedback('官方身份已验证，登录态已保存。');$('partner-import-text').value='';$('partner-import-file').value='';}
      window.dispatchEvent(new CustomEvent('partner-logins-updated',{detail:{loginId:id,service}}));
    }catch(error){if(token===revision&&id===current()?.id&&dialog.open){feedback(error.message,true);load();}}
    finally{busy=false;controls();}
  }
  function openImport(service){
    $('partner-import-service').value=service;$('partner-import').open=true;
    $('partner-import').scrollIntoView({block:'nearest'});$('partner-import-text').focus({preventScroll:true});
  }
  async function stopQr(){
    clearTimeout(timer);timer=null;const previous=flow;flow=null;$('partner-qr').hidden=true;controls();
    if(previous){try{await api(prefix+'/qr-cancel',{flow_id:previous.flow_id});}catch(_error){/* The server also expires abandoned QR sessions. */}}
  }
  async function startQr(service){
    if(busy||flow)return;
    const id=current()?.id,token=++revision;if(!id)return;
    busy=true;controls();feedback('正在从官方服务获取登录二维码…');
    try{
      const result=await api(prefix+'/qr-start',{login_id:id,service});
      if(token!==revision||!dialog.open||id!==current()?.id){await api(prefix+'/qr-cancel',{flow_id:result.flow_id});return;}
      flow=result;$('partner-qr-title').textContent=service==='taobao'?'淘宝 / 菜鸟扫码更新':'百度扫码更新';
      $('partner-qr-image').src=result.image;$('partner-qr-status').textContent=result.message;$('partner-qr').hidden=false;
      feedback('请用官方 APP 扫码，并在 APP 中确认此次登录。');timer=setTimeout(poll,500);
      $('partner-qr').scrollIntoView({block:'nearest'});
    }catch(error){if(token===revision&&id===current()?.id&&dialog.open)feedback(error.message,true);}
    finally{busy=false;controls();}
  }
  async function poll(){
    const expected=flow;if(!expected||!dialog.open)return;
    try{
      const result=await api(prefix+'/qr-poll',{flow_id:expected.flow_id});
      if(flow!==expected||result.login_id!==current()?.id)return;
      $('partner-qr-status').textContent=result.message||'等待官方确认…';
      if(result.state==='complete'){
        flow=null;$('partner-qr').hidden=true;model=result.status;render();feedback(result.message);
        window.dispatchEvent(new CustomEvent('partner-logins-updated',{detail:{loginId:result.login_id,service:expected.service}}));return;
      }
      if(result.state==='expired'){await stopQr();feedback('本次二维码已结束，请重新获取。');return;}
      if(result.state==='closed'){await stopQr();feedback(result.message||'本机扫码会话已结束，请重新获取。');return;}
      timer=setTimeout(poll,1500);
    }catch(error){if(flow===expected){await stopQr();feedback(error.message,true);}}
  }
  async function open(service){
    feedback('');if(!dialog.open)dialog.showModal();render();await load();
    if(dialog.open&&['baidu-map','taobao','kuaishou'].includes(service)){
      const card=dialog.querySelector(`[data-service="${service}"]`);
      card?.scrollIntoView({block:'nearest'});card?.querySelector('button')?.focus({preventScroll:true});
    }
  }
  $('checkin-partners-open').addEventListener('click',()=>open());
  window.addEventListener('partner-login-request',event=>open(event.detail.service));
  $('partner-close').addEventListener('click',()=>dialog.close());
  $('partner-qr-cancel').addEventListener('click',async()=>{await stopQr();feedback('本次扫码已结束。');load();});
  $('partner-import-submit').addEventListener('click',()=>update($('partner-import-service').value,$('partner-import-text').value));
  $('partner-import-file').addEventListener('change',async event=>{
    const file=event.target.files[0];if(!file)return;
    if(file.size>45000){feedback('请仅选择当前合作方的必要登录资料，文件不得超过 45 KB。',true);event.target.value='';return;}
    const token=revision,id=current()?.id,text=await file.text();
    if(token!==revision||id!==current()?.id||!dialog.open||event.target.files[0]!==file)return;
    $('partner-import-text').value=text;feedback('资料已读取，点击“验证并更新”后才会保存。');
  });
  $('partner-copy-directory').addEventListener('click',async()=>{
    try{await navigator.clipboard.writeText($('partner-data-directory').textContent);feedback('数据目录路径已复制。');}
    catch(_error){feedback('请选中数据目录路径后手动复制。');}
  });
  dialog.addEventListener('close',()=>{++revision;stopQr();$('partner-import-text').value='';$('partner-import-file').value='';});
  window.addEventListener('checkin-account-changed',()=>{++revision;stopQr();model=null;$('partner-import-text').value='';$('partner-import-file').value='';if(dialog.open){render();load();}});
})();
