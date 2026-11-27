(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const endpoint = '/api/sunday-drama';
  const clock = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  let model = {accounts:[],settings:{},campaign:{}}, selected = '', loading = false, pendingLoad = false;
  let rendered = '', accountSignature = '', confirmation = null, inviteOwner = '';
  const posting = new Set();
  const node = (tag, className='', text) => {
    const element = document.createElement(tag); element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const current = () => model.accounts.find(a => a.id === selected);
  const label = a => a ? (a.name && !a.phone_masked.endsWith(a.name) ? `${a.name} · ${a.phone_masked}` : a.phone_masked) : '请选择账号';
  const date = value => value ? clock.format(new Date(value)) : '—';
  const sunday = () => new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Shanghai',weekday:'short'}).format(new Date(model.server_time)) === 'Sun';
  function feedback(message, error=false) {
    $('drama-feedback').textContent = message; $('drama-feedback').hidden = !message;
    $('drama-feedback').classList.toggle('error', error);
  }
  async function api(path, payload) {
    const r = await fetch(endpoint+path, payload === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const value = await r.json();
    if (!r.ok) throw new Error(value.error || '操作未完成，请稍后再试。');
    return value;
  }
  function action(text, kind, extra={}, disabled=false) {
    const button = node('button','secondary',text); button.type = 'button';
    button.disabled = disabled || !model.campaign.active || current()?.status !== 'active' || current()?.running || current()?.pending || posting.has(selected);
    button.addEventListener('click', () => run(kind,extra));
    return button;
  }
  function select(id) {
    if (selected !== id) {
      selected = id; rendered = ''; $('drama-completed').open = false; feedback('');
      for (const name of ['drama-share','drama-confirm']) $(name).close();
      inviteOwner = ''; confirmation = null;
    }
    render();
    refresh(id);
  }
  function renderAccounts() {
    const signature = JSON.stringify(model.accounts.map(a => [a.id,a.name,a.phone_masked,a.status,a.running,selected===a.id]));
    if (signature === accountSignature) return;
    accountSignature = signature;
    $('drama-account-count').textContent = model.accounts.length;
    $('drama-accounts').replaceChildren(...model.accounts.map(a => {
      const button = node('button','drama-account'); button.type='button'; button.setAttribute('aria-pressed',String(a.id===selected));
      button.append(node('strong','',a.name || a.phone_masked.slice(-4)),node('em','',a.status==='active' ? (a.running?'执行中':'已登录'):'需登录'),node('small','',a.phone_masked));
      button.addEventListener('click',()=>select(a.id)); return button;
    }));
    $('drama-account-select').replaceChildren(...model.accounts.map(a => {
      const option = node('option','',label(a)+(a.status==='active'?'':' · 需登录')); option.value=a.id; return option;
    }));
    $('drama-account-select').value=selected;
  }
  function renderObservation(account) {
    const observation = account?.observation;
    const key = `${selected}:${observation?.queried_at}:${account?.status}:${account?.running}:${account?.pending}:${model.campaign.active}:${posting.has(selected)}`;
    if (key === rendered) return;
    rendered = key;
    $('drama-remain').textContent = observation?.remaining.remain ?? '—';
    $('drama-video-day').textContent = observation ? `${observation.video.doneDayCount} / ${observation.video.canDayCount}` : '—';
    $('drama-video-week').textContent = observation ? `${observation.video.doneWeekCount} / ${observation.video.canWeekCount}` : '—';
    const tasks = observation?.tasks || [];
    const completed = tasks.filter(task=>task.taskStage==='DONE');
    const pending = tasks.filter(task=>task.taskStage!=='DONE');
    $('drama-task-count').textContent = tasks.length ? `${completed.length} / ${tasks.length}` : '—';
    $('drama-tasks').replaceChildren(...pending.map(task => {
      const item = node('li','drama-task');
      const copy = node('div'), actions = node('div','drama-task-actions');
      copy.append(node('h3','',task.name));
      const note = task.reason || task.description;
      if (note && note!==task.name) copy.append(node('p','drama-muted',note));
      if (task.earnOpportunity!=null) actions.append(node('span','drama-task-reward',`+${task.earnOpportunity} 次`));
      const ready = task.taskStage==='WAIT_RECEIVE';
      const restricted = task.qualification==='ineligible', waiting = task.qualification==='waiting';
      if (restricted || waiting || (!ready && !task.automatic)) actions.append(node('span','drama-state',restricted?'当前不符资格':waiting?'等待处理':'尚未接通'));
      if (ready || task.automatic) actions.append(action(ready?'领取机会':restricted||waiting?'重新核对':'去完成','task',{task_id:task.taskId}));
      item.append(copy,actions);
      return item;
    }));
    if (!tasks.length) {
      $('drama-tasks').append(node('li','drama-empty',observation?'本周暂无任务。':account?.status==='active'?'刷新进度后可查看任务。':'请先在账号管理登录。'));
    }
    $('drama-completed').hidden = !completed.length;
    $('drama-completed-label').textContent = completed.length===tasks.length ? '本周任务已完成' : '已完成任务';
    $('drama-completed-tasks').replaceChildren(...completed.map(task=>{
      const item = node('li','drama-task drama-task-done');
      item.append(node('h3','',task.name));
      if (task.earnOpportunity!=null) item.append(node('span','drama-task-reward',`+${task.earnOpportunity} 次`));
      return item;
    }));
    const invite = observation?.invite;
    $('drama-invite-count').textContent = invite?.count ?? '—';
    const claimed = invite?.prizes.some(p=>['USED','UNUSED'].includes(p.useStatus));
    $('drama-invite-rewards').replaceChildren(...(invite?.prizes || []).map(prize => {
      const row = node('li'), info=node('div','',prize.name), target=[3,5,7][Number(prize.stage)-1];
      const received = ['USED','UNUSED'].includes(prize.useStatus);
      const eligible = !claimed && invite.available && invite.count>=target && prize.useStatus==='WITHOUT';
      info.append(node('small','',`累计邀请 ${target} 人`));
      row.append(info);
      if (!eligible) {
        const status = received?'已领取':claimed?'已领取其他档位':prize.useStatus==='NOSTOCK'?'暂无库存':invite.count<target?`还差 ${target-invite.count} 人`:'暂不可领取';
        row.append(node('span','drama-state',status));
        return row;
      }
      const button=node('button','secondary','领取'); button.type='button';
      button.disabled=!model.campaign.active || account.status!=='active' || account.running || account.pending || posting.has(selected);
      button.addEventListener('click',()=>{
        confirmation={id:selected,kind:'invite_claim',prize_id:String(prize.id),confirmed:true};
        $('drama-confirm-text').textContent=`${label(account)} 将领取「${prize.name}」。`;
        $('drama-confirm').showModal();
      });
      row.append(button); return row;
    }));
    const prizes=observation?.prizes || [];
    $('drama-prizes').replaceChildren(...(prizes.length?prizes.map(prize=>{
      const item=node('li','drama-prize',prize.prizeName || prize.name || '官方未提供奖品名称');
      item.append(node('small','',date(prize.winAt || prize.winTime || prize.createTime))); return item;
    }):[node('li','drama-empty','暂无中奖记录。')]));
    $('drama-sync').textContent = observation ? `最近核对 ${date(observation.queried_at)} · 北京时间` : '';
    $('drama-sync').hidden = !observation;
  }
  function render() {
    renderAccounts();
    const account=current(), observation=account?.observation;
    $('drama-period').textContent=model.campaign.end_at?`活动时间 ${model.campaign.start_at.slice(0,10)} 至 ${model.campaign.end_at.slice(0,10)}`:'';
    $('drama-announcement').textContent=model.campaign.announcement || ''; $('drama-announcement').hidden=!model.campaign.announcement;
    $('drama-content').setAttribute('aria-busy',String(!!(account?.refreshing || account?.running)));
    const drawDay=!!model.server_time && sunday();
    $('drama-draw-window').textContent=model.campaign.ended?'活动已结束':drawDay?'今日开放 · 周一 00:00 清零':'周日开放 · 周一 00:00 清零';
    $('drama-signin-state').textContent=model.campaign.signin_paused?'活动签到暂停':'活动签到暂不可用';
    $('drama-rules').textContent=model.campaign.rules || '暂无官方规则。';
    const blocked=!account || account.status!=='active' || !model.campaign.active || account.running || account.pending || posting.has(selected);
    $('drama-refresh').disabled=!account || account.status!=='active' || posting.has(selected);
    $('drama-run').disabled=blocked;
    $('drama-run').hidden=!!(observation?.tasks.length && observation.tasks.every(task=>task.taskStage==='DONE') && !account.running && !posting.has(selected));
    $('drama-run').textContent=account?.running?'任务执行中…':'完成可做任务';
    $('drama-draw').disabled=blocked || !drawDay || !(observation?.remaining.remain>0);
    $('drama-video').disabled=blocked || !observation || observation.video.doneDayCount>=observation.video.canDayCount;
    $('drama-video').textContent=observation?.video.canDayCount>0 && observation.video.doneDayCount>=observation.video.canDayCount?'今日已完成':'观看 5 分钟';
    $('drama-invite').disabled=!account || account.status!=='active' || !model.campaign.active || !observation?.invite.session || posting.has(selected);
    $('drama-membership').disabled=!account || account.status!=='active' || posting.has(selected+':membership');
    const membership=account?.membership;
    $('drama-membership-status').hidden=!membership;
    $('drama-membership-status').textContent=membership
      ? (membership.services.length?'':'未发现爱奇艺或随心看相关订购。 ')+membership.message
      : '';
    $('drama-membership-items').replaceChildren(...(membership?.services || []).map(item=>{
      const row=node('li','',item.name);row.append(node('small','',[item.fee,item.status].filter(Boolean).join(' · ')||'官方未提供资费或状态'));return row;
    }));
    $('drama-all').disabled=!model.campaign.active || posting.has('all');
    const latest=account?.records[0];
    const runStatus=account?.running
      ? (latest?.state==='running'?latest.message:'任务已排队或正在执行。')
      : account?.pending?'提交结果待核实，请刷新进度。':latest?.state==='error' || latest?.state==='uncertain'?latest.message:'';
    $('drama-run-status').textContent=runStatus;
    $('drama-run-status').hidden=!runStatus;
    renderObservation(account);
    const records=account?.records || [];
    $('drama-records').replaceChildren(...(records.length?records.slice(0,20).map(record=>{
      const item=node('li');item.append(node('time','',date(record.at)),node('span',record.state==='error'||record.state==='uncertain'?'drama-error':'',record.message)); return item;
    }):[node('li','','暂无执行记录。')]));
  }
  async function load() {
    if (loading) { pendingLoad=true; return; }
    loading=true;
    try {
      model=await api('/status');
      const changed=!model.accounts.some(a=>a.id===selected);
      if (changed) {
        selected=(model.accounts.find(a=>a.status==='active') || model.accounts[0])?.id || ''; rendered=''; $('drama-completed').open=false;
        confirmation=null;inviteOwner='';$('drama-confirm').close();$('drama-share').close();
      }
      render();
      if (changed && current()?.status==='active') refresh(selected);
    } catch(error) { feedback(error.message,true); }
    finally { loading=false; if(pendingLoad) { pendingLoad=false; load(); } }
  }
  async function refresh(id) {
    if (model.accounts.find(a=>a.id===id)?.status!=='active') return;
    try { await api('/run',{id,kind:'refresh'}); await load(); }
    catch(error) { if(id===selected)feedback(error.message,true); }
  }
  async function run(kind, extra={}, all=false) {
    const id=all?null:selected, key=id || 'all';
    if(posting.has(key))return;
    posting.add(key);render();
    try { const result=await api('/run',{id,kind,...extra});feedback(result.message);await load(); }
    catch(error) { feedback(error.message,true); }
    finally { posting.delete(key);rendered='';render(); }
  }
  $('drama-account-select').addEventListener('change',event=>select(event.target.value));
  $('drama-refresh').addEventListener('click',()=>refresh(selected));
  $('drama-run').addEventListener('click',()=>run('tasks'));
  $('drama-all').addEventListener('click',()=>run('tasks',{},true));
  $('drama-video').addEventListener('click',()=>run('video'));
  $('drama-draw').addEventListener('click',()=>run('draw'));
  $('drama-membership').addEventListener('click',async()=>{
    const id=selected;posting.add(id+':membership');render();
    try {
      const result=await api('/membership',{id});
      const account=model.accounts.find(a=>a.id===id);if(account)account.membership=result;
    }catch(error){feedback(error.message,true);}
    finally{posting.delete(id+':membership');render();}
  });
  $('drama-settings-open').addEventListener('click',()=>{
    $('drama-enabled').checked=model.settings.enabled;$('drama-video-enabled').checked=model.settings.video_enabled;
    $('drama-task-time').value=model.settings.task_time;$('drama-draw-time').value=model.settings.draw_time;
    $('drama-settings-error').textContent='';$('drama-settings').showModal();
  });
  $('drama-settings-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try { await api('/settings',{enabled:$('drama-enabled').checked,video_enabled:$('drama-video-enabled').checked,task_time:$('drama-task-time').value,draw_time:$('drama-draw-time').value});$('drama-settings').close();await load(); }
    catch(error) { $('drama-settings-error').textContent=error.message; }
    finally { button.disabled=false; }
  });
  $('drama-confirm-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!confirmation)return;const payload=confirmation;confirmation=null;$('drama-confirm').close();
    try { await api('/run',payload);await load(); }catch(error){feedback(error.message,true);}
  });
  $('drama-invite').addEventListener('click',async()=>{
    const id=selected;inviteOwner=id;$('drama-share-account').textContent=label(current());
    $('drama-command').value='';$('drama-invite-url').value='';$('drama-copy-url').disabled=$('drama-copy-command').disabled=true;$('drama-copy-status').textContent='正在获取官方邀请信息…';
    $('drama-poster').hidden=$('drama-poster-link').hidden=true;$('drama-share').showModal();
    try { const value=await api('/invite',{id});if(inviteOwner!==id)return;
      $('drama-command').value=typeof value.command==='string'?value.command:JSON.stringify(value.command);
      $('drama-invite-url').value=value.url || '';$('drama-copy-url').disabled=!value.url;
      $('drama-copy-command').disabled=false;$('drama-copy-status').textContent='';
      $('drama-poster').src=$('drama-poster-link').href=value.poster_url;
      $('drama-poster').hidden=$('drama-poster-link').hidden=false;
    }catch(error){if(inviteOwner===id)$('drama-copy-status').textContent=error.message;}
  });
  async function copyInvitation(id, message) {
    try {
      if(navigator.clipboard?.writeText)await navigator.clipboard.writeText($(id).value);
      else { $(id).select();if(!document.execCommand('copy'))throw new Error('请选中内容后手动复制。'); }
      $('drama-copy-status').textContent=message;
    } catch(error) { $('drama-copy-status').textContent=error.message || '复制失败，请手动复制。'; }
  }
  $('drama-copy-command').addEventListener('click',()=>copyInvitation('drama-command','邀请口令已复制。'));
  $('drama-copy-url').addEventListener('click',()=>copyInvitation('drama-invite-url','邀请链接已复制。'));
  document.querySelectorAll('[data-drama-close]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.dramaClose).close()));
  window.addEventListener('accounts-changed',async()=>{await load();if(current()?.status==='active')refresh(selected);});
  load();setInterval(()=>{if(!document.hidden)load();},2500);
})();
