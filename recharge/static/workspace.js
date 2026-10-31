import * as M from './task-model.js';
(() => {
  'use strict';
  const $=id=>document.getElementById(id),base=document.documentElement.dataset.apiBase||'';
  const csrf=document.querySelector('meta[name="csrf-token"]').content;
  const fmt=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const time=v=>v?fmt.format(new Date(v)):'—';
  const node=(tag,cls='',text='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=text;return e;};
  let data={accounts:[],logins:[],jobs:{}},selectedLogin='',tab=new URLSearchParams(location.search).get('tab')==='tasks'?'tasks':'activity',filter='all',selection=new Set(),shown=[],loaded=false,loading=false,mutation=false,stopping=[],queryHandled=false;
  const busy=new Set(),prizes=new Map();
  const prizeLabels={UNUSED:'可使用',USED:'已使用',EXPIRED:'已过期',TOSEND:'可赠送',SENDED:'已赠送',TOLOOK:'待查看',LOCKED:'待解锁',3:'发放中',1:'可使用'};
  async function api(path,body){const response=await fetch(base+path,{cache:'no-store',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)})});const value=await response.json();if(!response.ok){const error=new Error(value.error||'操作未完成');error.code=value.code;throw error;}return value;}
  function feedback(message=''){$('wed-feedback').textContent=message;$('wed-feedback').hidden=!message;}
  function button(text,action,cls='quiet'){const b=node('button',cls,text);b.type='button';b.addEventListener('click',action);return b;}
  function badge(text,kind='') {return node('span','wed-badge '+kind,text);}
  function empty(id,cols,text){const tr=node('tr'),td=node('td','wed-empty',text);td.colSpan=cols;tr.append(td);$(id).replaceChildren(tr);}
  function linked(loginId){return data.accounts.find(a=>a.login_id===loginId);}
  function week(){const date=new Date(Date.parse(data.server_time||new Date().toISOString())+8*3600000);date.setUTCDate(date.getUTCDate()+(3-date.getUTCDay()+7)%7);return date.toISOString().slice(0,10);}
  const skipped=job=>job.skip_week===week();
  const stoppable=row=>M.stoppable(row.job)&&!skipped(row.job);
  function accountCell(login){const td=node('td');td.append(node('strong','',login.name||login.phone_masked),node('small','',login.phone_masked));return td;}
  function taskRows(){const pool=new Map(data.logins.map(a=>[a.id,a]));const directory=data.accounts.map(a=>({...a,authenticated:pool.get(a.login_id)?.status==='active'}));return M.rows({...data,accounts:directory}).filter(r=>pool.has(r.account.login_id)).map(r=>skipped(r.job)?{...r,group:'ended',status:'本周已停止'}:r);}
  function renderAccounts(){
    $('wed-account-count').textContent=data.logins.length;
    const all=button('',()=>choose(''),'wed-account'),text=node('span');text.append(node('strong','','全部账号'),node('small','',`${data.logins.filter(a=>a.status==='active').length} 个有效账号自动参与`));all.append(text,node('span','','自动'));all.setAttribute('aria-pressed',String(!selectedLogin));
    const cards=data.logins.map(a=>{const b=button('',()=>choose(a.id),'wed-account'+(a.status==='active'?'':' expired')),info=node('span');info.append(node('strong','',a.name||a.phone_masked),node('small','',a.phone_masked));b.append(info,node('span','',a.status==='active'?'已登录':'需重新登录'));b.setAttribute('aria-pressed',String(a.id===selectedLogin));return b;});
    $('wed-accounts').replaceChildren(...(data.logins.length?[all,...cards]:[node('p','wed-note','暂无共享账号，请从左侧账号管理登录。')]));
    const current=data.logins.find(a=>a.id===selectedLogin);$('wed-scope').textContent=current?(current.name||current.phone_masked)+' · '+current.phone_masked:'全部账号';
  }
  function renderActivity(){
    const rows=data.logins.filter(a=>!selectedLogin||a.id===selectedLogin).map(login=>{
      const tr=node('tr'),account=linked(login.id),job=data.jobs[account?.id],state=account?.activity||{},active=M.active.has(job?.state);
      const status=node('td');status.append(login.status!=='active'?badge('需要重新登录','orange'):active?badge(M.labels[job.state],'blue'):state.state_label?badge(state.state_label,state.prize?'green':'blue'):badge('待周三准备','blue'));
      if(state.server_time)status.append(node('small','','最近查询 '+time(state.server_time)));
      const plan=node('td');plan.append(login.status==='active'?badge(job&&skipped(job)?'本周已停止':'自动参与',job&&skipped(job)?'':'green'):badge('登录恢复后自动纳入','orange'));
      if(job?.next_run_at)plan.append(node('small','',time(job.next_run_at)));else if(login.status==='active')plan.append(node('small','','周三 07:55 准备 → 08:00 领取'));
      const result=node('td','wed-result',job?.message||account?.error||(login.status==='active'?'无需手动设置，等待程序执行。':'请在账号管理恢复登录。'));
      const actions=node('td'),wrap=node('div','wed-row-actions');
      if(login.status==='active'){
        const read=button('查询资格',()=>queryAccount(login.id,true),'secondary'),view=button('查看奖品',()=>choose(login.id,true));read.disabled=view.disabled=busy.has(login.id)||active;wrap.append(read,view);
      }else{const relogin=button('重新登录',()=>{},'secondary');relogin.dataset.accountWindow='';relogin.dataset.accountRelogin=login.id;wrap.append(relogin);}
      if(job)wrap.append(button('领取记录',()=>details(account.id)));
      actions.append(wrap);tr.append(accountCell(login),status,plan,result,actions);return tr;
    });
    if(rows.length)$('wed-status-rows').replaceChildren(...rows);else empty('wed-status-rows',5,'共享账号池为空，登录后会自动加入每周三领取。');
    renderPrizes();
  }
  function renderPrizes(){
    const owner=data.logins.find(a=>a.id===selectedLogin),value=prizes.get(selectedLogin);$('wed-prize-owner').textContent=owner?(owner.name||owner.phone_masked)+' · '+owner.phone_masked:'选择账号后查看官方奖品记录';$('wed-prize-count').textContent=value?.items?.length?'· '+value.items.length+' 件':'';
    if(!owner){empty('wed-prizes',3,'点击账号或“查看奖品”，查看该账号已领取的奖品。');return;}
    if(!value?.items?.length){empty('wed-prizes',3,value?.message||(owner.status==='active'?'该账号暂无奖品记录。':'恢复账号登录后可查看奖品。'));return;}
    $('wed-prizes').replaceChildren(...value.items.map(p=>{const tr=node('tr'),name=node('td'),title=p.prizeName||p.name||'充值券';name.append(node('strong','',p.shortName&&!title.includes(p.shortName)?p.shortName+' '+title:title));if(p.subTitle)name.append(node('small','',p.subTitle));const status=node('td');status.append(badge(prizeLabels[p.sendStatus]||'已记录',p.sendStatus==='EXPIRED'?'':'green'));tr.append(name,node('td','',p.getTime||'—'),status);return tr;}));
  }
  function updateSelection(){const ids=shown.filter(stoppable).map(r=>r.id);selection=new Set([...selection].filter(id=>ids.includes(id)));$('wed-select-all').checked=ids.length>0&&ids.every(id=>selection.has(id));$('wed-select-all').indeterminate=selection.size>0&&!$('wed-select-all').checked;$('wed-select-all').disabled=!ids.length||mutation;$('wed-selected-count').textContent=`已选 ${selection.size} 项`;$('wed-stop-selected').disabled=!selection.size||mutation;}
  function renderTasks(){
    const rows=taskRows().filter(r=>!selectedLogin||r.account.login_id===selectedLogin);
    $('wed-task-count').textContent=rows.length;
    shown=M.visible(rows,filter,$('wed-task-search').value,'next');
    document.querySelectorAll('[data-wed-filter]').forEach(b=>{const active=b.dataset.wedFilter===filter;b.classList.toggle('selected',active);b.setAttribute('aria-pressed',String(active));});
    const elements=shown.map(r=>{const {job,account}=r,tr=node('tr'),select=node('td'),check=node('input');check.type='checkbox';check.checked=selection.has(r.id);check.disabled=!stoppable(r)||mutation;check.setAttribute('aria-label',`选择 ${account.name} 的任务`);check.addEventListener('change',()=>{check.checked?selection.add(r.id):selection.delete(r.id);updateSelection();});select.append(check);
      const login=data.logins.find(a=>a.id===account.login_id),status=node('td'),label=r.status==='已预约'&&!account.activity?.server_time?'等待07:55准备':r.status;
      status.append(badge(label,r.group==='attention'?'orange':r.group==='running'?'blue':skipped(job)?'':'green'));if(job.state==='success')status.append(node('small','','上场领取成功'));
      const plan=node('td');plan.append(node('span','',time(job.next_run_at||job.run_at)),node('small','',skipped(job)?'下周自动恢复':'每周三自动执行'));
      const progress=node('td');progress.append(node('strong','',`${job.attempts||0} / ${job.max_attempts} 轮`));const bar=node('progress','wed-progress');bar.max=job.max_attempts;bar.value=job.attempts||0;bar.setAttribute('aria-label',`已尝试 ${job.attempts||0} 轮`);progress.append(bar,node('small','',`实际提交 ${job.submissions||0} 次`));
      const result=node('td','wed-result',job.message||'等待周三自动执行');result.append(node('small','','更新 '+time(job.updated_at)));
      const actions=node('td'),wrap=node('div','wed-row-actions');wrap.append(button('记录',()=>details(r.id),'secondary'));if(stoppable(r)){const stop=button('停止本周',()=>openStop([r.id]));stop.disabled=mutation;wrap.append(stop);}if(login.status!=='active'){const relogin=button('重新登录',()=>{});relogin.dataset.accountWindow='';relogin.dataset.accountRelogin=login.id;wrap.append(relogin);}actions.append(wrap);tr.append(select,accountCell(login),status,plan,progress,result,actions);return tr;});
    if(elements.length)$('wed-task-rows').replaceChildren(...elements);else empty('wed-task-rows',7,data.logins.length?'当前筛选下没有任务。有效账号会自动加入，无需新建。':'登录共享账号后，将自动生成周三领取任务。');updateSelection();
  }
  function render(){renderAccounts();renderActivity();renderTasks();for(const name of ['activity','tasks']){$('wed-tab-'+name).setAttribute('aria-selected',String(tab===name));$('wed-'+name).hidden=tab!==name;}$('wed-warning').textContent=data.warning||'';$('wed-warning').hidden=!data.warning;$('wed-sync').textContent='北京时间 · 最近同步 '+time(data.server_time);}
  async function refresh(){if(loading)return;loading=true;try{data=await api('/api/accounts');loaded=true;if(selectedLogin&&!data.logins.some(a=>a.id===selectedLogin))selectedLogin='';render();if(!queryHandled){queryHandled=true;const p=new URLSearchParams(location.search),id=p.get('detail')||p.get('edit');if(id&&data.jobs[id])details(id);}}catch(error){feedback(error.message);if(!loaded)empty('wed-status-rows',5,'暂时无法读取，请点击刷新重试。');}finally{loading=false;}}
  function choose(id,force=false){selectedLogin=id;render();if(id&&data.logins.find(a=>a.id===id)?.status==='active'&&(!prizes.has(id)||force))queryAccount(id,false);}
  async function queryAccount(loginId,withStatus){
    if(busy.has(loginId))return;busy.add(loginId);if(!withStatus){prizes.set(loginId,{message:'正在查询官方奖品…'});renderPrizes();}renderActivity();
    try{let account=linked(loginId),id=account?.id;if(!account?.authenticated){const attached=await api('/api/login/attach',{login_id:loginId,...(id?{account_id:id}:{})});id=attached.account_id;}
      if(withStatus)await api('/api/refresh',{account_id:id});const result=await api('/api/prizes?account_id='+encodeURIComponent(id));prizes.set(loginId,{items:result.prizes});feedback();await refresh();
    }catch(error){prizes.set(loginId,{message:error.message});feedback(error.message);}finally{busy.delete(loginId);renderActivity();}
  }
  function details(id){const job=data.jobs[id],account=data.accounts.find(a=>a.id===id);$('wed-detail-title').textContent=(account?.name||'账号')+' · 领取记录';$('wed-detail-summary').textContent=`每周三 07:55 准备、08:00 领取 · 已尝试 ${job.attempts||0}/${job.max_attempts} 轮 · 实际提交 ${job.submissions||0} 次`;
    const records=[...(job.history||[])].reverse();$('wed-detail-history').replaceChildren(...(records.length?records.map(e=>{const item=node('article','wed-history');item.append(node('time','',time(e.time)),node('p','',e.message));return item;}):[node('p','wed-note',job.message||'尚未开始执行。')]));$('wed-detail').showModal();}
  function openStop(ids){stopping=ids;$('wed-stop-description').textContent=`停止所选 ${ids.length} 个账号本周的领取任务？`;$('wed-stop-error').hidden=true;$('wed-stop').showModal();}
  $('wed-refresh').addEventListener('click',refresh);for(const name of ['activity','tasks'])$('wed-tab-'+name).addEventListener('click',()=>{tab=name;const url=new URL(location.href);name==='tasks'?url.searchParams.set('tab','tasks'):url.searchParams.delete('tab');history.replaceState(null,'',url);render();});
  document.querySelectorAll('[data-wed-filter]').forEach(b=>b.addEventListener('click',()=>{filter=b.dataset.wedFilter;selection.clear();renderTasks();}));$('wed-task-search').addEventListener('input',()=>{selection.clear();renderTasks();});
  $('wed-select-all').addEventListener('change',()=>{for(const r of shown.filter(stoppable))$('wed-select-all').checked?selection.add(r.id):selection.delete(r.id);renderTasks();});$('wed-stop-selected').addEventListener('click',()=>openStop([...selection]));
  $('wed-stop-form').addEventListener('submit',async e=>{e.preventDefault();if(mutation)return;mutation=true;$('wed-stop-confirm').disabled=true;try{await api('/api/schedules/cancel',{account_ids:stopping});selection.clear();$('wed-stop').close();feedback('已停止本周的后续请求，下周三会自动恢复。');await refresh();}catch(error){$('wed-stop-error').textContent=error.message;$('wed-stop-error').hidden=false;}finally{mutation=false;$('wed-stop-confirm').disabled=false;renderTasks();}});
  document.querySelectorAll('[data-wed-close]').forEach(b=>b.addEventListener('click',()=>{if(!mutation)b.closest('dialog').close();}));$('wed-stop').addEventListener('cancel',e=>{if(mutation)e.preventDefault();});
  window.addEventListener('accounts-changed',refresh);window.addEventListener('focus',refresh);refresh();setInterval(()=>{if(!document.hidden&&!mutation)refresh();},4000);
})();
