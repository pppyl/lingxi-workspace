'use strict';
const $=id=>document.getElementById(id);
const kindNames={signin:'每日签到',flash:'月末秒杀'};
const accountStateNames={login_required:'需重新登录',restoring:'正在恢复',retry:'等待重连'};
const stateNames={waiting:'待执行',preparing:'准备中',running:'执行中',receiving:'接收结果',stopping:'正在停止',success:'已完成',completed:'已完成',signed:'今日已签到',already_signed:'今日已签到',unsigned:'今日未签到',not_signed:'今日未签到',pending:'今日未签到',unknown:'未查询',not_configured:'未配置',skipped:'本次未参与',available:'当前可参与',already_claimed:'本月已领取',unavailable:'暂不可参与',failed:'执行失败',error:'执行失败',cancelled:'已停止',stopped:'已停止',login_required:'需要重新登录',retry:'等待重试',interrupted:'已中断'};
const activeStates=new Set(['preparing','running','receiving','stopping']);
const signedStates=new Set(['signed','already_signed','success','completed']);
let model={accounts:[],tasks:[],products:[]},loaded=false,loading=false,working=false,filter='all',view='accounts',manual=false,historyId='',historyAccountId='';
const selectedTasks=new Set();
const dateFormat=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
function node(tag,className='',text){const element=document.createElement(tag);element.className=className;if(text!==undefined)element.textContent=text;return element;}
async function api(path,payload){
  const response=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'操作未完成，请重试。');return data;
}
function feedback(message,success=false){$('checkin-feedback').textContent=message;$('checkin-feedback').hidden=!message;$('checkin-feedback').classList.toggle('success',success);}
function badge(state){
  const tone=activeStates.has(state)?'active':['success','completed','signed','already_signed','already_claimed'].includes(state)?'success':['failed','error','login_required','interrupted'].includes(state)?'error':['waiting','retry','available'].includes(state)?'waiting':'ended';
  return node('span','checkin-badge '+tone,stateNames[state]||state);
}
function stoppable(task){return task.enabled||activeStates.has(task.state);}
function visibleTasks(){const query=$('checkin-search').value.trim().toLocaleLowerCase();return model.tasks.filter(task=>(filter==='all'||task.kind===filter)&&`${task.account_name} ${task.phone_masked}`.toLocaleLowerCase().includes(query));}
function accountGroup(account){
  if(signedStates.has(account.today_state))return 'signed';
  if(['unsigned','not_signed','pending'].includes(account.today_state))return 'unsigned';
  if(['login_required','failed','error'].includes(account.today_state)||account.status==='login_required')return 'attention';
  return 'unknown';
}
function renderAccounts(){
  const query=$('checkin-search').value.trim().toLocaleLowerCase(),state=$('checkin-account-state-filter').value;
  const accounts=model.accounts.filter(account=>(state==='all'||accountGroup(account)===state)&&`${account.name} ${account.phone_masked}`.toLocaleLowerCase().includes(query));
  $('checkin-account-total').textContent=model.accounts.length;
  $('checkin-account-summary').textContent=`显示 ${accounts.length} / ${model.accounts.length} 个账号 · 已确认今日签到 ${model.accounts.filter(account=>signedStates.has(account.today_state)).length} 个`;
  const rows=accounts.map(account=>{
    const row=node('tr'),identity=node('td'),today=node('td'),month=node('td'),recent=node('td'),automatic=node('td'),flash=node('td'),operations=node('td');
    identity.append(node('strong','',account.name||account.phone_masked),node('small','',account.phone_masked));
    if(account.status!=='active')identity.append(node('small','checkin-account-unavailable',accountStateNames[account.status]||'暂不可用'));
    const todayState=account.today_state||'unknown';
    today.append(signedStates.has(todayState)?node('span','checkin-badge success','今日已签到'):badge(todayState));
    month.append(node('strong','checkin-month-count',account.month_count===null||account.month_count===undefined?'—':account.month_count+' 次'));
    recent.append(node('time','checkin-account-last-time',dateFormat(account.last_signin_at)),node('p','checkin-task-message checkin-account-result',account.signin_message||'暂无签到结果'));
    recent.title=account.signin_message||'';
    automatic.append(node('span','checkin-badge '+(account.signin_enabled?'success':'ended'),account.signin_enabled===true?'已开启':account.signin_enabled===false?'未开启':'未读取'));
    const signTask=model.tasks.find(task=>task.login_id===account.id&&task.kind==='signin'&&task.enabled);
    if(signTask?.next_run_at)automatic.append(node('small','','下次 '+dateFormat(signTask.next_run_at)));
    flash.append(badge(account.flash_state||'unknown'),node('small','',account.flash_enabled===true?'每天 12:00 自动尝试':account.flash_enabled===false?'自动秒杀未开启':'自动设置未读取'));
    if(account.flash_message){const message=node('p','checkin-task-message checkin-account-result',account.flash_message);message.title=account.flash_message;flash.append(message);}
    const actions=node('div','checkin-row-actions');
    const sign=node('button','secondary','签到');sign.type='button';sign.disabled=working||account.status!=='active'||signedStates.has(todayState);sign.addEventListener('click',()=>openEditor(true,account.id));actions.append(sign);
    const configure=node('button','quiet','配置');configure.type='button';configure.disabled=working||account.status!=='active';configure.addEventListener('click',()=>openEditor(false,account.id));actions.append(configure);
    const history=node('button','quiet','记录');history.type='button';history.addEventListener('click',()=>openAccountHistory(account.id));actions.append(history);
    if(account.status==='login_required'){const login=node('button','quiet','重新登录');login.type='button';login.setAttribute('data-account-window','');login.dataset.accountRelogin=account.id;actions.append(login);}
    operations.append(actions);row.append(identity,today,month,recent,automatic,flash,operations);return row;
  });
  if(!rows.length){const row=node('tr'),cell=node('td','checkin-empty',model.accounts.length?'当前筛选下没有账号。':'共享账号池为空，请先从左侧“账号管理”登录账号。');cell.colSpan=7;row.append(cell);rows.push(row);}
  $('checkin-accounts').replaceChildren(...rows);
}
function updateSelection(){
  const visible=visibleTasks().filter(stoppable),count=visible.filter(task=>selectedTasks.has(task.id)).length;
  $('checkin-select-tasks').checked=visible.length>0&&visible.length===count;
  $('checkin-select-tasks').indeterminate=count>0&&count<visible.length;
  $('checkin-select-tasks').disabled=!visible.length||working;
  $('checkin-selected-tasks').textContent=`已选 ${selectedTasks.size} 项`;
  $('checkin-stop-tasks').disabled=!selectedTasks.size||working;$('checkin-clear-tasks').hidden=!selectedTasks.size;
}
function render(){
  $('checkin-accounts-view').hidden=view!=='accounts';$('checkin-account-controls').hidden=view!=='accounts';
  $('checkin-tasks-view').hidden=view!=='tasks';$('checkin-task-controls').hidden=view!=='tasks';$('checkin-selection-bar').hidden=view!=='tasks';
  renderAccounts();
  const shown=visibleTasks(),available=new Set(shown.filter(stoppable).map(task=>task.id));
  for(const id of selectedTasks)if(!available.has(id))selectedTasks.delete(id);
  $('checkin-task-count').textContent=model.tasks.length;
  $('checkin-burst-summary').textContent=`默认单账号 ${model.settings?.burst_workers??8} 并发 · 各账号独立`;
  for(const [kind,id] of [['signin','checkin-sign-count'],['flash','checkin-flash-count']]){
    const count=new Set(model.tasks.filter(task=>task.kind===kind&&task.enabled).map(task=>task.login_id)).size;
    $(id).textContent=count?`已开启 ${count} 个账号`:'尚未配置';
  }
  const rows=shown.map(task=>{
    const row=node('tr'),selection=node('td','checkin-task-selection'),check=node('input');check.type='checkbox';
    check.checked=selectedTasks.has(task.id);check.disabled=!stoppable(task)||working;
    check.setAttribute('aria-label',`选择 ${task.account_name} 的${kindNames[task.kind]||task.kind}任务`);
    check.addEventListener('change',()=>{if(check.checked)selectedTasks.add(task.id);else selectedTasks.delete(task.id);updateSelection();});selection.append(check);
    const account=node('td');account.append(node('strong','',task.account_name),node('small','',task.phone_masked));
    const kind=node('td');kind.append(node('strong','',kindNames[task.kind]||task.kind),node('small','',task.kind==='flash'?'每天 12:00':'每天一次'));
    if(task.kind==='flash')kind.append(node('small','',`${task.burst_count??40} 次 · ${task.burst_interval??0.05} 秒`),node('small','',`单账号 ${task.burst_workers??8} 并发`));
    const time=node('td','checkin-task-time');time.append(node('time','',dateFormat(task.next_run_at).replace(' ','\n')));
    if(task.last_run_at)time.append(node('small','','上次 '+dateFormat(task.last_run_at)));
    const state=node('td');state.append(badge(task.state));
    const result=node('td'),message=node('p','checkin-task-message',task.message||'暂无结果');message.title=task.message||'';result.append(message);
    const operations=node('td'),actions=node('div','checkin-row-actions');
    const history=node('button','secondary','记录');history.type='button';history.addEventListener('click',()=>openHistory(task.id));actions.append(history);
    const edit=node('button','quiet','调整');edit.type='button';edit.disabled=working||activeStates.has(task.state);edit.addEventListener('click',()=>openEditor(false,task.login_id));actions.append(edit);
    if(stoppable(task)){const stop=node('button','quiet','停止');stop.type='button';stop.disabled=working;stop.addEventListener('click',()=>stopTasks([task.id]));actions.append(stop);}
    operations.append(actions);row.append(selection,account,kind,time,state,result,operations);return row;
  });
  if(!rows.length){const row=node('tr'),cell=node('td','checkin-empty',model.tasks.length?'当前筛选下没有任务。':'还没有自动任务，点击“配置自动任务”设置执行账号。');cell.colSpan=7;row.append(cell);rows.push(row);}
  $('checkin-tasks').replaceChildren(...rows);updateSelection();
  if($('checkin-history').open)renderHistory();
}
async function refresh(){
  if(loading)return loaded;loading=true;
  try{
    model=await api('/api/checkin/status');loaded=true;render();
    $('checkin-warning').textContent=model.warning||'';$('checkin-warning').hidden=!model.warning;
    $('checkin-sync-state').textContent='北京时间 · 最近同步 '+dateFormat(model.server_time);
    $('checkin-configure').disabled=false;$('checkin-sign-now').disabled=false;return true;
  }catch(error){
    feedback('读取失败：'+error.message);$('checkin-sync-state').textContent='本地服务暂未连接';
    if(!loaded){$('checkin-sign-count').textContent='暂无法读取';$('checkin-flash-count').textContent='暂无法读取';$('checkin-account-summary').textContent='暂时无法读取各账号状态';for(const id of ['checkin-accounts','checkin-tasks']){const row=node('tr'),cell=node('td','checkin-empty','暂时无法读取，稍后点击“刷新”重试。');cell.colSpan=7;row.append(cell);$(id).replaceChildren(row);}}
    return false;
  }finally{loading=false;}
}
function updateAccountSelection(){
  const available=[...$('checkin-account-options').querySelectorAll('input:not(:disabled)')],selected=available.filter(input=>input.checked).length;
  $('checkin-account-count').textContent=`已选 ${selected} / ${available.length} 个`;
  $('checkin-select-accounts').textContent=available.length>0&&selected===available.length?'取消全选':'全选可用账号';
  $('checkin-select-accounts').disabled=!available.length;
}
function productLabel(product){return product.label||('消耗：'+(product.cost??'待官方确认'));}
function updateConfiguration(){
  $('checkin-signin-fields').hidden=!$('checkin-signin-enabled').checked;
  $('checkin-flash-fields').hidden=!$('checkin-flash-enabled').checked;
  const product=model.products.find(item=>item.id===$('checkin-product').value);
  $('checkin-product-description').textContent=product?productLabel(product)+(product.available===false?'。当前未开放，按预约时间再检查官方状态。':''):'选择商品后显示其消耗和参与条件。';
}
async function openEditor(asManual=false,loginId=''){
  if(working||!await refresh())return;
  manual=asManual;$('checkin-form').reset();$('checkin-form-error').hidden=true;
  $('checkin-burst-workers').value=model.settings?.burst_workers??8;
  $('checkin-editor-title').textContent=manual?'立即签到':'配置自动任务';
  $('checkin-editor-description').textContent=manual?'使用所选共享账号执行免费签到，每个账号今天最多一次。':'共享账号只需登录一次，选择后统一设置。';
  $('checkin-automatic-options').hidden=manual;$('checkin-save').textContent=manual?'开始签到':'保存自动任务';
  const options=model.accounts.map(account=>{
    const label=node('label','checkin-account-option'),check=node('input');check.type='checkbox';check.value=account.id;
    check.disabled=account.status!=='active';check.checked=!check.disabled&&account.id===loginId;
    const text=node('span');text.append(node('strong','',account.name||account.phone_masked),node('small','',account.phone_masked));
    if(check.disabled)text.append(node('small','checkin-account-unavailable',accountStateNames[account.status]||'暂不可用'));
    label.append(check,text);return label;
  });
  if(!options.length)options.push(node('p','checkin-setting-note','共享账号池为空，请先从左侧“账号管理”登录账号。'));
  $('checkin-account-options').replaceChildren(...options);
  const products=[node('option','','请选择商品')];products[0].value='';
  for(const product of model.products){const option=node('option','',`${product.name} · ${productLabel(product)}${product.available===false?'（当前未开放）':''}`);option.value=product.id;option.disabled=product.selectable!==true;products.push(option);}
  $('checkin-product').replaceChildren(...products);
  const flashReady=model.products.some(product=>product.selectable===true);
  $('checkin-flash-enabled').disabled=!flashReady;$('checkin-flash-unavailable').hidden=flashReady;
  if(loginId&&!manual){
    const jobs=model.tasks.filter(task=>task.login_id===loginId),sign=jobs.find(task=>task.kind==='signin'),flash=jobs.find(task=>task.kind==='flash');
    $('checkin-signin-enabled').checked=!!sign?.enabled;$('checkin-flash-enabled').checked=!!flash?.enabled&&flashReady;
    if(sign?.next_run_at)$('checkin-signin-time').value=new Date(sign.next_run_at).toLocaleTimeString('en-GB',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
    if(flash?.product_id)$('checkin-product').value=flash.product_id;
    if(flash)for(const key of ['count','interval','workers'])$('checkin-burst-'+key).value=flash['burst_'+key]??({count:40,interval:0.05,workers:8}[key]);
  }
  updateAccountSelection();updateConfiguration();$('checkin-editor').showModal();
}
async function stopTasks(ids){
  if(working)return;working=true;updateSelection();
  try{const result=await api('/api/checkin/tasks/stop',{ids});feedback(result.message||'停止请求已提交，正在执行的结果会保留。',true);await refresh();}
  catch(error){feedback(error.message);}
  finally{working=false;render();}
}
function renderHistory(){
  let records=[];
  if(historyAccountId){
    const account=model.accounts.find(item=>item.id===historyAccountId);
    $('checkin-history-description').textContent=account?(account.name||account.phone_masked)+' · 签到与秒杀记录':'账号已退出共享池';
    records=model.tasks.filter(task=>task.login_id===historyAccountId).flatMap(task=>(task.history||[]).map(record=>({...record,kind:task.kind}))).sort((a,b)=>(b.time||'').localeCompare(a.time||''));
  }else{
    const task=model.tasks.find(item=>item.id===historyId);
    if(!task){$('checkin-history-description').textContent='任务已不存在。';$('checkin-history-body').replaceChildren();return;}
    $('checkin-history-description').textContent=`${task.account_name} · ${kindNames[task.kind]||task.kind}`;
    records=[...(task.history||[])].reverse();
  }
  const rows=records.map(record=>{
    const row=node('article','checkin-history-item'),head=node('div');head.append(node('time','',(record.kind?(kindNames[record.kind]||record.kind)+' · ':'')+dateFormat(record.time)),badge(record.state));row.append(head,node('p','',record.message));
    if(record.attempts?.length){
      const details=node('details','checkin-attempts'),table=node('table'),thead=node('thead'),titles=node('tr'),body=node('tbody');
      details.append(node('summary','',`查看 ${record.attempt_count} 次请求明细`));
      for(const title of ['请求','实际发起 · 北京时间','结果'])titles.append(node('th','',title));
      thead.append(titles);
      for(const attempt of record.attempts){
        const item=node('tr');item.append(node('td','',attempt.followup?'延后补试':`第 ${attempt.index} 次`),node('td','',attempt.sent_at?dateFormat(attempt.sent_at)+'.'+(attempt.sent_at.match(/\.(\d{3})/)?.[1]||'000'):'未发送'),node('td','',attempt.message));body.append(item);
      }
      table.append(thead,body);details.append(table);row.append(details);
    }
    return row;
  });
  if(!rows.length)rows.push(node('p','checkin-empty',historyAccountId?'尚无客户端执行记录，最近签到结果可在账号表中查看。':'还没有执行记录。'));
  const scroll=$('checkin-history').scrollTop;$('checkin-history-body').replaceChildren(...rows);$('checkin-history').scrollTop=scroll;
}
function openHistory(id){historyAccountId='';historyId=id;renderHistory();$('checkin-history').showModal();}
function openAccountHistory(id){historyId='';historyAccountId=id;renderHistory();$('checkin-history').showModal();}
$('checkin-configure').disabled=true;$('checkin-sign-now').disabled=true;
$('checkin-refresh').addEventListener('click',refresh);
$('checkin-configure').addEventListener('click',()=>openEditor());$('checkin-sign-now').addEventListener('click',()=>openEditor(true));
$('checkin-settings-open').addEventListener('click',async()=>{
  if(working||!await refresh())return;
  $('checkin-settings-form').reset();$('checkin-settings-error').hidden=true;
  $('checkin-settings-workers').value=model.settings.burst_workers;
  $('checkin-settings-workers').dispatchEvent(new Event('input'));
  $('checkin-settings').showModal();
});
$('checkin-settings-workers').addEventListener('input',()=>{
  const workers=Number($('checkin-settings-workers').value);
  $('checkin-settings-example').textContent=Number.isInteger(workers)&&workers>=1&&workers<=32?`例如 10 个账号同时执行，理论上最多有 ${workers*10} 个请求在途；实际取决于发起间隔和接口响应速度。`:'可设置 1–32 个并发线程。';
});
$('checkin-settings-form').addEventListener('submit',async event=>{
  event.preventDefault();if(working)return;
  working=true;$('checkin-settings-save').disabled=true;$('checkin-settings-error').hidden=true;
  try{
    const result=await api('/api/checkin/settings',{burst_workers:Number($('checkin-settings-workers').value),apply_existing:$('checkin-settings-apply').checked});
    $('checkin-settings').close();feedback(result.message,true);await refresh();
  }catch(error){$('checkin-settings-error').textContent=error.message;$('checkin-settings-error').hidden=false;}
  finally{working=false;$('checkin-settings-save').disabled=false;render();}
});
$('checkin-settings').addEventListener('cancel',event=>{if(working)event.preventDefault();});
$('checkin-account-options').addEventListener('change',updateAccountSelection);
$('checkin-select-accounts').addEventListener('click',()=>{const options=[...$('checkin-account-options').querySelectorAll('input:not(:disabled)')],check=!options.every(input=>input.checked);options.forEach(input=>input.checked=check);updateAccountSelection();});
for(const id of ['checkin-signin-enabled','checkin-flash-enabled','checkin-product'])$(id).addEventListener('change',updateConfiguration);
$('checkin-form').addEventListener('submit',async event=>{
  event.preventDefault();if(working)return;
  try{
    const loginIds=[...$('checkin-account-options').querySelectorAll('input:checked')].map(input=>input.value);
    if(!loginIds.length)throw new Error('请选择至少一个可用账号。');
    const signin=$('checkin-signin-enabled').checked,flash=$('checkin-flash-enabled').checked,productId=$('checkin-product').value;
    if(!manual&&!signin&&!flash)throw new Error('请至少启用一项自动任务。');
    if(!manual&&flash&&!model.products.some(product=>product.id===productId&&product.selectable===true))throw new Error('请选择已确认可参与的秒杀商品。');
    working=true;$('checkin-save').disabled=true;$('checkin-form-error').hidden=true;
    const result=await api(manual?'/api/checkin/run':'/api/checkin/tasks/save',manual?{login_ids:loginIds,kind:'signin'}:{login_ids:loginIds,signin_enabled:signin,signin_time:$('checkin-signin-time').value,flash_enabled:flash,product_id:productId,burst_count:Number($('checkin-burst-count').value),burst_interval:Number($('checkin-burst-interval').value),burst_workers:Number($('checkin-burst-workers').value)});
    $('checkin-editor').close();feedback(result.message||(manual?'签到请求已完成，请查看各账号执行记录。':'自动任务已保存。'),true);await refresh();
  }catch(error){$('checkin-form-error').textContent=error.message;$('checkin-form-error').hidden=false;}
  finally{working=false;$('checkin-save').disabled=false;render();}
});
$('checkin-editor').addEventListener('cancel',event=>{if(working)event.preventDefault();});
document.querySelectorAll('[data-checkin-close]').forEach(button=>button.addEventListener('click',()=>{if(!working)$(button.dataset.checkinClose).close();}));
document.querySelectorAll('[data-checkin-kind]').forEach(button=>button.addEventListener('click',()=>{filter=button.dataset.checkinKind;selectedTasks.clear();document.querySelectorAll('[data-checkin-kind]').forEach(item=>{item.classList.toggle('selected',item===button);item.setAttribute('aria-pressed',String(item===button));});render();}));
document.querySelectorAll('[data-checkin-view]').forEach(button=>button.addEventListener('click',()=>{view=button.dataset.checkinView;selectedTasks.clear();document.querySelectorAll('[data-checkin-view]').forEach(item=>{item.classList.toggle('selected',item===button);item.setAttribute('aria-pressed',String(item===button));});render();}));
$('checkin-account-state-filter').addEventListener('change',render);
$('checkin-search').addEventListener('input',()=>{selectedTasks.clear();render();});
$('checkin-select-tasks').addEventListener('change',event=>{for(const task of visibleTasks().filter(stoppable)){if(event.target.checked)selectedTasks.add(task.id);else selectedTasks.delete(task.id);}render();});
$('checkin-clear-tasks').addEventListener('click',()=>{selectedTasks.clear();render();});
$('checkin-stop-tasks').addEventListener('click',()=>stopTasks([...selectedTasks]));
window.addEventListener('accounts-changed',refresh);window.addEventListener('focus',refresh);
refresh();setInterval(()=>{if(!document.hidden&&!working)refresh();},5000);
