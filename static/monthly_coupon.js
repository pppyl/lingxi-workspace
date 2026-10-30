(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const names={unknown:'未查询',available:'可领取',claimed:'本月已领取',completed:'已完成',not_eligible:'不符合条件',failed:'执行失败',login_required:'需要重新登录',running:'执行中',pending:'待完成'};
  const selected=new Set();
  let model={accounts:[]},loading=false,working=false,loaded=false,editing=[];
  const dateFormat=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
  const claimed=state=>state==='claimed'||state==='completed';
  const available=account=>account.status==='active'&&account.eligible!==false;
  function node(tag,className='',text){const element=document.createElement(tag);element.className=className;if(text!==undefined)element.textContent=text;return element;}
  function badge(state){return node('span','monthly-badge '+(claimed(state)?'success':state==='running'?'running':state==='failed'||state==='login_required'?'error':state==='available'?'available':''),names[state]||state||names.unknown);}
  function prizeName(prize){return typeof prize==='string'?prize:prize?.name||prize?.prizeName||'';}
  async function api(path,payload){const response=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const value=await response.json();if(!response.ok)throw new Error(value.error||'操作未完成，请重试。');return value;}
  function feedback(message,success=false){$('monthly-feedback').textContent=message;$('monthly-feedback').hidden=!message;$('monthly-feedback').classList.toggle('success',success);}
  function visibleAccounts(){
    const query=$('monthly-search').value.trim().toLocaleLowerCase(),filter=$('monthly-filter').value;
    return model.accounts.filter(account=>{
      if(!`${account.name} ${account.phone_masked}`.toLocaleLowerCase().includes(query))return false;
      if(filter==='completed')return claimed(account.direct_state)&&claimed(account.bonus_state);
      if(filter==='automatic')return account.auto_enabled===true;
      if(filter==='attention')return account.status==='login_required'||[account.direct_state,account.bonus_state].some(state=>['failed','login_required'].includes(state));
      if(filter==='ineligible')return account.eligible===false||account.direct_state==='not_eligible';
      if(filter==='pending')return account.eligible!==false&&!(claimed(account.direct_state)&&claimed(account.bonus_state));
      return true;
    });
  }
  function updateSelection(){
    const options=visibleAccounts().filter(available),count=options.filter(account=>selected.has(account.id)).length;
    $('monthly-select-all').checked=options.length>0&&count===options.length;
    $('monthly-select-all').indeterminate=count>0&&count<options.length;
    $('monthly-select-all').disabled=!options.length||working;
    $('monthly-selected-count').textContent=`已选 ${selected.size} 个`;
    $('monthly-clear-selection').hidden=!selected.size;
    $('monthly-run-selected').disabled=!selected.size||working;
    $('monthly-configure-selected').disabled=!selected.size||working;
  }
  function render(){
    const shown=visibleAccounts(),valid=new Set(shown.filter(available).map(account=>account.id));
    for(const id of selected)if(!valid.has(id))selected.delete(id);
    $('monthly-total').textContent=model.accounts.length;
    $('monthly-summary').textContent=`显示 ${shown.length} / ${model.accounts.length} 个 · 直接已领 ${model.accounts.filter(account=>claimed(account.direct_state)).length} 个 · 额外已领 ${model.accounts.filter(account=>claimed(account.bonus_state)).length} 个`;
    const rows=shown.map(account=>{
      const row=node('tr'),selection=node('td','monthly-cell-select'),check=node('input');check.type='checkbox';check.checked=selected.has(account.id);check.disabled=working||!available(account);check.setAttribute('aria-label',`选择 ${account.name||account.phone_masked}`);check.addEventListener('change',()=>{if(check.checked)selected.add(account.id);else selected.delete(account.id);updateSelection();});selection.append(check);
      const identity=node('td');identity.append(node('strong','',account.name||account.phone_masked),node('small','',account.phone_masked));
      if(account.status!=='active')identity.append(node('small','monthly-account-warning',account.status==='login_required'?'需要重新登录':account.status==='restoring'?'正在恢复登录':'等待重连'));
      if(account.eligible===false)identity.append(node('small','monthly-account-warning','不符合本活动条件'));
      const direct=node('td');direct.append(badge(account.direct_state||'unknown'));if(prizeName(account.direct_prize))direct.append(node('strong','monthly-prize',prizeName(account.direct_prize)));if(account.direct_message)direct.append(node('p','monthly-result',account.direct_message));
      const tasks=node('td','monthly-progress-cell'),progress=account.task_progress;
      if(typeof progress==='number'&&Number.isFinite(progress)){
        const heading=node('div','monthly-progress-heading');heading.append(node('strong','','任务进度'),node('span','',progress+'%'));const meter=node('progress');meter.max=100;meter.value=progress;meter.setAttribute('aria-label',`${account.name||account.phone_masked} 额外领取任务进度`);tasks.append(heading,meter);
      }else tasks.append(node('span','monthly-task-unknown','任务进度未查询'));
      const items=node('div','monthly-task-items');
      for(const task of account.tasks||[]){if(task.name?.includes('打卡'))continue;const done=task.done===true||typeof task.done==='number'&&typeof task.total==='number'&&task.done>=task.total;const item=node('span','monthly-task-item'+(done?' done':''));item.append(node('span','monthly-task-dot',done?'✓':'·'),node('span','',task.name));items.append(item);}
      if(!items.children.length)items.append(node('small','','三个浏览任务尚未查询'));
      const punch=node('div','monthly-punch');punch.append(node('span','',`累计打卡 ${account.punch_days===null||account.punch_days===undefined?'—':account.punch_days} / 3 天`));if(account.punched_today===true)punch.append(node('span','monthly-punch-today','今日已打卡'));else if(account.punched_today===false)punch.append(node('span','monthly-punch-pending','今日未打卡'));tasks.append(items,punch);
      const bonus=node('td');bonus.append(badge(account.bonus_state||'unknown'));if(prizeName(account.bonus_prize))bonus.append(node('strong','monthly-prize',prizeName(account.bonus_prize)));if(account.bonus_message)bonus.append(node('p','monthly-result',account.bonus_message));
      const automatic=node('td');automatic.append(node('span','monthly-badge '+(account.auto_enabled?'success':''),account.auto_enabled===true?'已开启':account.auto_enabled===false?'未开启':'未读取'));if(account.next_run_at)automatic.append(node('small','','下次 '+dateFormat(account.next_run_at)));
      const recent=node('td');recent.append(node('time','',dateFormat(account.last_run_at)),node('p','monthly-result',account.message||'暂无执行结果'));recent.title=account.message||'';
      const operations=node('td'),actions=node('div','monthly-row-actions');
      const run=node('button','secondary','执行');run.type='button';run.disabled=working||!available(account)||account.direct_state==='running'||account.bonus_state==='running';run.addEventListener('click',()=>runAccounts([account.id]));actions.append(run);
      const settings=node('button','quiet','设置');settings.type='button';settings.disabled=working||!available(account);settings.addEventListener('click',()=>openSettings([account.id]));actions.append(settings);
      if(account.auto_enabled){const stop=node('button','quiet','停用自动');stop.type='button';stop.disabled=working;stop.addEventListener('click',()=>saveSettings([account.id],false,'08:05'));actions.append(stop);}
      if(account.status==='login_required'){const login=node('button','quiet','重新登录');login.type='button';login.dataset.accountWindow='';login.dataset.accountRelogin=account.id;actions.append(login);}
      operations.append(actions);row.append(selection,identity,direct,tasks,bonus,automatic,recent,operations);return row;
    });
    if(!rows.length){const row=node('tr'),cell=node('td','monthly-empty',model.accounts.length?'当前筛选下没有账号。':'共享账号池为空，请从左侧“账号管理”登录账号。');cell.colSpan=8;row.append(cell);rows.push(row);}
    $('monthly-accounts').replaceChildren(...rows);updateSelection();
  }
  async function refresh(){
    if(loading)return;loading=true;
    try{model=await api('/api/monthly-coupon/status');loaded=true;render();$('monthly-warning').textContent=model.warning||'';$('monthly-warning').hidden=!model.warning;$('monthly-sync').textContent='北京时间 · 最近同步 '+dateFormat(model.server_time);$('monthly-month').textContent=model.server_time?new Date(model.server_time).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'long'}):'';}
    catch(error){feedback('读取失败：'+error.message);$('monthly-sync').textContent='本地服务暂未连接';if(!loaded){$('monthly-summary').textContent='暂时无法读取';const row=node('tr'),cell=node('td','monthly-empty','暂时无法读取，稍后点击“刷新”重试。');cell.colSpan=8;row.append(cell);$('monthly-accounts').replaceChildren(row);}}
    finally{loading=false;}
  }
  async function runAccounts(ids){
    if(working)return;working=true;render();
    try{const value=await api('/api/monthly-coupon/run',{login_ids:ids});feedback(value.message||'已安排所选账号检查领取与任务状态，请查看表格中的官方结果。',true);await refresh();}
    catch(error){feedback(error.message);}
    finally{working=false;render();}
  }
  function updateTime(){const enabled=$('monthly-auto-enabled').checked;$('monthly-time-field').hidden=!enabled;$('monthly-auto-time').required=enabled;}
  function openSettings(ids){
    editing=ids;$('monthly-settings-form').reset();$('monthly-settings-error').hidden=true;
    const account=ids.length===1?model.accounts.find(item=>item.id===ids[0]):null;
    $('monthly-settings-description').textContent=account?`设置 ${account.name||account.phone_masked} 的自动任务`:`统一设置所选 ${ids.length} 个账号`;
    $('monthly-auto-enabled').checked=account?account.auto_enabled!==false:true;
    if(account?.next_run_at)$('monthly-auto-time').value=new Date(account.next_run_at).toLocaleTimeString('en-GB',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
    updateTime();$('monthly-settings').showModal();
  }
  async function saveSettings(ids,enabled,time){
    if(working)return;working=true;$('monthly-settings-save').disabled=true;render();
    try{const value=await api('/api/monthly-coupon/settings',{login_ids:ids,enabled,time});$('monthly-settings').close();feedback(value.message||(enabled?'自动执行设置已保存。':'已停用所选账号的自动执行。'),true);await refresh();}
    catch(error){if($('monthly-settings').open){$('monthly-settings-error').textContent=error.message;$('monthly-settings-error').hidden=false;}else feedback(error.message);}
    finally{working=false;$('monthly-settings-save').disabled=false;render();}
  }
  $('monthly-refresh').addEventListener('click',refresh);
  for(const id of ['monthly-search','monthly-filter'])$(id).addEventListener(id==='monthly-search'?'input':'change',()=>{selected.clear();render();});
  $('monthly-select-all').addEventListener('change',event=>{for(const account of visibleAccounts().filter(available)){if(event.target.checked)selected.add(account.id);else selected.delete(account.id);}render();});
  $('monthly-clear-selection').addEventListener('click',()=>{selected.clear();render();});
  $('monthly-run-selected').addEventListener('click',()=>runAccounts([...selected]));
  $('monthly-configure-selected').addEventListener('click',()=>openSettings([...selected]));
  $('monthly-auto-enabled').addEventListener('change',updateTime);
  $('monthly-settings-form').addEventListener('submit',event=>{event.preventDefault();saveSettings(editing,$('monthly-auto-enabled').checked,$('monthly-auto-time').value);});
  document.querySelectorAll('[data-monthly-close]').forEach(button=>button.addEventListener('click',()=>{if(!working)$('monthly-settings').close();}));
  $('monthly-settings').addEventListener('cancel',event=>{if(working)event.preventDefault();});
  window.addEventListener('accounts-changed',refresh);window.addEventListener('focus',refresh);
  refresh();setInterval(()=>{if(!document.hidden&&!working)refresh();},5000);
})();
