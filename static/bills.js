(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const accountStates = {active:'已登录',login_required:'需重新登录',restoring:'正在恢复',retry:'等待重连'};
  const loginRequiredCodes = new Set(['APP_LOGIN_REQUIRED','APP_SESSION_EXPIRED','BILL_LOGIN_REQUIRED','BILLS_LOGIN_REQUIRED','USAGE_LOGIN_REQUIRED','SERVICES_LOGIN_REQUIRED']);
  const moneyFields = ['total','payable','original','discount','rebate','balance'];
  const formatter = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  let accounts = [], months = [], loginId = '', month = '', bill = null, errorMessage = '', errorCode = '';
  let usage = null, usageErrorMessage = '', usageErrorCode = '', informationErrorMessage = '';
  let services = null, servicesErrorMessage = '', servicesErrorCode = '', servicesCategory = '';
  let loading = false, usageLoading = false, servicesLoading = false, refreshing = false, refreshPending = false, requestVersion = 0, usageRequestVersion = 0, servicesRequestVersion = 0;
  const expanded = new Set(), usageExpanded = new Set(), serviceDetails = new Set();
  let cancellation = null, cancelPending = false, cancelVersion = 0, cancelTimer = null, cancelCloseTimer = null;

  function node(tag,className='',text){const item=document.createElement(tag);item.className=className;if(text!==undefined)item.textContent=text;return item;}
  function monthText(value){return value ? `${value.slice(0,4)} 年 ${Number(value.slice(4))} 月` : '月度账单';}
  function amountText(value){return value===null||value===undefined||value===''?'—':String(value);}
  function hasBillDetails(item){return Boolean(item.children?.length)&&item.compact_details!==true;}
  function applyAccount(account,accountId){
    if(account?.id!==accountId)return;
    const current=accounts.find(item=>item.id===accountId);
    if(current&&(account.credential_revision||0)<(current.credential_revision||0))return;
    accounts=accounts.map(item=>item.id===accountId?account:item);
    renderAccounts();
  }
  async function api(url,data){
    const response=await fetch(url,data===undefined?undefined:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(!(response.headers.get('Content-Type')||'').includes('application/json'))throw new Error(`本机服务返回异常页面（HTTP ${response.status}），请重启或更新应用后重试。`);
    const result=await response.json();
    if(!response.ok||result.error){const error=new Error(result.error||'信息查询未完成，请稍后重试。');error.code=result.code;error.account=result.account;error.retryAfter=result.retry_after;throw error;}
    return result;
  }
  function renderAccounts(){
    const selected=accounts.find(account=>account.id===loginId);
    $('bills-account-count').textContent=accounts.length;
    $('bills-account-hint').textContent=accounts.length?'与其他功能共用登录':'请从左侧“账号管理”添加账号';
    $('bills-current-account').textContent=selected?`${selected.name||selected.phone_masked} · ${selected.phone_masked}`:'尚未选择账号';
    $('usage-current-account').textContent=$('bills-current-account').textContent;
    $('services-current-account').textContent=$('bills-current-account').textContent;
    $('bills-accounts').replaceChildren(...accounts.map(account=>{
      const button=node('button','bills-account'+(account.id===loginId?' selected':''));button.type='button';
      button.setAttribute('aria-pressed',String(account.id===loginId));
      const label=node('span','bills-account-label');
      label.append(node('strong','',account.name||account.phone_masked),node('span','bills-account-state '+(account.status==='active'?'active':'attention'),accountStates[account.status]||'暂不可用'));
      button.append(label,node('small','',account.phone_masked));
      button.addEventListener('click',()=>{loginId=account.id;renderAccounts();return loadInformation();});
      return button;
    }));
  }
  function renderMonths(){
    const options=months.map(value=>{const option=node('option','',monthText(value));option.value=value;return option;});
    if(!options.length){const option=node('option','','暂无可查月份');option.value='';options.push(option);}
    $('bills-month').replaceChildren(...options);$('bills-month').value=month;
  }
  function expandItems(items,path=''){
    items.forEach((item,index)=>{const key=path+index;if(hasBillDetails(item)){expanded.add(key);expandItems(item.children,key+'.');}});
  }
  function renderUsage(){
    const selected=accounts.find(account=>account.id===loginId);
    const needsLogin=selected&&(loginRequiredCodes.has(usageErrorCode)||(!usage&&selected.status==='login_required'));
    $('usage-content').setAttribute('aria-busy',String(usageLoading));
    $('usage-feedback-message').textContent=usageErrorMessage||(needsLogin?'该账号需要恢复登录，可在账号管理中继续。':'');
    $('usage-feedback').hidden=!usageErrorMessage&&!needsLogin;
    $('usage-relogin').hidden=!needsLogin;
    function quantity(item,field,raw=false){
      const display=raw?null:item.display?.[field];
      const label=item.unlimited&&(field==='total'||field==='remaining')?'不限量':amountText(display?.value??item[field]);
      const value=node('span','usage-quantity',label);
      if(label!=='不限量'&&label!=='—'){
        const unit=display?.unit??item.unit;
        if(unit)value.append(node('small','',unit));
        if(display)value.setAttribute('title',`原始值：${item[field]} ${item.unit||''}`.trim());
      }
      return value;
    }
    function counters(item,className,fields=[['total','总量'],['used','已用'],['remaining','剩余']],raw=false){
      const values=node('dl',className);
      for(const [field,label] of fields){
        const entry=node('div'),value=node('dd');value.append(quantity(item,field,raw));entry.append(node('dt','',label),value);values.append(entry);
      }
      return values;
    }
    function metadata(item,inherited={},resourceName=''){
      const result=node('div','usage-metadata');
      if(item.scope&&item.scope!==inherited.scope)result.append(node('p','',`适用范围：${item.scope}`));
      if((item.start_at||item.end_at)&&(item.start_at!==inherited.start_at||item.end_at!==inherited.end_at))result.append(node('p','',`有效期：${item.start_at||'—'} 至 ${item.end_at||'—'}`));
      if(resourceName||item.description&&item.description!==inherited.description){
        const description=node('details','usage-description');
        description.append(node('summary','','适用说明'));
        if(resourceName)description.append(node('p','',`资源名称：${resourceName}`));
        if(item.description&&item.description!==inherited.description)description.append(node('p','',item.description));result.append(description);
      }
      return result;
    }
    const overview=(usage?.overview||[]).map(item=>{
      const type=['flow','voice','sms'].includes(item.key)?item.key:'other';
      const card=node('article','usage-overview-card usage-'+type);
      const heading=node('div','usage-overview-heading');heading.append(node('span','usage-type-mark',({flow:'流',voice:'话',sms:'信'})[type]||'余'),node('h3','',item.name));
      const remaining=node('div','usage-remaining');remaining.append(node('span','usage-remaining-label','剩余'),quantity(item,'remaining'));
      const values=node('div','usage-overview-values');
      values.append(remaining,counters(item,'usage-overview-counters',[['total','总量'],['used','已用']]));
      card.append(heading,values);
      if(item.used_percent!==null&&item.used_percent!==undefined){
        const meterRow=node('div','usage-meter-row'),meter=node('div','usage-meter');
        meter.setAttribute('role','meter');meter.setAttribute('aria-label',`${item.name}已用比例`);
        meter.setAttribute('aria-valuemin','0');meter.setAttribute('aria-valuemax','100');meter.setAttribute('aria-valuenow',item.used_percent);
        const percent=String(Number(item.used_percent)),label=`已用 ${percent}%`;
        meter.setAttribute('aria-valuetext',label);meter.setAttribute('title',label);
        const fill=node('span','usage-meter-fill');fill.style.width=`${item.used_percent}%`;meter.append(fill);
        meterRow.append(meter,node('span','usage-meter-percent',`${percent}%`));card.append(meterRow);
      }
      const meta=metadata(item,{scope:item.name});
      if(meta.children.length){meta.className+=' usage-overview-meta';card.append(meta);}
      return card;
    });
    if(!overview.length){
      const message=usageLoading?'正在查询该账号套餐余量…':!selected?(refreshing?'正在读取共享账号…':'共享账号池为空，请从左侧“账号管理”登录。'):usageErrorMessage?'本次余量查询未完成，点击“刷新信息”重试。':usage?usage.message||'暂无套餐余量概览，可查看下方套餐明细。':'选择账号后查询套餐余量。';
      overview.push(node('p','bills-empty',message));
    }
    $('usage-overview').replaceChildren(...overview);
    function detailItem(item,inherited={}){
      const detail=node('article','usage-item');
      const hasAmounts=entry=>entry.unlimited||['total','used','remaining'].some(field=>entry[field]!==null&&entry[field]!==undefined&&entry[field]!=='');
      const children=item.children||[];
      const resource=!hasAmounts(item)&&children.length===1&&!children[0].children?.length&&hasAmounts(children[0])?children[0]:item;
      const title=node('div','usage-item-title');title.append(node('h4','',item.name));detail.append(title);
      if(hasAmounts(resource)){
        const values=counters(resource,'usage-item-counters',[['used','已用'],['remaining','剩余'],['total','总量']]);
        if(resource.remaining_percent!==null&&resource.remaining_percent!==undefined){
          const track=node('div','usage-progress');track.setAttribute('role','progressbar');track.setAttribute('aria-label',`${item.name}剩余比例`);
          track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');track.setAttribute('aria-valuenow',resource.remaining_percent);
          const fill=node('span');fill.style.width=`${resource.remaining_percent}%`;track.append(fill);values.children[1].append(track);
        }
        detail.append(values);
        if(resource.display){
          const original=node('details','usage-original');original.append(node('summary','','查看原始用量'),counters(resource,'usage-original-counters',undefined,true));title.append(original);
        }
      }
      const meta=node('div','usage-item-meta');
      meta.append(metadata(item,inherited,resource!==item&&resource.name!==item.name?resource.name:''));detail.append(meta);
      if(resource!==item){
        meta.append(metadata(resource,{...inherited,...item}));
      }else if(children.length){
        const nested=node('div','usage-item-children');nested.append(...children.map(child=>detailItem(child,{...inherited,...item})));detail.append(nested);
      }
      return detail;
    }
    const groups=(usage?.groups||[]).map((group,index)=>{
      const open=usageExpanded.has(index),card=node('section','usage-group'+(open?' expanded':'')),heading=node('button','usage-group-heading');heading.type='button';
      heading.setAttribute('aria-expanded',String(open));heading.setAttribute('aria-label',`${open?'收起':'展开'}${group.name}`);
      heading.append(node('span','',group.name),node('span','usage-group-count',`${group.items.length} 项`),node('span','usage-group-chevron',open?'−':'+'));
      heading.addEventListener('click',()=>{if(usageExpanded.has(index))usageExpanded.delete(index);else usageExpanded.add(index);renderUsage();});card.append(heading);
      if(open){
        const shared={},first=group.items[0];
        if(group.items.length>1){
          if(first.scope&&group.items.every(item=>item.scope===first.scope))shared.scope=first.scope;
          if((first.start_at||first.end_at)&&group.items.every(item=>item.start_at===first.start_at&&item.end_at===first.end_at)){
            shared.start_at=first.start_at;shared.end_at=first.end_at;
          }
        }
        const common=metadata(shared,{scope:group.name});
        if(common.children.length){common.className+=' usage-group-context';card.append(common);}
        const items=node('div','usage-group-items');
        items.append(...group.items.map(item=>detailItem(item,shared)));card.append(items);
      }
      return card;
    });
    if(!groups.length)groups.push(node('p','usage-empty',usageLoading?'正在读取套餐明细…':usageErrorMessage?'套餐明细暂未获取。':usage?'官方暂未提供套餐明细。':'查询后查看套餐明细。'));
    $('usage-groups').replaceChildren(...groups);
    const notes=usage?.notes||[];$('usage-notes-list').replaceChildren(...notes.map(note=>node('li','',note)));$('usage-notes').hidden=!notes.length;
    $('usage-sync').textContent=usageLoading?'正在查询官方套餐余量…':usage?.queried_at?'北京时间 · 最近查询 '+formatter.format(new Date(usage.queried_at)):usageErrorMessage?'查询未完成':refreshing?'正在读取共享账号…':'尚未查询套餐余量';
  }
  function renderCancellation(){
    const ready=Boolean(cancellation?.cancel_id)&&!cancellation.finished;
    $('services-cancel-submit').disabled=cancelPending||!ready||!cancellation.smsReady||!/^\d{6}$/.test($('services-cancel-code').value);
    $('services-cancel-submit').textContent=cancellation?.stage==='submitting'?'提交中…':'确认退订';
    $('services-cancel-submit').hidden=Boolean(cancellation?.finished);
    $('services-cancel-sms').disabled=cancelPending||!ready||Boolean(cancellation.retryAfter);
    $('services-cancel-sms').textContent=cancellation?.retryAfter?`${cancellation.retryAfter} 秒后重发`:'获取验证码';
    $('services-cancel-code').disabled=cancelPending||!ready;
    $('services-cancel-close').disabled=cancelPending;
    $('services-cancel-dismiss').disabled=cancelPending;
    $('services-cancel-dismiss').textContent=cancellation?.finished?'关闭':'取消';
  }
  function cancellationMessage(message,kind=''){
    $('services-cancel-message').textContent=message;
    $('services-cancel-message').hidden=!message;
    $('services-cancel-message').className='services-cancel-message'+(kind?' '+kind:'');
  }
  function cancelCountdown(seconds){
    if(cancelTimer!==null)clearInterval(cancelTimer);
    cancellation.retryAfter=seconds;renderCancellation();
    cancelTimer=setInterval(()=>{
      cancellation.retryAfter=Math.max(0,cancellation.retryAfter-1);
      if(!cancellation.retryAfter){clearInterval(cancelTimer);cancelTimer=null;}
      renderCancellation();
    },1000);
  }
  function closeCancellation(){
    if(cancelPending)return;
    if(cancelTimer!==null){clearInterval(cancelTimer);cancelTimer=null;}
    if(cancelCloseTimer!==null){clearTimeout(cancelCloseTimer);cancelCloseTimer=null;}
    cancelVersion++;cancellation=null;$('services-cancel-dialog').close();
  }
  async function openCancellation(item){
    const version=++cancelVersion,selected=accounts.find(account=>account.id===loginId);
    cancellation={login_id:loginId,name:item.name,stage:'preparing',smsReady:false,retryAfter:0};cancelPending=true;
    $('services-cancel-account').textContent=`${selected.name||selected.phone_masked} · ${selected.phone_masked}`;
    $('services-cancel-name').textContent=item.name;$('services-cancel-info').hidden=true;
    $('services-cancel-code').value='';
    cancellationMessage('正在核对官方退订条件和关联影响…');renderCancellation();$('services-cancel-dialog').showModal();
    try{
      const result=await api('/api/services/cancel/prepare',{login_id:cancellation.login_id,action_id:item.cancel.action_id});
      if(version!==cancelVersion)return;
      applyAccount(result.account,cancellation.login_id);
      cancellation={...cancellation,...result,stage:'prepared',effective_type:result.effective_options[0].value};
      $('services-cancel-name').textContent=result.name;
      $('services-cancel-warnings').replaceChildren(...(result.warnings||[]).map(message=>node('p','',message)));
      const impacts=[];
      for(const [key,label] of [['bound_services','以下业务将一同退订'],['related_services','退订后将无法享受以下优惠']]){
        if(result[key]?.length){const block=node('section'),list=node('ul');block.append(node('h4','',label));list.append(...result[key].map(name=>node('li','',name)));block.append(list);impacts.push(block);}
      }
      $('services-cancel-related').replaceChildren(...impacts);
      $('services-cancel-description').textContent=result.description||'';
      $('services-cancel-effective').textContent='失效时间：'+result.effective_options[0].label;
      $('services-cancel-info').hidden=false;
      cancellationMessage('');
    }catch(error){
      if(version!==cancelVersion)return;
      applyAccount(error.account,cancellation.login_id);cancellationMessage(error.message,'error');
    }finally{if(version===cancelVersion){cancelPending=false;renderCancellation();}}
  }
  async function sendCancelSms(){
    cancelPending=true;cancellation.smsReady=false;cancellationMessage('正在发送退订验证码…');renderCancellation();
    try{
      const result=await api('/api/services/cancel/sms',{login_id:cancellation.login_id,cancel_id:cancellation.cancel_id,effective_type:cancellation.effective_type});
      cancellation.smsReady=true;$('services-cancel-code').value='';cancelCountdown(result.retry_after||60);
      cancellationMessage(result.message||'退订验证码已发送，请填写后确认。');$('services-cancel-code').focus();
    }catch(error){cancellationMessage(error.message,'error');if(error.retryAfter)cancelCountdown(error.retryAfter);}
    finally{cancelPending=false;renderCancellation();}
  }
  async function confirmCancellation(){
    cancelPending=true;cancellation.stage='submitting';cancellationMessage('正在提交退订，请等待官方结果…');renderCancellation();
    try{
      const result=await api('/api/services/cancel/confirm',{login_id:cancellation.login_id,cancel_id:cancellation.cancel_id,effective_type:cancellation.effective_type,sms_code:$('services-cancel-code').value,confirmed:true});
      cancellation.finished=true;cancellationMessage(result.message,result.status==='success'?'success':'notice');
      applyAccount(result.account,cancellation.login_id);
      if(result.status==='success'){
        cancelCloseTimer=setTimeout(closeCancellation,3000);
        if(loginId===cancellation.login_id)void loadServices();
      }
    }catch(error){
      cancellationMessage(error.message,'error');
      if(['312009','SERVICES_SMS_EXPIRED','SERVICES_SMS_MISMATCH'].includes(error.code))cancellation.smsReady=false;
      if(!['312008','312009','SERVICES_SMS_EXPIRED','SERVICES_SMS_MISMATCH','INVALID_REQUEST'].includes(error.code))cancellation.finished=true;
    }finally{cancellation.stage='prepared';cancelPending=false;renderCancellation();}
  }
  function renderServices(selectMatchingCategory=false){
    const selected=accounts.find(account=>account.id===loginId);
    const needsLogin=selected&&(loginRequiredCodes.has(servicesErrorCode)||(!services&&selected.status==='login_required'));
    $('services-content').setAttribute('aria-busy',String(servicesLoading));
    $('services-feedback-message').textContent=servicesErrorMessage||(needsLogin?'该账号需要恢复登录，可在账号管理中继续。':'');
    $('services-feedback').hidden=!servicesErrorMessage&&!needsLogin;
    $('services-relogin').hidden=!needsLogin;
    const query=$('services-search').value.trim().toLocaleLowerCase(),cancelOnly=Boolean($('services-cancel-only').checked);
    $('services-search').disabled=!services||servicesLoading;
    $('services-cancel-only').disabled=!services||servicesLoading;
    function containsQuery(item){
      return item.name.toLocaleLowerCase().includes(query)||(item.fields||[]).some(field=>field.name.includes('说明')&&String(field.value??'').toLocaleLowerCase().includes(query))||(item.children||[]).some(containsQuery);
    }
    const matches=item=>(!cancelOnly||item.cancel?.available)&&(!query||containsQuery(item));
    function serviceTable(items,path,label,sourceItems=items){
      const positions=new Map(sourceItems.map((item,index)=>[item,index]));
      const table=node('table','services-table'),head=node('thead'),header=node('tr'),body=node('tbody');
      table.setAttribute('aria-label',label);
      for(const title of ['业务 / 有效期','操作']){const cell=node('th','',title);cell.setAttribute('scope','col');header.append(cell);}
      head.append(header);
      [...items].sort((a,b)=>Number(b.status==='已退订')-Number(a.status==='已退订')).forEach(item=>{
        const key=path+positions.get(item),fields=[{name:'资费',value:item.fee},{name:'状态',value:item.status},...(item.fields||[])];
        if(item.cancel?.reason)fields.push({name:'退订说明',value:item.cancel.reason});
        const cancelled=item.status==='已退订';
        const row=node('tr','services-row'+(cancelled?' services-cancelled':'')),name=node('td','services-name'),actions=node('td','services-actions');
        const heading=node('button','services-name-label');
        const chevron=node('span','services-chevron');chevron.setAttribute('aria-hidden','true');
        heading.type='button';heading.setAttribute('aria-label',`${item.name}详情`);heading.append(chevron);
        heading.append(node('span','',item.name));name.append(heading);
        if(cancelled)heading.append(node('small','services-cancelled-label','已退订'));
        const period=fields.find(field=>['有效期','有效期限'].includes(field.name)&&amountText(field.value)!=='—');
        const dates=period?[period]:fields.filter(field=>['生效时间','失效时间'].includes(field.name)&&amountText(field.value)!=='—');
        if(dates.length){
          const validity=node('small','services-validity',dates.map(field=>`${period?'':field.name==='生效时间'?'生效 ':'失效 '}${String(field.value).replace(/^(\d{4})(\d{2})(\d{2})\d{6}$/,'$1-$2-$3').replace(/^(\d{4}-\d{2}-\d{2})(?:\s+|T)\d{2}:\d{2}(?::\d{2})?/,'$1')}`).join(' · '));
          validity.setAttribute('title',dates.map(field=>`${field.name}：${field.value}`).join('\n'));name.append(validity);
        }
        if(item.cancel?.available){
          const cancel=node('button','services-cancel-action','退订');cancel.type='button';cancel.setAttribute('aria-label',`退订${item.name}`);
          cancel.addEventListener('click',()=>openCancellation(item));actions.append(cancel);
        }else{const unavailable=node('span','services-unavailable','—');unavailable.setAttribute('title',item.cancel?.reason||'该条目未提供在线退订入口');actions.append(unavailable);}
        row.append(name,actions);body.append(row);
        {
          const detailRow=node('tr','services-detail-row'+(cancelled?' services-cancelled':'')),cell=node('td','services-details-cell');cell.setAttribute('colspan','2');
          const detailId='services-detail-'+key.replaceAll('.','-');detailRow.setAttribute('id',detailId);heading.setAttribute('aria-controls',detailId);
          detailRow.append(cell);body.append(detailRow);
          const showDetails=()=>{
            const open=serviceDetails.has(key);detailRow.hidden=!open;heading.setAttribute('aria-expanded',String(open));chevron.textContent=open?'−':'+';
            if(!open||cell.children.length)return;
            const details=node('dl','services-fields');
            for(const field of fields){const entry=node('div');entry.append(node('dt','',field.name),node('dd','',amountText(field.value)));details.append(entry);}
            cell.append(details);
            if(item.children?.length){const children=node('div','services-related');children.append(node('h4','','关联业务'),serviceTable(item.children,key+'.',`${item.name}关联业务`));cell.append(children);}
          };
          heading.addEventListener('click',()=>{if(serviceDetails.has(key))serviceDetails.delete(key);else serviceDetails.add(key);showDetails();});showDetails();
        }
      });
      table.append(head,body);return table;
    }
    const categoryNames=['我的套餐','增值业务','基础功能','活动'];
    const categories=categoryNames.map(name=>({name,sections:[]}));
    let firstCategory='';
    for(const group of services?.groups||[]){
      const label=group.name.replace(/^手机(?=\s*·|线路)/,'').replace(/^\s*·\s*/,'').trim();
      const name=categoryNames.find(name=>label===name||label.endsWith(' · '+name))||label;
      let category=categories.find(category=>category.name===name);
      if(!category){category={name,sections:[]};categories.push(category);}
      category.sections.push({...group,name:label});
      if(!firstCategory)firstCategory=name;
    }
    const counts=categories.map(category=>category.sections.reduce((count,section)=>count+section.items.filter(matches).length,0));
    let activeName=categories.some(category=>category.name===servicesCategory)?servicesCategory:firstCategory||categories[0].name;
    if(selectMatchingCategory&&!counts[categories.findIndex(category=>category.name===activeName)]&&counts.some(Boolean))activeName=categories[counts.findIndex(Boolean)].name;
    if(services)servicesCategory=activeName;
    const activeIndex=categories.findIndex(category=>category.name===activeName);
    const tabs=categories.map((category,index)=>{
      const selected=category.name===activeName,button=node('button','services-tab'+(selected?' selected':''));button.type='button';
      button.disabled=!services||servicesLoading;button.tabIndex=selected?0:-1;
      button.setAttribute('id',`services-tab-${index}`);button.setAttribute('role','tab');button.setAttribute('aria-selected',String(selected));button.setAttribute('aria-controls','services-groups');
      const count=counts[index];
      const badge=node('small','services-tab-count',services?String(count):'—');
      badge.setAttribute('aria-label',services?`${count} 项业务`:'数量待查询');
      button.append(node('span','',category.name),badge);
      button.addEventListener('click',()=>{servicesCategory=category.name;renderServices();});
      button.addEventListener('keydown',event=>{
        if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
        event.preventDefault();
        const next=event.key==='Home'?0:event.key==='End'?categories.length-1:(index+(event.key==='ArrowLeft'?-1:1)+categories.length)%categories.length;
        servicesCategory=categories[next].name;renderServices();$('services-tabs').children[next].focus();
      });
      return button;
    });
    $('services-tabs').replaceChildren(...tabs);
    $('services-groups').setAttribute('aria-labelledby',`services-tab-${activeIndex}`);
    $('services-filter-count').textContent=services?(query||cancelOnly?`匹配 ${counts.reduce((sum,count)=>sum+count,0)} 项 · 当前 ${counts[activeIndex]} 项`:`当前 ${counts[activeIndex]} 项`):'';
    const groups=categories[activeIndex].sections.flatMap((section,index)=>{
      const items=section.items.filter(matches);
      if(!items.length)return [];
      const content=node('section','services-section');
      if(section.name!==activeName)content.append(node('h3','services-section-heading',section.name));
      content.append(serviceTable(items,`${activeIndex}.${index}.`,section.name,section.items));return [content];
    });
    if(!groups.length){
      const message=servicesLoading?'正在查询该账号已订业务…':!selected?(refreshing?'正在读取共享账号…':'共享账号池为空，请从左侧“账号管理”登录。'):servicesErrorMessage?'本次已订业务查询未完成，点击“刷新信息”重试。':services?(query||cancelOnly?'当前分类没有匹配的业务，请调整关键词或筛选条件。':services.message||`${activeName}暂无已订业务记录。`):'选择账号后查询已订业务。';
      groups.push(node('p','services-empty',message));
    }
    $('services-groups').replaceChildren(...groups);
    const notes=services?.notes||[];$('services-notes-list').replaceChildren(...notes.map(note=>node('li','',note)));$('services-notes').hidden=!notes.length;
    $('services-sync').textContent=servicesLoading?'正在查询官方已订业务…':services?.queried_at?'北京时间 · 最近查询 '+formatter.format(new Date(services.queried_at)):servicesErrorMessage?'查询未完成':refreshing?'正在读取共享账号…':'尚未查询已订业务';
  }
  function render(){
    const selected=accounts.find(account=>account.id===loginId),index=months.indexOf(month);
    const needsLogin=selected&&(loginRequiredCodes.has(errorCode)||(!bill&&selected.status==='login_required'));
    $('bills-refresh').disabled=loading||usageLoading||servicesLoading||refreshing;
    $('bills-refresh').textContent=loading||usageLoading||servicesLoading?'查询中…':'刷新信息';
    $('bills-content').setAttribute('aria-busy',String(loading));
    $('information-feedback-message').textContent=informationErrorMessage;
    $('information-feedback').hidden=!informationErrorMessage;
    $('bills-month').disabled=!months.length;
    $('bills-previous').disabled=index<0||index===months.length-1;
    $('bills-next').disabled=index<=0;
    $('information-current-account').textContent=selected?`${selected.name||selected.phone_masked} · ${selected.phone_masked}`:'尚未选择账号';
    $('bills-title').textContent='月度账单';
    const start=Math.max(0,Math.min(index-2,months.length-7));
    $('bills-months').replaceChildren(...months.slice(start,start+7).map(value=>{
      const button=node('button','bills-month-tab'+(value===month?' selected':''));button.type='button';
      button.setAttribute('aria-pressed',String(value===month));button.setAttribute('aria-label',monthText(value));
      button.append(node('strong','',`${Number(value.slice(4))}月`),node('span','',value.slice(0,4)));
      button.addEventListener('click',()=>changeMonth(value));return button;
    }));
    $('bills-period').textContent=bill?.period||(month?monthText(month):'选择账号和月份后查看');
    for(const field of moneyFields)$('bills-'+field).textContent=amountText(bill?.[field]);
    const summary=(bill?.summary||[]).filter(item=>amountText(item.amount)!=='—');
    $('bills-extra-summary').replaceChildren(...summary.map(item=>{
      const entry=node('div'),amount=node('dd',Number(item.amount)<0?'bills-deduction':'',amountText(item.amount));amount.append(node('span','',' 元'));
      entry.append(node('dt','',item.name),amount);return entry;
    }));
    $('bills-extra-summary').hidden=!summary.length;
    const notes=bill?.notes||[];
    $('bills-notes-list').replaceChildren(...notes.map(note=>node('li','',note)));
    $('bills-notes').hidden=!notes.length;
    $('bills-feedback-message').textContent=errorMessage||(needsLogin?'该账号需要恢复登录，可在账号管理中继续。':'');
    $('bills-feedback').hidden=!errorMessage&&!needsLogin;
    $('bills-relogin').hidden=!needsLogin;
    function appendRows(items,rows,depth=0,path=''){
      items.forEach((item,itemIndex)=>{
        const key=path+itemIndex,hasChildren=hasBillDetails(item),open=hasChildren&&expanded.has(key);
        const row=node('tr',depth===0?'bills-row-total':'bills-row-detail'),name=node('td'),label=node('div','bills-item-name');
        name.style.paddingLeft=`${19+Math.min(depth,8)*23}px`;
        if(hasChildren){
          const toggle=node('button','bills-item-toggle',open?'−':'+');toggle.type='button';
          toggle.setAttribute('aria-expanded',String(open));toggle.setAttribute('aria-label',`${open?'收起':'展开'}${item.name}明细`);
          toggle.addEventListener('click',()=>{if(expanded.has(key))expanded.delete(key);else expanded.add(key);render();});label.append(toggle);
        }else label.append(node('span','bills-item-leaf'));
        label.append(node('span','bills-item-text',item.name));name.append(label);
        row.append(name,node('td','bills-number'+(Number(item.original)<0?' bills-deduction':''),amountText(item.original)),node('td','bills-number'+(Math.abs(Number(item.discount))>0?' bills-deduction':''),amountText(item.discount)),node('td','bills-number'+(Number(item.amount)<0?' bills-deduction':''),amountText(item.amount)));rows.push(row);
        if(hasChildren&&open)appendRows(item.children,rows,depth+1,key+'.');
      });
    }
    const cards=[];
    function appendCards(items,path=''){
      items.forEach((item,itemIndex)=>{
        const key=path+itemIndex,hasChildren=hasBillDetails(item),open=hasChildren&&expanded.has(key);
        const card=node('article','bills-detail-card'+(open?' expanded':''));
        const heading=node(hasChildren?'button':'div','bills-card-heading');
        const label=node('span','bills-card-label',item.name),amount=node('span','bills-card-value');
        amount.append(node('strong',Number(item.amount)<0?'bills-deduction':'',amountText(item.amount)),node('small','','元'));
        if(hasChildren){
          heading.type='button';heading.setAttribute('aria-expanded',String(open));heading.setAttribute('aria-label',`${open?'收起':'展开'}${item.name}明细`);
          label.append(node('span','bills-card-chevron',open?'−':'+'));
          heading.addEventListener('click',()=>{if(expanded.has(key))expanded.delete(key);else expanded.add(key);render();});
        }
        heading.append(label,amount);
        const summary=node('div','bills-card-summary');
        for(const [field,title] of [['original','优惠前'],['discount','减免']]){
          const value=amountText(item[field]);
          if(value!=='—')summary.append(node('span',field==='discount'&&Math.abs(Number(item.discount))>0?'bills-deduction':'',`${title} ${value} 元`));
        }
        card.append(heading);
        if(summary.children.length)card.append(summary);else card.className+=' no-summary';
        if(hasChildren&&open){
          const wrap=node('div','bills-table-wrap'),table=node('table','bills-table'),thead=node('thead'),header=node('tr'),tbody=node('tbody'),rows=[];
          table.setAttribute('aria-label',`${item.name}费用明细`);
          ['费用项目','优惠前 / 元','优惠减免 / 元','金额 / 元'].forEach(text=>{const cell=node('th','',text);cell.setAttribute('scope','col');header.append(cell);});
          thead.append(header);appendRows(item.children,rows,0,key+'.');tbody.append(...rows);table.append(thead,tbody);wrap.append(table);card.append(wrap);
        }
        cards.push(card);
      });
    }
    if(bill)appendCards(bill.items);
    (bill?.sections||[]).forEach((section,sectionIndex)=>{
      if(!section.items.length)return;
      if(section.items.length!==1||section.items[0].compact_details!==true)cards.push(node('h3','bills-section-heading',section.name));
      appendCards(section.items,`section-${sectionIndex}.`);
    });
    if(!cards.length){
      const message=loading?'正在查询该账号账单…':!selected?(refreshing?'正在读取共享账号…':'共享账号池为空，请从左侧“账号管理”登录。'):errorMessage?'本次查询未完成，点击“刷新信息”重试。':!month?'当前没有可查询的账单月份。':bill?bill.message||'该月暂无账单明细。':'选择账号和月份后查询账单。';
      cards.push(node('p','bills-empty',message));
    }
    $('bills-items').replaceChildren(...cards);
    $('bills-expand').disabled=!(bill?.items.some(hasBillDetails)||(bill?.sections||[]).some(section=>section.items.some(hasBillDetails)));
    $('bills-collapse').disabled=$('bills-expand').disabled;
    $('bills-sync').textContent=loading?'正在查询官方账单…':bill?.queried_at?'北京时间 · 最近查询 '+formatter.format(new Date(bill.queried_at)):errorMessage?'查询未完成':refreshing?'正在读取共享账号…':'尚未查询账单';
    renderUsage();
    renderServices();
  }
  async function loadBill(){
    const version=++requestVersion,accountId=loginId,queryMonth=month;
    bill=null;expanded.clear();errorMessage='';errorCode='';
    if(!accountId||!queryMonth){loading=false;render();return;}
    loading=true;render();
    try{
      const result=await api('/api/bills/query?'+new URLSearchParams({login_id:accountId,month:queryMonth}));
      if(version!==requestVersion)return;
      applyAccount(result.account,accountId);bill=result;
    }catch(error){
      if(version!==requestVersion)return;
      applyAccount(error.account,accountId);errorMessage=error.message;errorCode=error.code||'';
    }finally{if(version===requestVersion){loading=false;render();}}
  }
  async function loadUsage(){
    const version=++usageRequestVersion,accountId=loginId;
    usage=null;usageExpanded.clear();usageErrorMessage='';usageErrorCode='';
    if(!accountId){usageLoading=false;render();return;}
    usageLoading=true;render();
    try{
      const result=await api('/api/usage/query?'+new URLSearchParams({login_id:accountId}));
      if(version!==usageRequestVersion)return;
      applyAccount(result.account,accountId);usage=result;
    }catch(error){
      if(version!==usageRequestVersion)return;
      applyAccount(error.account,accountId);usageErrorMessage=error.message;usageErrorCode=error.code||'';
    }finally{if(version===usageRequestVersion){usageLoading=false;render();}}
  }
  async function loadServices(){
    const version=++servicesRequestVersion,accountId=loginId;
    services=null;servicesErrorMessage='';servicesErrorCode='';serviceDetails.clear();
    if(!accountId){servicesLoading=false;render();return;}
    servicesLoading=true;render();
    try{
      const result=await api('/api/services/query?'+new URLSearchParams({login_id:accountId}));
      if(version!==servicesRequestVersion)return;
      applyAccount(result.account,accountId);services=result;
    }catch(error){
      if(version!==servicesRequestVersion)return;
      applyAccount(error.account,accountId);servicesErrorMessage=error.message;servicesErrorCode=error.code||'';
    }finally{if(version===servicesRequestVersion){servicesLoading=false;render();}}
  }
  function loadInformation(){return Promise.all([loadBill(),loadUsage(),loadServices()]);}
  async function refresh(){
    if(refreshing){refreshPending=true;return;}
    refreshing=true;informationErrorMessage='';render();
    try{
      const result=await api('/api/bills/accounts');accounts=result.accounts;months=[...new Set(result.months)].sort().reverse();
      if(!accounts.some(account=>account.id===loginId))loginId=accounts.find(account=>account.status==='active')?.id||accounts[0]?.id||'';
      if(!months.includes(month))month=months.includes(result.default_month)?result.default_month:months[0]||'';
      renderAccounts();renderMonths();await loadInformation();
    }catch(error){informationErrorMessage=error.message;$('bills-account-hint').textContent='账号或月份读取失败，请刷新重试。';}
    finally{refreshing=false;render();if(refreshPending){refreshPending=false;await refresh();}}
  }
  function changeMonth(value){month=value;$('bills-month').value=month;return loadBill();}
  $('bills-refresh').addEventListener('click',refresh);
  $('bills-month').addEventListener('change',()=>changeMonth($('bills-month').value));
  $('bills-previous').addEventListener('click',()=>changeMonth(months[months.indexOf(month)+1]));
  $('bills-next').addEventListener('click',()=>changeMonth(months[months.indexOf(month)-1]));
  $('bills-expand').addEventListener('click',()=>{expandItems(bill.items);(bill.sections||[]).forEach((section,index)=>expandItems(section.items,`section-${index}.`));render();});
  $('bills-collapse').addEventListener('click',()=>{expanded.clear();render();});
  function openAccountManager(){window.dispatchEvent(new CustomEvent('open-account-manager',{detail:{loginId,message:'请恢复当前账号登录，完成后将继续查询余量、账单和已订业务。'}}));}
  $('bills-relogin').addEventListener('click',openAccountManager);
  $('usage-relogin').addEventListener('click',openAccountManager);
  $('services-relogin').addEventListener('click',openAccountManager);
  $('services-search').addEventListener('input',()=>renderServices(true));
  $('services-cancel-only').addEventListener('change',()=>renderServices(true));
  $('services-cancel-close').addEventListener('click',closeCancellation);
  $('services-cancel-dismiss').addEventListener('click',closeCancellation);
  $('services-cancel-dialog').addEventListener('cancel',event=>{event.preventDefault();closeCancellation();});
  $('services-cancel-sms').addEventListener('click',sendCancelSms);
  $('services-cancel-submit').addEventListener('click',confirmCancellation);
  $('services-cancel-code').addEventListener('input',renderCancellation);
  window.addEventListener('accounts-changed',refresh);
  refresh();
})();
