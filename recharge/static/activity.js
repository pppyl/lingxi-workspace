'use strict';
const base = document.documentElement.dataset.apiBase || '';
const unified = document.documentElement.dataset.unifiedLogin !== 'false' && !!base;
const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
const $ = id => document.getElementById(id);
const activeStates = new Set(['preparing','armed','running','cancelling']);
const jobNames = {waiting:'等待执行',preparing:'正在准备',armed:'等待到点',running:'正在领取',cancelling:'正在停止',cancelled:'已停止',success:'领取成功',exhausted:'已达尝试上限',failed:'需要处理',interrupted:'执行中断',missed:'错过时间',completed:'本场已结束'};
const prizeNames = {UNUSED:'可使用',USED:'已使用',EXPIRED:'已过期',TOSEND:'可赠送',SENDED:'已赠送',TOLOOK:'待查看',LOCKED:'待解锁',3:'发放中',1:'可使用'};
let accounts = [], logins = [], jobs = {}, selectedLogin = '', selectedId = null;
let state = null, anchor = null, busy = false, syncing = false, initialized = false;
let localPending = null, pollTimer, toastTimer, selectionVersion = 0;

function node(tag, cls, text) {
  const value = document.createElement(tag); if (cls) value.className = cls;
  if (text !== undefined) value.textContent = text; return value;
}
function notice(text = '') { $('page-notice').textContent = text; $('page-notice').hidden = !text; }
function toast(text, error = false) {
  $('toast').textContent = text; $('toast').classList.toggle('error',error); $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true,error ? 7000 : 4000);
}
async function api(path, body, id = selectedId) {
  if (id && ['/api/status','/api/refresh','/api/prizes','/api/claim','/api/login/sms','/api/login','/api/accounts/remove'].includes(path)) {
    if (body === undefined) path += '?account_id=' + encodeURIComponent(id); else body = {...body,account_id:id};
  }
  const response = await fetch(base + path,{cache:'no-store',signal:AbortSignal.timeout(60000),
    ...(body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)})});
  let result;
  try { result = await response.json(); } catch { throw new Error('本地服务响应异常，请刷新页面。'); }
  if (!response.ok) { const error = new Error(result.error || '操作失败，请稍后重试。'); error.code=result.code; error.status=response.status; throw error; }
  return result;
}
function messageFor(error) {
  return ['TypeError','TimeoutError'].includes(error.name) ? '暂时无法连接服务，请稍后刷新。' : error.message;
}
function linked() { return accounts.find(a => unified ? a.login_id === selectedLogin : a.id === selectedLogin); }
function renderControls() {
  const row = linked(), working = busy || syncing, connected = !!row?.authenticated;
  const hasSelection = !!selectedLogin;
  $('activity-login').disabled = working || !logins.length;
  $('refresh-button').disabled = working || activeStates.has(jobs[row?.id]?.state);
  $('prizes-button').disabled = working || !row || !connected || activeStates.has(jobs[row.id]?.state);
  $('current-app-login').hidden = !unified || connected || !hasSelection;
  $('current-app-login').disabled = working;
  $('connection-status').textContent = connected ? '已连接' : {restoring:'正在恢复',retry:'等待重连',login_required:'需要验证'}[row?.session_status] || (hasSelection ? '待连接' : '暂无账号');
  $('connection-status').classList.toggle('connected',connected);
  $('remove-account-button').hidden = !row || accounts.length <= 1;
  $('remove-account-button').disabled = working || activeStates.has(jobs[row?.id]?.state);
  $('claim-button').disabled = working || !state?.can_claim || !connected || activeStates.has(jobs[row?.id]?.state);
}
function clearView() {
  state=null; anchor=null;
  $('server-time').textContent=''; $('opening-note').textContent='每周三 08:00 开放';
  $('prize-count').textContent=''; $('pool-details').hidden=true; $('pool-details').open=false;
  renderPrizes(null); renderState();
}
function renderState() {
  const row=linked(), job=jobs[selectedId];
  $('claim-label').classList.toggle('ready',!!state?.can_claim);
  if (!row?.authenticated || !state?.authenticated) {
    $('claim-label').textContent=row?.authenticated ? '同步中' : '待连接';
    $('countdown').textContent=selectedLogin ? (row?.authenticated ? '正在同步本场状态' : '连接账号，查看领取资格') : '选择账号，查看领取资格';
    $('claim-reason').textContent=selectedLogin ? (row?.authenticated ? '正在读取本场资格和领取结果。' : '点击“使用所选账号”即可连接活动。') : '在账号管理中添加一个登录账号。';
  } else {
    $('claim-label').textContent=state.state_label || '待查询';
    $('claim-reason').textContent=state.prize ? '已领取的奖品可在下方查看。' : state.reason || '点击刷新，获取本场领取资格。';
    $('opening-note').textContent=`每周三 ${String(state.start_hour || 8).padStart(2,'0')}:00 开放`;
    if (activeStates.has(job?.state)) {
      $('countdown').textContent=jobNames[job.state];
      $('claim-reason').textContent='定时任务正在处理，请在任务管理中查看进度。';
    } else updateClock();
  }
  renderControls(); renderSchedule();
}
function updateClock() {
  if (!state?.authenticated) return;
  const now = anchor ? anchor.time + performance.now() - anchor.mark : Date.now();
  if (anchor) $('server-time').textContent='北京时间 ' + new Date(now).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  if (activeStates.has(jobs[selectedId]?.state)) return;
  if (state.prize) $('countdown').textContent='本场已有领取结果';
  else if (state.can_claim) $('countdown').textContent='本场可以领取';
  else {
    const seconds = Math.floor((Date.parse(state.next_start) - now) / 1000);
    if (!Number.isFinite(seconds) || seconds < 0) { $('countdown').textContent=state.state_label || '等待查询本场资格'; return; }
    const days=Math.floor(seconds/86400), hours=String(Math.floor(seconds%86400/3600)).padStart(2,'0'), minutes=String(Math.floor(seconds%3600/60)).padStart(2,'0'), rest=String(seconds%60).padStart(2,'0');
    $('countdown').textContent=`距下次开放 ${days ? days+'天 ' : ''}${hours}:${minutes}:${rest}`;
  }
}
function renderSchedule() {
  const job=jobs[selectedId], upcoming=job?.enabled && job.next_run_at;
  const live=job && activeStates.has(job.state);
  const total=Object.values(jobs).filter(j=>j.enabled || activeStates.has(j.state)).length;
  $('task-count').textContent=total; $('task-count').hidden=!total;
  $('scheduled-plan').hidden=!upcoming && !live; $('unscheduled-plan').hidden=!!upcoming || live;
  $('schedule-frequency').textContent=upcoming ? (job.weekly ? '每周三重复' : '单次预约') : live ? '执行中' : '未设置';
  if (upcoming || live) {
    const date=new Date(job.next_run_at || job.run_at);
    $('next-date').textContent=date.toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',weekday:'short'});
    $('next-time').textContent=date.toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,hour:'2-digit',minute:'2-digit'});
    $('schedule-state').textContent=(live ? `${jobNames[job.state]} · ${job.attempts}/${job.max_attempts} 轮` : '已预约 · ' + (job.state==='waiting' ? '等待执行' : '上场'+(jobNames[job.state] || '已结束')));
  }
  const action=$('schedule-action');
  action.href=base+'/static/tasks.html?'+(job?.enabled ? (live ? 'detail=' : 'edit=') : 'create=1&account_id=')+encodeURIComponent(selectedId || '');
  action.replaceChildren(document.createTextNode(job?.enabled ? (live ? '查看执行进度' : '调整预约') : '设置预约'),node('span','', '→'));
}
function renderCoupons(coupons) {
  $('pool-details').hidden=!coupons?.length;
  $('coupons').replaceChildren(...(coupons || []).map(coupon=>{
    const card=node('article','pool-item');
    const title=String(coupon.name || coupon.title || '充值券');
    card.append(node('strong','',title),node('small','',coupon.subTitle || coupon.title || '以官方规则为准')); return card;
  }));
}
function prizeTitle(prize) {
  const name=prize.prizeName || prize.name || '充值券', short=prize.shortName || '';
  return short && !name.includes(short) ? short+' '+name : name;
}
function renderPrizes(prizes) {
  const list=$('prize-list'); list.replaceChildren();
  if (!prizes?.length) {
    const empty=node('div','prize-empty');
    empty.append(node('p','',prizes ? '这个账号暂无奖品记录。' : linked()?.authenticated ? '正在读取奖品记录…' : selectedLogin ? '连接账号后，查询已领取的奖品。' : '选择账号后，查看已领取的奖品。')); list.append(empty);
    $('prize-count').textContent=''; return;
  }
  $('prize-count').textContent=prizes.length+' 件';
  for (const prize of prizes) {
    const item=node('article','prize-row'),info=node('div','prize-info');
    info.append(node('strong','',prizeTitle(prize)),node('p','',[prize.subTitle,prize.getTime && '领取于 '+prize.getTime].filter(Boolean).join(' · ')));
    item.append(node('span','prize-mark','券'),info,node('span','prize-state'+(prize.sendStatus==='EXPIRED' ? ' expired' : ''),prizeNames[prize.sendStatus] || '已记录')); list.append(item);
  }
}
async function readLocal() {
  if (localPending) return localPending;
  localPending=(async()=>{
    const [directory,pool]=await Promise.all([api('/api/accounts'),unified ? api('/api/logins') : Promise.resolve(null)]);
    const oldId=selectedId, oldAuth=linked()?.authenticated, oldJob=jobs[selectedId]?.state;
    accounts=directory.accounts; jobs=directory.jobs;
    logins=unified ? pool.accounts : accounts.map(a=>({id:a.id,name:a.name,phone_masked:a.phone}));
    const remembered=selectedLogin || localStorage.getItem('recharge-login') || accounts.find(a=>a.id===localStorage.getItem('recharge-account'))?.login_id;
    const choice=logins.some(a=>a.id===remembered) ? remembered : logins[0]?.id || '';
    const options=logins.map(a=>{ const option=node('option','',a.name ? a.name+(a.phone_masked ? ' · '+a.phone_masked : '') : a.phone_masked); option.value=a.id; return option; });
    if (!options.length) { const option=node('option','','暂无账号，请先添加'); option.value=''; options.push(option); }
    $('activity-login').replaceChildren(...options); $('activity-login').value=choice;
    if (choice!==selectedLogin) { selectedLogin=choice; selectionVersion++; clearView(); }
    selectedId=linked()?.id || null;
    if (oldId!==selectedId) { selectionVersion++; clearView(); }
    if (directory.warning) notice(directory.warning);
    renderState();
    if (initialized && !busy && !syncing && linked()?.authenticated && (!oldAuth || oldId!==selectedId || oldJob!=='success' && jobs[selectedId]?.state==='success')) syncCurrent().catch(error=>notice(messageFor(error)));
  })();
  try { return await localPending; } finally { localPending=null; }
}
async function loadPrizes(id=selectedId, version=selectionVersion) {
  const result=await api('/api/prizes',undefined,id);
  if (id===selectedId && version===selectionVersion) renderPrizes(result.prizes);
}
async function syncCurrent() {
  const row=linked(); if (!row?.authenticated || syncing || !selectedId) return;
  if (activeStates.has(jobs[selectedId]?.state)) { state={...row,...state}; renderState(); return; }
  const id=selectedId, version=selectionVersion; syncing=true; renderControls();
  try {
    const result=await api('/api/refresh',{},id);
    if (id!==selectedId || version!==selectionVersion) return;
    state=result; anchor=result.server_time ? {time:Date.parse(result.server_time),mark:performance.now()} : null;
    notice(); renderState(); renderCoupons(result.coupons);
    await loadPrizes(id,version);
  } catch (error) {
    if (id===selectedId && version===selectionVersion) {
      if (error.status===401) { state={authenticated:false}; if (linked()) linked().authenticated=false; }
      else if (state) state.can_claim=false;
      renderState(); notice(messageFor(error));
    }
  } finally { syncing=false; renderControls(); }
}
async function action(button, callback) {
  if (busy || syncing) return;
  busy=true; const text=button.textContent; button.textContent='处理中…'; renderControls();
  try { await callback(); } catch (error) { notice(messageFor(error)); toast(messageFor(error),true); }
  finally { busy=false; button.textContent=text; renderState(); }
}
function installStandaloneLogin() {
  if (unified) return;
  document.querySelector('.account-manager').hidden=true;
  const host=$('standalone-login'); host.hidden=false;
  // The standalone server keeps its own SMS fallback; mounted pages use only
  // the shared account manager and never show a second login form.
  const form=node('form'); form.id='app-sms-form';
  function field(text,id,max) { const label=node('label','',text), input=node('input'); input.id=id; input.required=true; input.inputMode='numeric'; input.maxLength=max; label.append(input); form.append(label); return input; }
  const phone=field('手机号','app-phone',11), code=field('验证码','app-code',6);
  const sms=node('button','secondary-button','获取验证码'); sms.type='button';
  let readyAt=0, sentPhone='';
  sms.addEventListener('click',()=>action(sms,async()=>{
    if (!/^1[3-9]\d{9}$/.test(phone.value)) throw new Error('请输入正确手机号。');
    readyAt=Date.now()+60000; const result=await api('/api/login/sms',{phone:phone.value}); sentPhone=phone.value; readyAt=Date.now()+result.retry_after*1000; toast('验证码已发送');
  }));
  const submit=node('button','primary-button','登录'); submit.type='submit';
  form.addEventListener('submit',event=>{event.preventDefault();action(submit,async()=>{
    if (phone.value!==sentPhone) throw new Error('请先获取验证码。');
    await api('/api/login',{phone:phone.value,code:code.value}); code.value=''; await readLocal(); await syncCurrent();
  });});
  form.append(sms,submit); host.append(form);
  setInterval(()=>{const wait=Math.max(0,Math.ceil((readyAt-Date.now())/1000));sms.disabled=busy || syncing || wait>0;sms.textContent=wait ? wait+' 秒后重发' : '获取验证码';},1000);
}
async function init() {
  // Static shell upgrades a running server's cached template, preserving all
  // live credentials and waiting jobs without a service restart.
  if (!$('activity-app')) {
    const response=await fetch(base+'/static/activity.html',{cache:'no-store'});
    if (!response.ok) throw new Error('无法加载活动页面，请刷新重试。');
    const fragment=document.createElement('template'); fragment.innerHTML=await response.text();
    document.body.replaceChildren(fragment.content);
  }
  document.title='周三充值日';
  document.querySelectorAll('[data-task-link]').forEach(link=>link.href=base+'/static/tasks.html');
  installStandaloneLogin();
  $('activity-login').addEventListener('change',async()=>{
    selectedLogin=$('activity-login').value; selectedId=linked()?.id || null; selectionVersion++;
    localStorage.setItem('recharge-login',selectedLogin); if (selectedId) localStorage.setItem('recharge-account',selectedId);
    notice(); clearView(); renderState(); await syncCurrent();
  });
  $('current-app-login').addEventListener('click',()=>action($('current-app-login'),async()=>{
    const slot=linked() || accounts.find(a=>a.mode==='none' && !a.login_id);
    await api('/api/login/attach',{login_id:selectedLogin,...(slot ? {account_id:slot.id} : {})});
    await readLocal(); await syncCurrent(); toast('已连接所选账号');
  }));
  $('refresh-button').addEventListener('click',async()=>{try{await readLocal();await syncCurrent();}catch(error){notice(messageFor(error));}});
  $('prizes-button').addEventListener('click',()=>action($('prizes-button'),()=>loadPrizes()));
  $('claim-button').addEventListener('click',()=>action($('claim-button'),async()=>{
    let result;
    try { result=await api('/api/claim',{}); }
    catch (error) { if (state) state.can_claim=false; throw new Error('领取结果暂未确认，请刷新并核对奖品。'+messageFor(error)); }
    toast(result.message || '领取请求已完成'); await syncCurrent();
  }));
  $('remove-account-button').addEventListener('click',()=>{
    if (!confirm('移出此活动账号也会移除它的定时任务。账号管理中的登录仍会保留。')) return;
    action($('remove-account-button'),async()=>{await api('/api/accounts/remove',{});await readLocal();toast('已移出本活动');});
  });
  window.addEventListener('accounts-changed',()=>readLocal().catch(error=>notice(messageFor(error))));
  window.addEventListener('focus',()=>readLocal().catch(error=>notice(messageFor(error))));
  window.addEventListener('pagehide',()=>clearTimeout(pollTimer));
  window.addEventListener('pageshow',event=>{if(event.persisted) poll();});
  document.addEventListener('visibilitychange',()=>{clearTimeout(pollTimer);if(!document.hidden) poll();});
  async function poll() {
    try { await readLocal(); } catch (error) { notice(messageFor(error)); }
    if (!document.hidden) pollTimer=setTimeout(poll,activeStates.has(jobs[selectedId]?.state) ? 1500 : 6000);
  }
  try { await readLocal(); } catch(error) { notice(messageFor(error)); }
  initialized=true; await syncCurrent();
  pollTimer=setTimeout(poll,6000); setInterval(updateClock,1000);
}
init().catch(error=>{
  if ($('page-notice')) notice(messageFor(error));
  else { const errorNode=node('p','page-notice',messageFor(error));document.body.replaceChildren(errorNode); }
});
