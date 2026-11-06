(() => {
  'use strict';
  const $=id=>document.getElementById(id),busy={rights:'',youth:'',points:''};
  let accounts=[],loginId='',version=0,items=[],coupons=[],points=null,claimedAmount='',stopBatch=false,smsRemaining=0,smsTimer=null,pendingYouthCoupon='',rightsComplete=false;
  const reads=new Map();
  let refreshing=false,refreshPending=false;
  const time=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const node=(tag,cls='',text)=>{const el=document.createElement(tag);el.className=cls;if(text!==undefined)el.textContent=text;return el;};
  const writing=()=>Object.values(busy).some(value=>value==='claim'||value==='sms');
  async function api(operation,payload){
    const response=await fetch('/api/mobile-rights/'+operation,payload===undefined?undefined:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!(response.headers.get('Content-Type')||'').includes('application/json'))throw new Error('本机服务返回异常，请刷新页面或重启服务后重试。');
    const result=await response.json();
    if(!response.ok||result.error){const error=new Error(result.error||'本次操作未完成。');error.code=result.code;error.retryAfter=result.retry_after;throw error;}
    return result;
  }
  function message(kind,text,tone=''){
    const box=$('rights-'+(kind==='rights'?'message':kind+'-message'));
    box.textContent=text;box.hidden=!text;box.className='rights-message'+(tone?' '+tone:'');
  }
  function controls(){
    const any=Object.values(busy).some(Boolean);
    $('rights-refresh').disabled=!loginId||any;
    $('rights-query').disabled=!loginId||Boolean(busy.rights);
    $('rights-claim-all').disabled=!loginId||Boolean(busy.rights)||!items.some(item=>item.can_claim);
    $('rights-claim-all').textContent=busy.rights==='claim'?'正在领取…':'领取全部待领权益';
    $('rights-youth-query').disabled=!loginId||Boolean(busy.youth);
    $('rights-youth-coupon').disabled=Boolean(busy.youth)||!coupons.length;
    $('rights-youth-sms').disabled=!loginId||Boolean(busy.youth)||!$('rights-youth-coupon').value||smsRemaining>0;
    $('rights-youth-sms').textContent=smsRemaining?`${smsRemaining} 秒后重发`:busy.youth==='sms'?'发送中…':'获取验证码';
    $('rights-youth-code').disabled=Boolean(busy.youth)||!coupons.length;
    $('rights-youth-claim').disabled=!loginId||Boolean(busy.youth)||pendingYouthCoupon!==$('rights-youth-coupon').value||!$('rights-youth-coupon').value||!/^\d{6}$/.test($('rights-youth-code').value);
    $('rights-points-query').disabled=!loginId||Boolean(busy.points);
    $('rights-points-claim').disabled=!loginId||Boolean(busy.points)||!points?.can_claim;
    $('rights-track-add').disabled=!loginId||Boolean(busy.rights);
    for(const button of $('rights-accounts').children)button.disabled=writing();
    for(const button of $('rights-rows').querySelectorAll('button'))button.disabled=Boolean(busy.rights);
  }
  function renderAccounts(){
    $('rights-account-count').textContent=accounts.length;
    $('rights-accounts').replaceChildren(...accounts.map(account=>{
      const button=node('button','rights-account'+(account.id===loginId?' selected':''));button.type='button';
      button.setAttribute('aria-pressed',String(account.id===loginId));button.dataset.loginId=account.id;
      const label=node('span');label.append(node('strong','',account.name||account.phone_masked),node('small','rights-account-state'+(account.status==='login_required'?' attention':''),account.status==='login_required'?'APP需验证':'共享账号'));
      button.append(label,node('small','',account.phone_masked));button.addEventListener('click',()=>selectAccount(account.id));return button;
    }));
    const selected=accounts.find(account=>account.id===loginId);
    $('rights-owner').textContent=selected?`${selected.name||selected.phone_masked} · ${selected.phone_masked}`:'选择账号后查询';
    controls();
  }
  function renderRights(){
    const search=$('rights-search').value.trim().toLocaleLowerCase(),pending=$('rights-pending-only').checked,tracked=$('rights-tracked-only').checked;
    const shown=items.filter(item=>(!pending||item.can_claim)&&(!tracked||item.tracked)&&`${item.name} ${item.status} ${item.note||''}`.toLocaleLowerCase().includes(search));
    $('rights-item-count').textContent=`${shown.length} / ${items.length}`;
    const rows=shown.map(item=>{
      const row=node('tr'),name=node('td');name.append(node('strong','',item.name));
      const status=node('td'),badge=node('span','rights-state'+(item.can_claim?' pending':['0','1'].includes(item.status_code)?' success':item.status.includes('核实')?' attention':''),item.status);status.append(badge);
      const actions=node('td'),buttons=node('div','rights-row-actions');
      if(item.can_claim){const claim=node('button','primary','领取');claim.type='button';claim.dataset.action='claim';claim.dataset.id=item.id;buttons.append(claim);}
      const track=node('button','secondary',item.tracked?'移除记录':'添加记录');track.type='button';track.dataset.action='track';track.dataset.id=item.id||'';track.dataset.name=item.name;track.dataset.enabled=String(!item.tracked);buttons.append(track);actions.append(buttons);
      row.append(name,node('td','',item.order_time?time.format(new Date(item.order_time)):'—'),node('td','',item.order_no||'—'),status,node('td','rights-row-note',item.note||'—'),actions);return row;
    });
    if(!rows.length){const row=node('tr'),cell=node('td','rights-empty',busy.rights==='query'?'正在查询本月权益…':!loginId?'请先从账号管理添加账号。':items.length?'没有匹配的权益。':'本账号暂无本月权益记录。');cell.colSpan=6;row.append(cell);rows.push(row);}
    $('rights-rows').replaceChildren(...rows);controls();
  }
  function renderYouth(){
    $('rights-youth-coupon').replaceChildren(...(coupons.length?coupons:[{id:'',name:'本账号暂无可用兑换券'}]).map(coupon=>{const option=node('option','',coupon.name);option.value=coupon.id;return option;}));controls();
  }
  function renderPoints(){
    $('rights-points-pending').textContent=points?.pending_amount??'—';$('rights-points-balance').textContent=points?.total_balance??'—';$('rights-points-claimed').textContent=claimedAmount||'—';controls();
  }
  async function query(kind,preserveSms=false){
    const id=loginId,epoch=version;if(!id||busy[kind])return;
    if(kind==='youth'&&preserveSms&&pendingYouthCoupon)return;
    if(kind==='youth'){pendingYouthCoupon='';coupons=[];$('rights-youth-code').value='';renderYouth();}
    busy[kind]='query';message(kind,'正在查询…');if(kind==='rights')renderRights();else controls();
    const operation={rights:'query',youth:'youth-query',points:'points-query'}[kind];
    try{
      const key=id+'|'+kind;
      let pending=reads.get(key);
      if(!pending){pending=api(operation,{login_id:id}).finally(()=>reads.delete(key));reads.set(key,pending);}
      const result=await pending;if(epoch!==version)return;
      if(kind==='rights'){items=result.items;rightsComplete=Boolean(result.complete);renderRights();message(kind,(result.warnings||[]).join('\n'));}
      else if(kind==='youth'){coupons=result.coupons;pendingYouthCoupon='';$('rights-youth-code').value='';renderYouth();message(kind,coupons.length?'':'本账号暂无可用的新青年兑换券。');}
      else{points=result;renderPoints();message(kind,'');}
    }catch(error){if(epoch===version)message(kind,kind==='points'&&claimedAmount?`已领取 ${claimedAmount}，当前余额暂未核实。${error.message}`:error.message,'error');}
    finally{if(epoch===version){busy[kind]='';if(kind==='rights')renderRights();else controls();}}
  }
  async function selectAccount(id){
    if(writing())return;
    const changed=id!==loginId;loginId=id;version++;
    for(const key of Object.keys(busy))busy[key]='';
    if(changed){items=[];rightsComplete=false;coupons=[];points=null;claimedAmount='';pendingYouthCoupon='';if(smsTimer!==null)clearInterval(smsTimer);smsTimer=null;smsRemaining=0;$('rights-youth-code').value='';}
    renderAccounts();renderRights();renderYouth();renderPoints();
    await Promise.allSettled(['rights','youth','points'].map(kind=>query(kind,true)));
  }
  async function refreshAccounts(){
    if(refreshing||writing()){refreshPending=true;return;}
    refreshing=true;
    try{
      const result=await api('accounts');accounts=result.accounts;
      $('rights-account-error').hidden=true;
      const selected=accounts.some(account=>account.id===loginId)?loginId:accounts.find(account=>account.status==='active')?.id||accounts[0]?.id||'';
      if(selected)await selectAccount(selected);else{loginId='';renderAccounts();renderRights();}
    }catch(error){$('rights-account-error').textContent=error.message;$('rights-account-error').hidden=false;}
    finally{refreshing=false;if(refreshPending){refreshPending=false;void refreshAccounts();}}
  }
  function countdown(seconds){
    if(smsTimer!==null)clearInterval(smsTimer);smsRemaining=seconds;
    smsTimer=setInterval(()=>{smsRemaining=Math.max(0,smsRemaining-1);if(!smsRemaining){clearInterval(smsTimer);smsTimer=null;}controls();},1000);controls();
  }
  async function claimRights(ids){
    if(busy.rights||!loginId)return;const epoch=version,id=loginId;
    busy.rights='claim';stopBatch=false;$('rights-stop').hidden=ids.length<2;$('rights-stop').disabled=false;controls();
    let success=0,finalMessage='',finalTone='success';
    try{
      for(const [index,action] of ids.entries()){
        if(stopBatch||epoch!==version)break;
        message('rights',`正在领取 ${index+1} / ${ids.length}…`);
        const result=await api('claim',{login_id:id,id:action});if(epoch!==version)return;
        if(result.status==='success')success++;else{finalMessage=result.message;finalTone='error';stopBatch=true;break;}
      }
      if(!finalMessage)finalMessage=`已领取 ${success} 项权益。`+(stopBatch?'已停止后续领取。':'');
    }catch(error){if(epoch===version){finalMessage=error.message;finalTone='error';}}
    finally{if(epoch===version){busy.rights='';$('rights-stop').hidden=true;await query('rights');message('rights',finalMessage,finalTone);controls();if(refreshPending){refreshPending=false;void refreshAccounts();}}}
  }
  async function track(name,enabled){
    if(!name.trim())return;
    const id=loginId,epoch=version;
    try{
      const result=await api('track',{login_id:id,name:name.trim(),enabled});if(epoch!==version)return;
      items=items.filter(item=>item.order_no||result.tracked.includes(item.name)).map(item=>({...item,tracked:result.tracked.includes(item.name)}));
      for(const watched of result.tracked)if(!items.some(item=>item.name===watched))items.push({id:null,name:watched,order_no:'',order_time:null,status:rightsComplete?'本月未发放':'待核对',status_code:'',can_claim:false,tracked:true,note:''});
      renderRights();$('rights-track-name').value='';
    }catch(error){if(epoch===version)message('rights',error.message,'error');}
  }
  async function youthAction(kind){
    if(busy.youth)return;const epoch=version,id=loginId,coupon=$('rights-youth-coupon').value;
    busy.youth=kind;controls();message('youth',kind==='sms'?'正在发送活动验证码…':'正在领取新青年瑞幸…');
    try{
      const result=await api(kind==='sms'?'youth-sms':'youth-claim',{login_id:id,id:coupon,...(kind==='claim'?{code:$('rights-youth-code').value}:{})});
      if(epoch!==version)return;
      if(kind==='sms'){pendingYouthCoupon=coupon;countdown(result.retry_after||70);}
      else{pendingYouthCoupon='';coupons=coupons.filter(item=>item.id!==coupon);$('rights-youth-code').value='';renderYouth();}
      message('youth',result.message,'success');
    }catch(error){if(epoch===version){message('youth',error.message,'error');if(error.retryAfter)countdown(error.retryAfter);}}
    finally{if(epoch===version){busy.youth='';controls();if(refreshPending){refreshPending=false;void refreshAccounts();}}}
  }
  async function claimPoints(){
    if(busy.points||!points?.claim_id)return;const epoch=version,id=loginId,claimId=points.claim_id;
    busy.points='claim';points={...points,can_claim:false};controls();
    try{
      const result=await api('points-claim',{login_id:id,id:claimId});if(epoch!==version)return;
      claimedAmount=result.claimed_amount;points={pending_amount:null,total_balance:null,can_claim:false,claim_id:null};renderPoints();message('points',result.message,'success');
      busy.points='';await query('points');
    }catch(error){if(epoch===version)message('points',error.message,'error');}
    finally{if(epoch===version){busy.points='';renderPoints();if(refreshPending){refreshPending=false;void refreshAccounts();}}}
  }
  $('rights-refresh').addEventListener('click',refreshAccounts);
  $('rights-query').addEventListener('click',()=>query('rights'));
  $('rights-youth-query').addEventListener('click',()=>query('youth'));
  $('rights-points-query').addEventListener('click',()=>query('points'));
  $('rights-claim-all').addEventListener('click',()=>claimRights(items.filter(item=>item.can_claim).map(item=>item.id)));
  $('rights-stop').addEventListener('click',()=>{stopBatch=true;$('rights-stop').disabled=true;});
  $('rights-rows').addEventListener('click',event=>{const button=event.target.closest('button[data-action]');if(!button)return;if(button.dataset.action==='claim')void claimRights([button.dataset.id]);else void track(button.dataset.name,button.dataset.enabled==='true');});
  $('rights-track-add').addEventListener('click',()=>track($('rights-track-name').value,true));
  $('rights-youth-sms').addEventListener('click',()=>youthAction('sms'));
  $('rights-youth-claim').addEventListener('click',()=>youthAction('claim'));
  $('rights-youth-code').addEventListener('input',controls);
  $('rights-youth-coupon').addEventListener('change',()=>{pendingYouthCoupon='';$('rights-youth-code').value='';controls();});
  $('rights-points-claim').addEventListener('click',claimPoints);
  for(const name of ['rights-search','rights-pending-only','rights-tracked-only'])$(name).addEventListener(name==='rights-search'?'input':'change',renderRights);
  window.addEventListener('accounts-changed',refreshAccounts);
  renderRights();renderPoints();controls();void refreshAccounts();
})();
