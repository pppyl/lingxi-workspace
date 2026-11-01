(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const categories = {fee:'话费券',flow:'流量券',other:'其他卡券'};
  const accountStates = {active:'已登录',login_required:'需重新登录',restoring:'正在恢复',retry:'等待重连'};
  const couponStates = {available:'可使用',used:'已使用',expired:'已过期',unavailable:'不可使用',invalid:'已失效','官方状态 9':'已锁定／占用'};
  const useNames = {exchange:'兑换话费',recharge:'充值使用',rights:'兑换流量',flow:'兑换流量'};
  const formatter = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  let accounts = [], loginId = '', state = 'available', availableSort = 'default', items = [], page = 0, total = null, hasMore = false, loading = false, refreshing = false, queryFailed = false, requestVersion = 0;
  let useOperation = null, useBusy = false, useConfirming = false, useSubmitted = false, useGeneration = 0;
  let captchaToken = '', captchaRequired = false;
  let pendingOrders = [], paymentOrder = '';
  let useItem = null, relogin = null, reloginTimer = null, useRecheck = null;
  const loginSmsCooldowns = new Map();
  const loginRequiredCodes = ['ALIPAY_RELOGIN_REQUIRED','APP_LOGIN_REQUIRED','COUPON_LOGIN_REQUIRED','500003','531022'];
  let exchangeRiskToken = '', riskSdk = null;
  let successToastTimer = null;
  const availableCounts = new Map();
  let countsGeneration = 0;
  let refreshPending = false;
  let pendingExpiryTimer = null;

  function node(tag,className='',text){const item=document.createElement(tag);item.className=className;if(text!==undefined)item.textContent=text;return item;}
  function dateText(value){if(!value)return '—';const date=new Date(value);return Number.isNaN(date.getTime())?value:formatter.format(date);}
  function amountText(item){return item.amount?`${item.amount}${item.unit||''}`:'—';}
  function statusText(item){return couponStates[item.status]||item.status||(state==='available'?'可使用':'历史记录');}
  async function api(url,payload){
    const accountId=payload?.login_id||loginId;
    const response=await fetch(url,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!(response.headers.get('Content-Type')||'').includes('application/json')){
      const error=new Error(`本机服务返回异常页面（HTTP ${response.status}），请重启或更新应用后重试。`);
      error.status=response.status;error.code='LOCAL_API_RESPONSE_INVALID';throw error;
    }
    const result=await response.json();
    if(accountId&&loginRequiredCodes.includes(result.code)&&!$('coupons-use').open){
      window.dispatchEvent(new CustomEvent('open-account-manager',{detail:{loginId:accountId,message:'当前账号需要恢复登录，请在高亮行点击“重新登录”，完成后返回重试。'}}));
    }
    if(!response.ok||loginRequiredCodes.includes(result.code)){const error=new Error(result.error||result.message||'操作未完成，请稍后重试。');error.status=response.status;error.code=result.code;throw error;}
    return result;
  }
  function feedback(message){$('coupons-feedback').textContent=message;$('coupons-feedback').hidden=!message;}
  function emptyRow(message){const row=node('tr'),cell=node('td','coupons-empty',message);cell.colSpan=6;row.append(cell);return row;}

  function setPendingOrders(orders){
    clearTimeout(pendingExpiryTimer);pendingExpiryTimer=null;
    const now=Date.now();
    pendingOrders=orders.map(order=>({...order,_deadline:order._deadline!==undefined?order._deadline:typeof order.expires_in==='number'?now+order.expires_in*1000:null}));
    const deadlines=pendingOrders.map(order=>order._deadline).filter(deadline=>deadline!==null);
    if(!deadlines.length)return;
    const accountId=loginId,version=requestVersion,delay=Math.max(0,Math.ceil(Math.min(...deadlines)-now));
    pendingExpiryTimer=setTimeout(async()=>{
      if(accountId!==loginId||version!==requestVersion)return;
      const expired=new Set(pendingOrders.filter(order=>order._deadline!==null&&order._deadline<=Date.now()).map(order=>order.order_id));
      setPendingOrders(pendingOrders.filter(order=>!expired.has(order.order_id)));
      if(expired.has(paymentOrder)){closeUse(true);feedback('该订单付款窗口已结束，已移出待支付列表。');}
      render();
      try{
        const result=await api('/api/coupons/pending?'+new URLSearchParams({login_id:accountId}));
        if(accountId===loginId&&version===requestVersion){setPendingOrders(result.pending_orders);render();}
      }catch{
        // Local expiry already applied. A later normal refresh will sync storage;
        // a network failure must not start a payment/query retry loop.
      }
    },delay);
  }

  function renderAccounts(){
    const selected=accounts.find(account=>account.id===loginId);
    $('coupons-account-count').textContent=accounts.length;
    $('coupons-account-hint').textContent=accounts.length?'与其他功能共用登录':'请从左侧“账号管理”添加账号';
    $('coupons-current-account').textContent=selected?`${selected.name||selected.phone_masked} · ${selected.phone_masked}`:'尚未选择账号';
    $('coupons-accounts').replaceChildren(...accounts.map(account=>{
      const button=node('button','coupons-account'+(account.id===loginId?' selected':''));button.type='button';
      button.setAttribute('aria-pressed',String(account.id===loginId));
      const label=node('span','coupons-account-label'),name=node('strong','',account.name||account.phone_masked),status=node('span','coupons-account-state '+(account.status==='active'?'active':'attention'),accountStates[account.status]||'暂不可用');
      const count=availableCounts.get(account.id),ready=account.status==='active'&&count?.state==='ready'&&count.revision===account.credential_revision;
      const countBadge=node('span','coupons-account-coupons '+(ready?(count.value>0?'has-coupons':'no-coupons'):'unknown'),ready?`可用 ${count.value} 张`:account.status!=='active'?'—':count?.state==='failed'?'未获取':'查券中');
      countBadge.title=ready?'当前可使用的卡券数量':count?.error||'正在查询当前账号的可用卡券数量';
      label.append(name,countBadge,status);button.append(label,node('small','',account.phone_masked));
      button.addEventListener('click',()=>{if(account.id===loginId)return loadCards();closeUse(true);loginId=account.id;renderAccounts();return loadCards();});return button;
    }));
  }

  async function loadAvailableCounts(force=false){
    const generation=++countsGeneration,known=new Set(accounts.map(account=>account.id));
    for(const id of availableCounts.keys())if(!known.has(id))availableCounts.delete(id);
    const queue=accounts.filter(account=>account.status==='active'&&!(state==='available'&&account.id===loginId)&&
      (force||!availableCounts.has(account.id)||availableCounts.get(account.id).state==='loading'||availableCounts.get(account.id).revision!==account.credential_revision));
    await Promise.all(Array.from({length:Math.min(3,queue.length)},async()=>{
      while(generation===countsGeneration&&queue.length){
        const account=queue.shift(),record={revision:account.credential_revision,state:'loading'};
        availableCounts.set(account.id,record);renderAccounts();
        try{
          const response=await fetch('/api/coupons?'+new URLSearchParams({login_id:account.id,state:'available',page:'1',sort:'default'}));
          const result=await response.json();
          if(!response.ok)throw new Error(result.error||'卡券数量未获取，请刷新重试。');
          if(generation!==countsGeneration||availableCounts.get(account.id)!==record)continue;
          record.state='ready';record.value=result.items.filter(item=>item.status==='available').length;
        }catch(error){
          if(generation!==countsGeneration||availableCounts.get(account.id)!==record)continue;
          record.state='failed';record.error=error.message;
        }
        renderAccounts();
      }
    }));
  }

  function render(){
    const selected=accounts.find(account=>account.id===loginId);
    $('coupons-refresh').disabled=loading||refreshing;
    $('coupons-more').disabled=loading;
    $('coupons-more').textContent=loading?'正在加载…':'加载更多';
    $('coupons-more').hidden=!hasMore;
    $('coupons-sort').disabled=state==='history';
    $('coupons-order-note').hidden=state!=='history';
    for(const button of document.querySelectorAll('[data-coupons-state]')){
      const active=button.dataset.couponsState===state;button.classList.toggle('selected',active);button.setAttribute('aria-pressed',String(active));
    }
    const search=$('coupons-search').value.trim().toLocaleLowerCase(),category=$('coupons-category').value;
    const shown=items.filter(item=>(category==='all'||item.category===category)&&(!search||item.name.toLocaleLowerCase().includes(search)));
    const filtered=search||category!=='all';
    $('coupons-result-count').textContent=items.length?`${filtered?`当前已加载中筛选出 ${shown.length} 张 · `:''}已加载 ${items.length} 张${total===null?'':` / 共 ${total} 张`}`:loading?'正在查询…':queryFailed?'查询未完成':total===0?'共 0 张':'选择账号后查看卡券';
    const rows=shown.map(item=>{
      const row=node('tr'),name=node('td'),title=node('strong','coupons-name',item.name);name.append(title,node('small','coupons-source',item.source||'来源未提供'));
      const value=node('td','coupons-value',amountText(item)),categoryCell=node('td'),badge=node('span','coupons-category '+item.category,categories[item.category]||categories.other);categoryCell.append(badge);
      const status=node('td'),statusBadge=node('span','coupons-status '+(item.status==='available'||(!item.status&&state==='available')?'available':'history'),statusText(item));status.append(statusBadge);
      const action=node('td'),actions=node('div','coupons-row-actions'),details=node('button','quiet','详情');details.type='button';details.setAttribute('aria-label',`查看${item.name}详情`);details.addEventListener('click',()=>openDetails(item));
      const pending=pendingOrders.find(order=>order.coupon_id===item.id);
      if(pending||useNames[item.use_kind]){const label=pending?'继续支付':useNames[item.use_kind];const use=node('button','secondary coupons-use-button',label);use.type='button';use.setAttribute('aria-label',`${label}：${item.name}`);use.addEventListener('click',()=>pending?openPayment(pending):openUse(item));actions.append(use);}
      actions.append(details);action.append(actions);
      row.append(name,value,categoryCell,node('td','coupons-time',dateText(item.expires_at)),status,action);return row;
    });
    if(!rows.length){
      const message=!selected?(refreshing?'正在读取共享账号…':'共享账号池为空，请从左侧“账号管理”登录。'):selected.status!=='active'?'此账号暂不可用，请从左侧“账号管理”恢复登录。':loading?'正在查询该账号卡券…':queryFailed?'本次查询未完成，点击“刷新卡券”重试。':filtered?'已加载的卡券中没有匹配结果。':state==='available'?'此账号暂无可用卡券。':'此账号暂无卡券历史。';
      rows.push(emptyRow(message));
    }
    $('coupons-list').replaceChildren(...rows);
    $('coupons-pending').hidden=!pendingOrders.length;
    $('coupons-pending-list').replaceChildren(...pendingOrders.map(order=>{
      const line=node('div','coupons-pending-order'),label=node('div');
      label.append(node('strong','',order.coupon_name||'充值订单'),node('small','',`订单 ${order.order_id}${order.amount?' · 实付 '+order.amount+' 元':''}`));
      const button=node('button','secondary','继续支付');button.type='button';button.addEventListener('click',()=>openPayment(order));
      line.append(label,button);return line;
    }));
  }

  async function loadCards(more=false){
    const selected=accounts.find(account=>account.id===loginId),version=++requestVersion;
    if(!more){items=[];setPendingOrders([]);page=0;total=null;hasMore=false;queryFailed=false;feedback('');}
    if(!selected){loading=false;render();return;}
    loading=true;render();
    try{
      if(!more){
        const pending=await api('/api/coupons/pending?'+new URLSearchParams({login_id:loginId}));
        if(version!==requestVersion)return;
        setPendingOrders(pending.pending_orders);render();
      }
      if(selected.status!=='active')return;
      const query=new URLSearchParams({login_id:loginId,state,page:String(more?page+1:1),sort:$('coupons-sort').value});
      const result=await api('/api/coupons?'+query);
      if(version!==requestVersion)return;
      items=more?[...items,...result.items]:result.items;setPendingOrders(result.pending_orders||[]);page=result.page;total=result.total;hasMore=result.has_more;queryFailed=false;
      if(state==='available'){
        availableCounts.set(selected.id,{revision:selected.credential_revision,state:'ready',value:items.filter(item=>item.status==='available').length});
        renderAccounts();
      }
      $('coupons-sync').textContent='北京时间 · 最近查询 '+formatter.format(new Date());
    }catch(error){
      if(version!==requestVersion)return;
      if(state==='available'){
        availableCounts.set(selected.id,{revision:selected.credential_revision,state:'failed',error:error.message});renderAccounts();
      }
      queryFailed=true;feedback(error.message);if(!more)$('coupons-sync').textContent='查询未完成，点击“刷新卡券”重试。';
    }finally{if(version===requestVersion){loading=false;render();}}
  }

  async function refresh(forceCounts=false){
    if(refreshing){refreshPending=true;return;}refreshing=true;render();
    try{
      const result=await api('/api/coupons/accounts');accounts=result.accounts;
      if(!accounts.some(account=>account.id===loginId)){closeUse(true);loginId=accounts.find(account=>account.status==='active')?.id||accounts[0]?.id||'';}
      if(accounts.find(account=>account.id===loginId)?.status!=='active'&&!relogin?.required)closeUse(true);
      renderAccounts();loadAvailableCounts(forceCounts);await loadCards();
    }catch(error){feedback(error.message);$('coupons-account-hint').textContent='账号读取失败，请刷新重试。';}
    finally{refreshing=false;render();if(refreshPending){refreshPending=false;await refresh();}}
  }

  function openDetails(item){
    $('coupons-details-title').textContent=item.name;$('coupons-details-category').textContent=categories[item.category]||categories.other;
    $('coupons-details-amount').textContent=amountText(item);$('coupons-details-state').textContent=statusText(item);
    const meta=[];
    for(const [name,value] of [['所属账号',accounts.find(account=>account.id===loginId)?.phone_masked],['卡券来源',item.source],['有效期至',dateText(item.expires_at)]])meta.push(node('dt','',name),node('dd','',value||'—'));
    $('coupons-details-meta').replaceChildren(...meta);$('coupons-details-description').textContent=item.description||'官方暂未提供详细使用说明，请在中国移动 APP 中查看。';$('coupons-details').showModal();
  }

  function renderUseControls(){
    $('coupons-use-recheck').hidden=!useRecheck;
    $('coupons-use-recheck').disabled=useBusy||useConfirming;
    $('coupons-use-retry').hidden=!useRecheck?.can_retry;
    $('coupons-use-retry').disabled=useBusy||useConfirming;
    const terminal=useSubmitted&&!useConfirming;
    const needsLogin=Boolean(relogin?.required);
    $('coupons-use-confirm').disabled=needsLogin||useBusy||useSubmitted||!useOperation||(captchaRequired&&!captchaToken)||(useOperation.kind==='recharge'&&!useOperation.choices.length);
    $('coupons-use-confirm').hidden=needsLogin||terminal||(!useBusy&&!useOperation);
    $('coupons-use-confirm').textContent=useConfirming?'正在提交…':useOperation?(useOperation.kind==='recharge'?'生成支付订单':'确认兑换'):'请稍候';
    $('coupons-use-sms').disabled=needsLogin||useBusy||useSubmitted;
    $('coupons-use-code').disabled=needsLogin||useBusy||useSubmitted;
    $('coupons-use-charge').disabled=useBusy||useSubmitted;
    $('coupons-use-close').disabled=useConfirming;
    $('coupons-use-cancel').disabled=useConfirming;
    $('coupons-use-cancel').textContent=terminal?'关闭':'取消';
    $('coupons-use-get-payment').disabled=useBusy||(captchaRequired&&!captchaToken);
    $('coupons-use-get-payment').hidden=needsLogin||!paymentOrder;
    $('coupons-use-relogin').hidden=!needsLogin;
    const remaining=needsLogin?Math.max(0,Math.ceil(((loginSmsCooldowns.get(relogin.accountId)||0)-Date.now())/1000)):0;
    $('coupons-relogin-sms').disabled=Boolean(relogin?.busy)||remaining>0;
    $('coupons-relogin-sms').textContent=relogin?.busy?'请稍候…':remaining?`${remaining} 秒后重新获取`:'重新获取';
    $('coupons-relogin-code').disabled=Boolean(relogin?.busy);
    $('coupons-relogin-confirm').disabled=Boolean(relogin?.busy)||!relogin?.sent;
    $('coupons-relogin-confirm').textContent=relogin?.busy?'正在处理…':'登录并继续';
  }

  function closeUse(force=false){
    if(useConfirming&&!force)return;
    const refreshAfter=useSubmitted;
    ++useGeneration;useOperation=null;useItem=null;useBusy=false;useConfirming=false;useSubmitted=false;relogin=null;useRecheck=null;
    exchangeRiskToken='';
    clearInterval(reloginTimer);reloginTimer=null;$('coupons-use-relogin').hidden=true;
    $('coupons-relogin-code').value='';$('coupons-relogin-error').hidden=true;
    captchaToken='';captchaRequired=false;$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
    paymentOrder='';$('coupons-payment-qr').hidden=true;$('coupons-payment-qr').removeAttribute('src');$('coupons-use-get-payment').hidden=true;
    $('coupons-payment-panel').hidden=true;$('coupons-payment-url').value='';$('coupons-copy-payment').textContent='复制支付链接';
    $('coupons-use').classList.remove('coupons-payment-mode');
    $('coupons-use-form').reset();$('coupons-use-payment').hidden=true;$('coupons-use-payment').removeAttribute('href');
    $('coupons-use-sms').textContent='获取验证码';
    $('coupons-use-message').hidden=true;$('coupons-use-error').hidden=true;
    if($('coupons-use').open)$('coupons-use').close();
    if(refreshAfter&&!force)loadCards();
  }

  async function openUse(item){
    if(useConfirming)return;
    closeUse();const generation=useGeneration,accountId=loginId;
    useItem=item;
    useBusy=true;$('coupons-use-title').textContent=useNames[item.use_kind];$('coupons-use-meta').replaceChildren();
    $('coupons-use-description').textContent='正在读取官方卡券使用方式，请稍候。';
    $('coupons-use-sms-field').hidden=true;$('coupons-use-charge-field').hidden=true;renderUseControls();$('coupons-use').showModal();
    await prepareUse(item,generation,accountId);
  }

  function showUseSuccess(message){
    useConfirming=false;useSubmitted=true;closeUse();
    const toast=$('coupons-toast');
    clearTimeout(successToastTimer);toast.classList.remove('fading');
    toast.textContent='兑换成功 · '+message;toast.hidden=false;
    successToastTimer=setTimeout(()=>{toast.classList.add('fading');successToastTimer=setTimeout(()=>{toast.hidden=true;},250);},2750);
  }

  async function prepareUse(item,generation,accountId,retry=false){
    useRecheck=null;$('coupons-use-error').hidden=true;
    try{
      const prepared=await api('/api/coupons/use/prepare',{login_id:accountId,coupon_id:item.id,...(retry?{retry:true}:{})});
      if(generation!==useGeneration)return;
      if(prepared.state==='accepted'){showUseSuccess(prepared.message);return;}
      if(prepared.state==='unconfirmed'){
        useOperation=null;useRecheck=prepared;
        $('coupons-use-description').textContent='正在核实此前的兑换结果；查询不会再次使用卡券。';
        $('coupons-use-message').textContent=prepared.message;$('coupons-use-message').hidden=false;
        $('coupons-use-sms-field').hidden=true;$('coupons-use-charge-field').hidden=true;
        return;
      }
      if(prepared.state==='payment_required'){
        await openPayment({...prepared.order,coupon_name:prepared.order.coupon_name||item.name});
        return;
      }
      useOperation=prepared;$('coupons-use-title').textContent=useNames[prepared.kind];
      if(['exchange','rights','flow'].includes(prepared.kind)){
        await loadExchangeRiskSdk();
        if(generation!==useGeneration)return;
      }
      const meta=[];for(const [name,value] of [['使用账号',prepared.account_masked],['卡券名称',prepared.coupon_name],['卡券面额',prepared.coupon_amount+' '+(prepared.coupon_unit||(['rights','flow'].includes(prepared.kind)?'':'元'))]])meta.push(node('dt','',name),node('dd','',value||'—'));
      $('coupons-use-meta').replaceChildren(...meta);$('coupons-use-description').textContent=prepared.description||'请核对卡券和账号后确认。';
      $('coupons-use-sms-field').hidden=!prepared.requires_sms;$('coupons-use-code').required=prepared.requires_sms;
      $('coupons-use-charge-field').hidden=prepared.kind!=='recharge';$('coupons-use-charge').required=prepared.kind==='recharge';
      const best=prepared.choices[0];
      $('coupons-use-charge').value=best?.value||'';
      $('coupons-use-best').textContent=best?.label||'暂无可用充值方案';
      $('coupons-use-message').textContent=prepared.message||'核对后点击确认才会提交用券。';$('coupons-use-message').hidden=false;
    }catch(error){
      if(generation!==useGeneration)return;
      if(loginRequiredCodes.includes(error.code)){await requireInlineLogin('prepare');return;}
      useOperation=null;
      $('coupons-use-error').textContent=error.message;$('coupons-use-error').hidden=false;$('coupons-use-description').textContent='准备未完成，本次没有提交用券。';
    }finally{if(generation===useGeneration){useBusy=false;renderUseControls();}}
  }

  $('coupons-use-recheck').addEventListener('click',()=>{if(!useBusy&&useItem){useBusy=true;renderUseControls();return prepareUse(useItem,useGeneration,loginId);}});
  $('coupons-use-retry').addEventListener('click',()=>{if(!useBusy&&useItem&&useRecheck?.can_retry){useBusy=true;renderUseControls();return prepareUse(useItem,useGeneration,loginId,true);}});

  $('coupons-use-sms').addEventListener('click',async()=>{
    if(!useOperation||useBusy||useSubmitted)return;
    const generation=useGeneration;useBusy=true;$('coupons-use-error').hidden=true;renderUseControls();
    try{
      const result=await api('/api/coupons/use/sms',{operation_id:useOperation.operation_id});
      if(generation!==useGeneration)return;
      $('coupons-use-message').textContent=result.message;$('coupons-use-message').hidden=false;$('coupons-use-code').value='';
      $('coupons-use-sms').textContent='重新获取验证码';
    }catch(error){if(generation===useGeneration){
      if(loginRequiredCodes.includes(error.code)){await requireInlineLogin('prepare');return;}
      $('coupons-use-error').textContent=error.message;$('coupons-use-error').hidden=false;
    }}
    finally{if(generation===useGeneration){useBusy=false;renderUseControls();}}
  });

  $('coupons-use-form').addEventListener('submit',async event=>{
    event.preventDefault();if(relogin?.required){$('coupons-relogin-confirm').click();return;}if(!useOperation||useBusy||useSubmitted)return;
    const generation=useGeneration;useBusy=true;useConfirming=true;useSubmitted=true;$('coupons-use-error').hidden=true;renderUseControls();
    try{
      const payload={operation_id:useOperation.operation_id,sms_code:$('coupons-use-code').value.trim(),charge_amount:$('coupons-use-charge').value,captcha_token:captchaToken};
      if(['exchange','rights','flow'].includes(useOperation.kind)){
        const verified=await api('/api/coupons/use/confirm',{...payload,prepare_risk:true});
        if(generation!==useGeneration)return;
        if(verified.state!=='risk_required')throw new Error('官方用券步骤未完成，请重新打开用券窗口。');
        exchangeRiskToken=await prepareExchangeRisk(verified.sdk_server);
        if(generation!==useGeneration)return;
        const lid=document.cookie.match(/(?:^|;\s*)_zw_kvani5r=([^;]*)/);
        if(!lid?.[1]){
          const error=new Error('官方用券校验未完成，请重试；已通过的短信校验会保留。');
          error.code='RISK_INITIALIZATION_FAILED';throw error;
        }
        Object.assign(payload,{risk_token:exchangeRiskToken,risk_lid:lid[1]});
      }
      const result=await api('/api/coupons/use/confirm',payload);
      if(generation!==useGeneration)return;
      if(result.state==='accepted'){
        showUseSuccess(result.message);
        return;
      }
      captchaToken='';
      if(result.state==='verification_required'&&result.verification==='captcha'){
        useSubmitted=false;captchaRequired=true;
        $('coupons-use-verification').hidden=false;
        $('coupons-use-verification').querySelector('p').textContent='完成验证后会自动继续，无需再次点击。';
        const frame=node('iframe');frame.src='/static/coupon-captcha.html';frame.title='中国移动官方滑块验证';
        $('coupons-use-captcha').replaceChildren(frame);
      }else{$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();}
      const labels={accepted:'已受理',payment_required:'等待付款',unknown:'结果待核实'};
      $('coupons-use-message').textContent=(labels[result.state]?labels[result.state]+' · ':'')+result.message+(result.order_id?' 订单号：'+result.order_id:'');$('coupons-use-message').hidden=false;
      if(result.state==='payment_required'&&result.payment_url){$('coupons-use-payment').href=result.payment_url;$('coupons-use-payment').hidden=false;}
      if(result.state==='payment_required'){
        paymentOrder=result.order_id;useConfirming=false;
        await loadPayment(generation);
      }
    }catch(error){
      if(generation!==useGeneration)return;
      if(loginRequiredCodes.includes(error.code)){useConfirming=false;useSubmitted=false;await requireInlineLogin('prepare');return;}
      if(['SMS_REQUIRED','510002','510003','INVALID_AMOUNT','CAPTCHA_REQUIRED','RISK_INITIALIZATION_FAILED'].includes(error.code))useSubmitted=false;
      $('coupons-use-error').textContent=error.status||error.code==='RISK_INITIALIZATION_FAILED'?error.message:'提交结果待核实，请先在中国移动 APP 中查看，本页不会自动重试。';$('coupons-use-error').hidden=false;
    }finally{if(generation===useGeneration){useBusy=false;useConfirming=false;renderUseControls();}}
  });

  async function loadPayment(generation=useGeneration,refreshLink=false){
    useBusy=true;renderUseControls();$('coupons-use-error').hidden=true;
    if(!refreshLink||!/^https:\/\/(?:[a-z0-9-]+\.)*alipay\.com(?::443)?(?:[/?#]|$)/i.test($('coupons-payment-url').value)){
      $('coupons-payment-panel').hidden=true;$('coupons-payment-url').value='';$('coupons-copy-payment').textContent='复制支付链接';
      $('coupons-use-payment').hidden=true;$('coupons-use-payment').removeAttribute('href');
      $('coupons-payment-qr').hidden=true;$('coupons-payment-qr').removeAttribute('src');
    }
    $('coupons-use-message').textContent='正在核对订单并获取官方支付链接…';$('coupons-use-message').hidden=false;
    try{
      const submittedToken=captchaToken;captchaToken='';
      const result=await api('/api/coupons/use/payment',{login_id:loginId,order_id:paymentOrder,refresh:refreshLink,captcha_token:submittedToken});
      if(generation!==useGeneration)return;
      captchaToken='';
      if(result.state==='verification_required'){
        captchaRequired=true;$('coupons-use-verification').hidden=false;
        $('coupons-use-verification').querySelector('p').textContent='完成验证后会自动获取付款链接，无需再次点击。';
        const frame=node('iframe');frame.title='支付宝支付前的官方滑块验证';
        frame.src='/static/coupon-captcha.html?'+new URLSearchParams(result.challenge);
        $('coupons-use-captcha').replaceChildren(frame);
        $('coupons-use-message').textContent=result.message;
        $('coupons-use-get-payment').textContent='验证后继续获取支付链接';
        return;
      }
      captchaRequired=false;$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
      if(result.state==='closed'){
        const closedOrder=paymentOrder;paymentOrder='';$('coupons-use-message').textContent=result.message;
        setPendingOrders(pendingOrders.filter(order=>order.order_id!==closedOrder));render();
        $('coupons-use-payment').hidden=true;$('coupons-use-payment').removeAttribute('href');
        $('coupons-payment-url').value='';$('coupons-payment-qr').hidden=true;$('coupons-payment-qr').removeAttribute('src');$('coupons-payment-panel').hidden=true;return;
      }
      $('coupons-use-message').textContent=`实付 ${result.amount} 元 · 订单 ${result.order_id}，请在支付宝核对后付款。`;
      $('coupons-use-payment').href=result.payment_url;$('coupons-use-payment').hidden=false;
      $('coupons-use-payment').textContent='打开网页付款';
      $('coupons-use-payment').target='_blank';
      $('coupons-payment-qr').src=result.qr_image;$('coupons-payment-qr').hidden=false;
      $('coupons-payment-url').value=result.payment_url;$('coupons-payment-panel').hidden=false;
      $('coupons-use').classList.add('coupons-payment-mode');$('coupons-use-title').textContent='支付宝付款';
      $('coupons-use-get-payment').textContent='刷新支付链接';
    }catch(error){
      if(generation!==useGeneration)return;
      captchaRequired=false;captchaToken='';$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
      if(loginRequiredCodes.includes(error.code)){await requireInlineLogin('payment');return;}
      $('coupons-use-error').textContent=error.message;$('coupons-use-error').hidden=false;
      $('coupons-use-message').textContent=$('coupons-payment-url').value?'本次未更新链接，已保留之前取得的网页入口，请以官方收银台的订单状态为准。':'订单已保留，获取链接不会重新下单。';
    }finally{if(generation===useGeneration){useBusy=false;renderUseControls();}}
  }

  async function requireInlineLogin(resume){
    if(!relogin)relogin={accountId:loginId,generation:useGeneration,required:true,busy:false,requested:false,sent:false};
    const context=relogin;
    relogin.required=true;relogin.resume=resume;
    captchaRequired=false;captchaToken='';$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
    $('coupons-use-error').hidden=true;$('coupons-payment-panel').hidden=true;$('coupons-payment-url').value='';
    $('coupons-use-message').textContent=paymentOrder?'订单已保留。登录成功后自动获取这笔订单的支付宝链接。':'登录成功后重新读取此券，核对后再确认用券。';
    $('coupons-use-message').hidden=false;
    if(!relogin.requested){
      relogin.requested=true;
      if((loginSmsCooldowns.get(loginId)||0)>Date.now()){
        relogin.sent=true;$('coupons-relogin-message').textContent='登录验证码已发送，请填写最近收到的验证码。';
      }else await sendInlineLoginSms();
    }else $('coupons-relogin-message').textContent='官方仍要求重新登录，请核对网络后重试；不会自动重复发送短信。';
    if(context!==relogin||context.generation!==useGeneration)return;
    if(!reloginTimer)reloginTimer=setInterval(renderUseControls,1000);
    renderUseControls();$('coupons-relogin-code').focus();
  }

  async function sendInlineLoginSms(){
    const context=relogin;if(!context||context.busy)return;
    context.busy=true;$('coupons-relogin-error').hidden=true;
    $('coupons-relogin-message').textContent='正在向当前账号发送登录验证码…';renderUseControls();
    try{
      const result=await api('/api/logins/relogin/sms',{id:context.accountId,force:true,resend:true});
      if(context!==relogin||context.generation!==useGeneration)return;
      context.sent=true;loginSmsCooldowns.set(context.accountId,Date.now()+result.retry_after*1000);
      $('coupons-relogin-code').value='';
      $('coupons-relogin-message').textContent=`登录验证码已发送到 ${result.phone_masked}。输入后重新登录共享账号。`;
    }catch(error){
      if(context!==relogin)return;
      $('coupons-relogin-message').textContent='登录验证码未发送成功，可点击“重新获取”重试。';
      $('coupons-relogin-error').textContent=error.message;$('coupons-relogin-error').hidden=false;
    }finally{if(context===relogin){context.busy=false;renderUseControls();}}
  }

  $('coupons-relogin-sms').addEventListener('click',()=>{
    if(relogin&&(loginSmsCooldowns.get(relogin.accountId)||0)<=Date.now())return sendInlineLoginSms();
  });
  $('coupons-relogin-code').addEventListener('keydown',event=>{
    if(event.key==='Enter'){event.preventDefault();$('coupons-relogin-confirm').click();}
  });
  $('coupons-relogin-confirm').addEventListener('click',async()=>{
    const context=relogin;if(!context?.required||context.busy||!context.sent)return;
    const code=$('coupons-relogin-code').value.trim();
    if(!/^\d{6}$/.test(code)){
      $('coupons-relogin-error').textContent='请输入 6 位登录验证码。';$('coupons-relogin-error').hidden=false;return;
    }
    context.busy=true;$('coupons-relogin-error').hidden=true;renderUseControls();
    try{
      const result=await api('/api/logins/relogin',{id:context.accountId,code,force:true});
      if(context!==relogin||context.generation!==useGeneration)return;
      accounts=accounts.map(account=>account.id===context.accountId?result.account:account);renderAccounts();
      context.required=false;context.busy=false;$('coupons-relogin-code').value='';
      clearInterval(reloginTimer);reloginTimer=null;
      window.publishAccountsChanged?.();
      renderUseControls();
      if(context.resume==='payment')await loadPayment(context.generation,true);
      else{
        useOperation=null;useSubmitted=false;useBusy=true;renderUseControls();
        await prepareUse(useItem,context.generation,context.accountId);
      }
    }catch(error){
      if(context!==relogin)return;
      $('coupons-relogin-error').textContent=error.message;$('coupons-relogin-error').hidden=false;
    }finally{if(context===relogin){context.busy=false;renderUseControls();}}
  });

  async function openPayment(order){
    if(useConfirming)return;
    closeUse();paymentOrder=order.order_id;useSubmitted=true;
    $('coupons-use').classList.add('coupons-payment-mode');$('coupons-use-title').textContent='继续支付';
    $('coupons-use-meta').replaceChildren(node('dt','','使用账号'),node('dd','',accounts.find(a=>a.id===loginId)?.phone_masked||''),node('dt','','卡券名称'),node('dd','',order.coupon_name||'充值订单'),node('dt','','订单号'),node('dd','',order.order_id));
    $('coupons-use-description').textContent='继续已有订单，付款由你在官方支付页确认。';
    $('coupons-use-sms-field').hidden=true;$('coupons-use-charge-field').hidden=true;
    $('coupons-use-get-payment').textContent='获取支付链接';
    renderUseControls();$('coupons-use').showModal();await loadPayment();
  }
  $('coupons-use-get-payment').addEventListener('click',()=>{if(!useBusy&&paymentOrder)loadPayment(useGeneration,true);});
  $('coupons-copy-payment').addEventListener('click',async()=>{
    const field=$('coupons-payment-url');
    field.focus();field.select();
    try{await navigator.clipboard.writeText(field.value);$('coupons-copy-payment').textContent='已复制';}
    catch{$('coupons-copy-payment').textContent='链接已选中，请按 Ctrl+C 复制';}
  });

  function loadExchangeRiskSdk(){
    if(!riskSdk)riskSdk=new Promise((resolve,reject)=>{
      const script=node('script');script.src='/coupon-verification/wallet/index-private-web_a54cc0c.js';
      script.onload=resolve;script.onerror=()=>{riskSdk=null;reject(new Error('官方用券组件加载失败，请重新打开用券窗口。'));};
      document.head.append(script);
    });
    return riskSdk;
  }

  function prepareExchangeRisk(server){
    $('coupons-use-message').textContent='正在获取官方用券校验凭据…';$('coupons-use-message').hidden=false;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('官方用券校验超时，请重试；已通过的短信校验会保留。')),40000);
      loadExchangeRiskSdk().then(()=>window._zw.ConstID({
        appId:'e1f92650d20263d38dbe327e7237f516',server:location.origin+server,cache:false
      },(error,token)=>{
        clearTimeout(timer);
        if(error||typeof token!=='string'||!token)reject(new Error('官方用券校验未完成，请重试；已通过的短信校验会保留。'));
        else resolve(token);
      })).catch(error=>{clearTimeout(timer);reject(error);});
    }).catch(error=>{error.code='RISK_INITIALIZATION_FAILED';throw error;});
  }

  window.addEventListener('message',event=>{
    const frame=$('coupons-use-captcha').querySelector('iframe');
    if(useBusy||!captchaRequired||!frame||event.source!==frame.contentWindow||event.origin!==location.origin)return;
    if(event.data?.type==='coupon-captcha-success'&&typeof event.data.token==='string'&&event.data.token.length>0&&event.data.token.length<=4096){
      captchaToken=event.data.token;captchaRequired=false;
      $('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
      $('coupons-use-message').textContent='验证已完成，正在自动继续…';
      $('coupons-use-message').hidden=false;renderUseControls();
      if(paymentOrder)loadPayment(useGeneration,true);
      else $('coupons-use-form').requestSubmit();
    }
  });

  $('coupons-refresh').addEventListener('click',()=>refresh(true));
  $('coupons-more').addEventListener('click',()=>{if(!loading)loadCards(true);});
  $('coupons-search').addEventListener('input',render);$('coupons-category').addEventListener('change',render);
  $('coupons-sort').addEventListener('change',()=>{availableSort=$('coupons-sort').value;loadCards();});
  for(const button of document.querySelectorAll('[data-coupons-state]'))button.addEventListener('click',()=>{if(state===button.dataset.couponsState)return;state=button.dataset.couponsState;$('coupons-sort').value=state==='history'?'default':availableSort;loadCards();});
  $('coupons-details-close').addEventListener('click',()=>$('coupons-details').close());
  $('coupons-use-close').addEventListener('click',()=>closeUse());$('coupons-use-cancel').addEventListener('click',()=>closeUse());
  $('coupons-use').addEventListener('cancel',event=>{event.preventDefault();closeUse();});
  window.addEventListener('accounts-changed',()=>refresh());
  refresh();
})();
