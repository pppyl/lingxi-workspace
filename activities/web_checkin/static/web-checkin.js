'use strict';
const $=id=>document.getElementById(id);
const stateNames={login_required:'渠道待登录',ready:'已登录',running:'正在检查',queued:'等待检查',signed:'今日已签到',completed:'本周期已完成',unsigned:'今日未签到',unknown:'尚未查询',failed:'检查失败',error:'检查失败',ineligible:'不符合活动资格',rate_limited:'请求频繁',uncertain:'结果待核实',unknown_result:'结果待核实'};
let model={settings:{enabled:true,time:'08:00'},accounts:[]},localRefresh=Promise.resolve(true),refreshing=false,loginId='',historyId='',loginWorking=false;
const smsDeadlines=new Map(),smsPending=new Set();
const dateText=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
function node(tag,className='',text){const element=document.createElement(tag);element.className=className;if(text!==undefined)element.textContent=text;return element;}
async function api(path,payload){
  const response=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const text=await response.text();let data;
  try{data=JSON.parse(text);}catch{throw new Error('服务未返回有效结果，请稍后刷新。');}
  if(!response.ok){const error=new Error(data.error||'操作未完成，请重试。');error.retryAfter=data.retry_after;throw error;}
  return data;
}
function feedback(message,success=false){$('web-checkin-feedback').textContent=message;$('web-checkin-feedback').hidden=!message;$('web-checkin-feedback').classList.toggle('success',success);}
function badge(text,tone='ended'){return node('span','checkin-badge '+tone,text);}
function accountState(account){return account.activity_state||'unknown';}
function attention(account){return ['login_required','failed','error','ineligible','rate_limited','uncertain','unknown_result'].includes(accountState(account));}
function renderHistory(){
  const account=model.accounts.find(item=>item.id===historyId);
  $('web-checkin-history-account').textContent=account?`${account.label} · ${account.phone_masked}`:'账号已退出共享池';
  const records=account?.records||[];
  const rows=[...records].reverse().map(record=>{const item=node('article','web-checkin-history-item'),head=node('div');head.append(node('time','',dateText(record.time||record.at)),badge(stateNames[record.state]||record.state||'执行记录',record.state==='signed'?'success':'ended'));item.append(head,node('p','',record.message||''));return item;});
  $('web-checkin-history-body').replaceChildren(...(rows.length?rows:[node('p','web-checkin-note','暂无本渠道执行记录。')]));
}
function render(){
  const signed=model.accounts.filter(account=>account.today_signed===true).length;
  $('web-checkin-signed-total').textContent=signed;
  $('web-checkin-login-total').textContent=model.accounts.filter(account=>accountState(account)==='login_required').length;
  $('web-checkin-total').textContent=model.accounts.length;
  $('web-checkin-auto-time').textContent=model.settings.enabled?`每天 ${model.settings.time} 起`:'已关闭';
  const query=$('web-checkin-search').value.trim().toLocaleLowerCase(),filter=$('web-checkin-filter').value;
  const accounts=model.accounts.filter(account=>`${account.label} ${account.phone_masked}`.toLocaleLowerCase().includes(query)&&(filter==='all'||filter==='signed'&&account.today_signed===true||filter==='unsigned'&&account.today_signed===false&&!account.cycle_complete&&accountState(account)!=='completed'||filter==='attention'&&attention(account)));
  $('web-checkin-summary').textContent=`显示 ${accounts.length} / ${model.accounts.length} 个账号 · 今日已签到 ${signed} 个`;
  const rows=accounts.map(account=>{
    const row=node('tr'),identity=node('td'),today=node('td'),count=node('td'),recent=node('td'),next=node('td'),actionsCell=node('td'),actions=node('div','checkin-row-actions');
    const state=accountState(account),busy=['running','queued'].includes(state),complete=account.cycle_complete===true||state==='completed';
    identity.append(node('strong','',account.label||account.phone_masked),node('small','',account.phone_masked));
    if(account.status==='login_required')identity.append(node('small','web-checkin-shared-status','共享 APP 登录需恢复'));
    today.append(account.today_signed===true?badge('今日已签到','success'):complete?badge('本周期已完成','success'):account.today_signed===false?badge('今日未签到','waiting'):badge('尚未查询'));
    if(state==='login_required')today.append(node('small','','本渠道需要单独登录'));
    else if(busy||attention(account))today.append(node('small','',stateNames[state]||state));
    count.append(node('strong','web-checkin-amount',`${account.marked_times??'—'} / ${account.total_times??'—'}`),node('small','','本渠道累计次数'));
    recent.append(node('time','',dateText(account.last_at)),node('p','web-checkin-result',account.message||'尚未核对官方签到进度'));
    next.append(node('time','',model.settings.enabled?dateText(account.next_run_at):'自动签到未开启'));
    const queryButton=node('button','secondary','查询');queryButton.type='button';queryButton.disabled=busy;queryButton.addEventListener('click',()=>run(account.id,'refresh'));actions.append(queryButton);
    if(state==='login_required'){const login=node('button','primary','短信登录');login.type='button';login.disabled=busy;login.addEventListener('click',()=>openLogin(account.id));actions.append(login);}
    else{const signin=node('button','secondary','签到');signin.type='button';signin.disabled=busy||complete||account.today_signed===true||state==='ineligible';signin.addEventListener('click',()=>run(account.id,'signin'));actions.append(signin);}
    const history=node('button','quiet','记录');history.type='button';history.addEventListener('click',()=>{historyId=account.id;renderHistory();$('web-checkin-history').showModal();});actions.append(history);
    actionsCell.append(actions);row.append(identity,today,count,recent,next,actionsCell);return row;
  });
  if(!rows.length){const row=node('tr'),cell=node('td','checkin-empty',model.accounts.length?'当前筛选下没有账号。':'共享账号池为空，请先从左侧“账号管理”添加账号。');cell.colSpan=6;row.append(cell);rows.push(row);}
  $('web-checkin-accounts').replaceChildren(...rows);
  $('web-checkin-warning').textContent=model.warning||'';$('web-checkin-warning').hidden=!model.warning;
  $('web-checkin-sync').textContent='北京时间 · 最近同步 '+dateText(model.server_time);
  $('web-checkin-settings-open').disabled=false;
  if($('web-checkin-history').open)renderHistory();
}
function apply(value){model=value;render();}
function refreshLocal(){
  localRefresh=localRefresh.then(async()=>{
    try{apply(await api('/api/checkin-web/status'));return true;}
    catch(error){feedback(error.message);return false;}
  });
  return localRefresh;
}
async function run(id,kind){
  try{await api('/api/checkin-web/run',{id,kind});await refreshLocal();}
  catch(error){feedback(error.message);}
}
async function refreshOfficial(){
  if(refreshing)return;refreshing=true;$('web-checkin-refresh').disabled=true;
  try{
    if(!await refreshLocal())return;
    const accounts=model.accounts.filter(account=>account.status==='active');let cursor=0;
    await Promise.all(Array.from({length:Math.min(4,accounts.length)},async()=>{while(cursor<accounts.length){const account=accounts[cursor++];await run(account.id,'refresh');}}));
    await refreshLocal();
  }finally{refreshing=false;$('web-checkin-refresh').disabled=false;}
}
function updateSms(){
  const remaining=Math.max(0,Math.ceil(((smsDeadlines.get(loginId)||0)-Date.now())/1000));
  $('web-checkin-sms').disabled=smsPending.has(loginId)||loginWorking||remaining>0;
  $('web-checkin-sms').textContent=remaining?`${remaining} 秒后重发`:'获取验证码';
}
async function sendSms(){
  const id=loginId;
  if(smsPending.has(id)||loginWorking||(smsDeadlines.get(id)||0)>Date.now())return;
  smsPending.add(id);$('web-checkin-login-error').hidden=true;updateSms();
  try{const result=await api('/api/checkin-web/sms',{id});smsDeadlines.set(id,Date.now()+(result.retry_after??60)*1000);if(id===loginId)$('web-checkin-sms-status').textContent=result.message||'验证码已发送，请填写本次收到的验证码。';}
  catch(error){if(error.retryAfter)smsDeadlines.set(id,Date.now()+error.retryAfter*1000);if(id===loginId){$('web-checkin-login-error').textContent=error.message;$('web-checkin-login-error').hidden=false;}}
  finally{smsPending.delete(id);updateSms();}
}
async function openLogin(id){
  loginId=id;const account=model.accounts.find(item=>item.id===id);
  $('web-checkin-code').value='';$('web-checkin-login-error').hidden=true;$('web-checkin-sms-status').textContent='';
  $('web-checkin-login-account').textContent=`${account.label} · ${account.phone_masked}`;updateSms();$('web-checkin-login').showModal();await sendSms();
}
$('web-checkin-refresh').addEventListener('click',refreshOfficial);
$('web-checkin-search').addEventListener('input',render);$('web-checkin-filter').addEventListener('change',render);
$('web-checkin-settings-open').addEventListener('click',()=>{$('web-checkin-enabled').checked=model.settings.enabled;$('web-checkin-time').value=model.settings.time;$('web-checkin-settings-error').hidden=true;$('web-checkin-settings').showModal();});
$('web-checkin-settings-form').addEventListener('submit',async event=>{
  event.preventDefault();$('web-checkin-settings-save').disabled=true;
  try{await api('/api/checkin-web/settings',{enabled:$('web-checkin-enabled').checked,time:$('web-checkin-time').value});await refreshLocal();$('web-checkin-settings').close();feedback('自动签到设置已保存。',true);}
  catch(error){$('web-checkin-settings-error').textContent=error.message;$('web-checkin-settings-error').hidden=false;}
  finally{$('web-checkin-settings-save').disabled=false;}
});
$('web-checkin-sms').addEventListener('click',sendSms);
$('web-checkin-login-form').addEventListener('submit',async event=>{
  event.preventDefault();if(loginWorking)return;
  loginWorking=true;$('web-checkin-login-submit').disabled=true;updateSms();$('web-checkin-login-error').hidden=true;
  try{await api('/api/checkin-web/login',{id:loginId,code:$('web-checkin-code').value.trim()});await refreshLocal();$('web-checkin-code').value='';$('web-checkin-login').close();feedback('本渠道登录已恢复，将按自动设置核对今天的签到。',true);}
  catch(error){$('web-checkin-login-error').textContent=error.message;$('web-checkin-login-error').hidden=false;}
  finally{loginWorking=false;$('web-checkin-login-submit').disabled=false;updateSms();}
});
for(const button of document.querySelectorAll('[data-web-checkin-close]'))button.addEventListener('click',()=>{if(!loginWorking||button.dataset.webCheckinClose!=='web-checkin-login')$(button.dataset.webCheckinClose).close();});
$('web-checkin-login').addEventListener('cancel',event=>{if(loginWorking)event.preventDefault();});
window.addEventListener('accounts-changed',refreshLocal);
setInterval(()=>{if(!document.hidden)refreshLocal();},5000);
setInterval(updateSms,1000);
refreshOfficial();
