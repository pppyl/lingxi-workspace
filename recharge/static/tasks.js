'use strict';
import * as M from './task-model.js';
(() => {
  const $ = id => document.getElementById(id);
  // This page is served as a static asset, so it can be added without restarting
  // a service that already holds account sessions and waiting tasks in memory.
  const apiBase = new URL('../', location.href).pathname.replace(/\/$/, '');
  let items = [], accounts = [], shown = [], filter = 'all', selected = new Set(), online = false;
  let timer, toastTimer, loading = false, loaded = false, mutation = false;
  let csrf = '', detailId = null, editingId = null, stoppingIds = [];
  let lastVersion = '';
  let queryApplied = false;
  const timeFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', hour12:false,
    year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'});
  function time(value) {
    return value && Number.isFinite(Date.parse(value)) ? timeFormat.format(new Date(value)) : '—';
  }
  function el(tag, className, text) {
    const node = document.createElement(tag); if (className) node.className = className;
    if (text !== undefined) node.textContent = text; return node;
  }
  function notice(text) {
    $('toast').textContent = text; $('toast').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4500);
  }
  async function api(path, body) {
    const response = await fetch(apiBase + path, {cache:'no-store', signal:AbortSignal.timeout(12000),
      ...(body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf}, body:JSON.stringify(body)})});
    let data;
    try { data = await response.json(); } catch { throw new Error('本地服务响应异常，请检查服务后刷新。'); }
    if (!response.ok) { if (data.code === 'CSRF_REJECTED') csrf = ''; throw new Error(data.error || '操作失败，请重试。'); }
    return data;
  }
  async function ensureCsrf() {
    if (csrf) return;
    const response = await fetch(apiBase + '/', {cache:'no-store', signal:AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('无法读取页面验证信息，请刷新后重试。');
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    csrf = doc.querySelector('meta[name="csrf-token"]')?.content || '';
    if (!csrf) throw new Error('页面验证信息缺失，请刷新后重试。');
  }
  function setOnline(value, error) {
    online = value;
    $('sync-status').textContent = value ? '已连接 · 北京时间' : '本地服务未连接';
    $('sync-status').classList.toggle('offline', !value);
    $('connection-error').hidden = value;
    $('connection-error').textContent = value ? '' : `${error || '读取失败'} ${loaded ? '以下保留上次同步结果。' : ''}将自动重试，也可点击“刷新列表”。`;
  }
  function updateSelection() {
    const eligible = shown.filter(row => M.stoppable(row.job));
    $('select-visible').checked = !!eligible.length && eligible.every(row => selected.has(row.id));
    $('select-visible').indeterminate = ! $('select-visible').checked && eligible.some(row => selected.has(row.id));
    $('select-visible').disabled = !eligible.length || mutation || !online;
    $('selection-count').textContent = `已选 ${selected.size} 项`;
    $('stop-selected').disabled = !selected.size || mutation || !online;
    document.querySelectorAll('#task-rows tr').forEach(tr => {
      tr.classList.toggle('selected', selected.has(tr.dataset.id));
      const check = tr.querySelector('input[type=checkbox]'); if (check) check.checked = selected.has(tr.dataset.id);
    });
  }
  function rowButton(text, handler, disabled, className) {
    const button = el('button', className, text); button.type = 'button'; button.disabled = disabled;
    button.addEventListener('click', handler); return button;
  }
  function taskRow(row) {
    const {id, account, job} = row, tr = el('tr'); tr.dataset.id = id;
    const checkCell = el('td'), check = el('input'); check.type = 'checkbox';
    check.disabled = !M.stoppable(job) || mutation || !online;
    check.setAttribute('aria-label', `选择 ${account.name} 的任务`);
    check.addEventListener('change', () => { check.checked ? selected.add(id) : selected.delete(id); updateSelection(); }); checkCell.append(check);
    const identity = el('td'); identity.append(el('strong', '', account.name || '未命名账号'), el('small', 'phone', account.phone || '手机号未载入'));
    identity.append(el('small', account.authenticated ? 'cell-sub' : 'auth-missing', account.authenticated ? '● 已登录' : row.status));
    const status = el('td'); status.append(el('span', `status-badge ${row.group}`, row.status));
    if (job.enabled && !M.active.has(job.state) && job.state !== 'waiting') status.append(el('small', '', '上场：' + (M.labels[job.state] || job.state)));
    else status.append(el('small', '', job.weekly ? (job.enabled ? '每周三重复' : '每周重复已关闭') : '单次任务'));
    const plan = el('td'); plan.dataset.label = '执行计划 · 北京时间';
    plan.append(el('div', 'time-main', time(job.enabled ? job.next_run_at || job.run_at : job.run_at)));
    plan.append(el('small', '', M.active.has(job.state) ? '本次执行计划' : job.enabled ? (job.weekly ? '下次执行 · 每周三' : '单次预约') : '原计划 · ' + (job.weekly ? '每周重复已关闭' : '单次执行')));
    const progressCell = el('td'); progressCell.dataset.label = '执行进度';
    progressCell.append(el('div', '', `${job.attempts || 0} / ${job.max_attempts} 轮`));
    const track = el('div', 'progress-track'), progress = el('progress'); progress.max = job.max_attempts || 20; progress.value = job.attempts || 0;
    progress.setAttribute('aria-label', `已尝试 ${job.attempts || 0} 轮，上限 ${job.max_attempts} 轮`); track.append(progress);
    progressCell.append(track, el('small', '', `实际提交 ${job.submissions || 0} 次`));
    const sessionMessage = !account.authenticated ? (account.error || (account.session_status === 'restoring' ? '正在读取数据库中的凭据并恢复会话' : account.session_status === 'retry' ? '会话暂时不可用，服务会自动重连' : account.session_status === 'login_required' ? '旧凭据被拒绝，请在账号管理重新短信验证' : '请到活动页选择共享账号，点击“使用所选账号”')) : '';
    const message = el('td'), text = el('p', 'last-message', sessionMessage || job.message || '等待执行'); text.title = text.textContent;
    message.append(text, el('small', '', '更新 ' + time(job.updated_at)));
    const actions = el('td'), buttons = el('div', 'row-actions');
    buttons.append(rowButton('详情', () => openDetails(id), false), rowButton(job.enabled ? '调整' : '重新预约', () => openEditor(id), mutation || !online || M.active.has(job.state)));
    if (M.stoppable(job)) buttons.append(rowButton('停止', () => openStop([id]), mutation || !online, 'stop'));
    actions.append(buttons); tr.append(checkCell, identity, status, plan, progressCell, message, actions); return tr;
  }
  function render() {
    const counts = M.counts(items);
    for (const key of Object.keys(counts)) $('count-' + key).textContent = loaded ? counts[key] : '—';
    document.querySelectorAll('[data-filter]').forEach(button => {
      const active = button.dataset.filter === filter;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
    shown = M.visible(items, filter, $('task-search').value, $('task-sort').value);
    selected = new Set([...selected].filter(id => shown.some(row => row.id === id && M.stoppable(row.job))));
    $('visible-count').textContent = loaded ? `${shown.length} / ${items.length}` : '尚未同步';
    $('task-rows').replaceChildren(...shown.map(taskRow)); updateSelection();
    $('empty-state').hidden = !loaded || !!shown.length; $('loading-state').hidden = loaded || !online;
    $('empty-title').textContent = items.length ? '没有符合条件的任务' : '还没有提交的任务';
    $('empty-message').textContent = items.length ? '试试其他状态，或清除搜索条件。' : '点击“新建预约”，选择活动账号和执行时间。';
    $('empty-create').hidden = !!items.length;
    const upcoming = items.filter(row => row.job.enabled && row.job.next_run_at && !M.active.has(row.job.state));
    const next = upcoming.sort((a,b) => Date.parse(a.job.next_run_at) - Date.parse(b.job.next_run_at))[0];
    $('next-time').textContent = !loaded ? '暂未获取预约信息' : next ? time(next.job.next_run_at) : '暂无等待执行的预约';
    if (next) {
      const sameTime = upcoming.filter(row => row.job.next_run_at === next.job.next_run_at).length;
      $('next-description').textContent = `${next.account.name}${sameTime > 1 ? ` 等 ${sameTime} 个账号` : ''} · ${next.job.weekly ? '每周三重复' : '单次任务'}${!next.account.authenticated ? ' · ' + next.status : ' · 任务已保存，等待本地服务执行'}`;
    } else $('next-description').textContent = '可以查看已有执行记录，或前往活动页新建预约。';
    if ($('detail-dialog').open) renderDetails();
  }
  function renderDetails() {
    const row = items.find(r => r.id === detailId), host = $('detail-content');
    if (!row) { host.replaceChildren(el('p', 'history-empty', '此任务已移除，请刷新列表。')); return; }
    const {job, account} = row;
    $('detail-account').textContent = [account.name, account.phone].filter(Boolean).join(' · ');
    const status = el('div', 'detail-status'); status.append(el('span', `status-badge ${row.group}`, row.status), el('p', '', job.message));
    const grid = el('dl', 'detail-grid');
    const fields = [['下次执行（北京时间）', job.enabled ? time(job.next_run_at) : '无后续预约'],
      ['执行方式', (job.weekly ? '每周三重复' : job.immediate ? '手动立即执行' : '单次预约') + (!job.enabled && job.weekly ? '（已停止）' : '')],
      ['本次计划时间', time(job.run_at)], ['最近完成时间', time(job.finished_at)],
      ['最近执行状态', M.labels[job.state] || job.state], ['账号登录', account.authenticated ? '已登录' : '待恢复登录，请在执行前到活动页连接共享账号'],
      ['尝试 / 实际提交', `${job.attempts || 0} / ${job.max_attempts} 轮 · 提交 ${job.submissions || 0} 次`],
      ['重试间隔', `${job.interval_min}～${job.interval_max} 秒（请求结束后）`]];
    fields.forEach(([label,value]) => { const pair = el('div'); pair.append(el('dt','',label),el('dd','',value)); grid.append(pair); });
    const title = el('div', 'history-title'); title.append(el('h3', '', '执行记录'), el('span', '', '最新在前 · 最多保留 45 条'));
    const previousScroll = host.querySelector('.history-list')?.scrollTop || 0;
    const history = el('ol', 'history-list');
    for (const record of [...(job.history || [])].reverse()) {
      const item = el('li'), stamp = el('time', '', time(record.time)); stamp.dateTime = record.time;
      item.append(stamp, el('p', '', `${record.attempt ? `第 ${record.attempt} 轮 · ` : ''}${record.message}`)); history.append(item);
    }
    const log = history.children.length ? history : el('p', 'history-empty', job.enabled ? '任务已保存，开始准备后会在这里显示执行记录。' : '此任务尚无执行记录。');
    const result = job.result?.message ? el('p', 'notice', '执行结果：' + job.result.message) : null;
    host.replaceChildren(status, grid, ...(result ? [result] : []), title, log); history.scrollTop = previousScroll;
  }
  function openDetails(id) { detailId = id; renderDetails(); $('detail-dialog').showModal(); }
  function openEditor(id) {
    const row = items.find(r => r.id === id); if (!row || M.active.has(row.job.state)) return;
    editingId = id;
    $('create-account-field').hidden = true;
    $('replace-notice').textContent = '保存后替换此账号的原任务，执行计数和原记录会重置。';
    $('save-task').textContent = '保存预约';
    $('edit-dialog').querySelector('.retry-settings').open = false;
    $('edit-title').textContent = row.job.enabled ? '调整预约' : '重新预约';
    $('edit-account').textContent = [row.account.name, row.account.phone].filter(Boolean).join(' · ');
    const target = row.job.next_run_at || row.job.run_at;
    $('edit-run-at').value = Date.parse(target) > Date.now() ? M.beijingInput(target) : M.nextWednesday(Date.now(), target);
    $('edit-run-at').min = M.beijingInput(Date.now());
    $('edit-min').value = row.job.interval_min; $('edit-max').value = row.job.interval_max;
    $('edit-attempts').value = row.job.max_attempts; $('edit-weekly').checked = row.job.weekly;
    $('edit-error').hidden = true; $('edit-dialog').showModal();
  }
  function openCreate(preferred = '') {
    if (!loaded || !online) { notice('请先等待任务列表连接成功。'); return; }
    editingId = null; $('create-account-field').hidden = false;
    $('edit-title').textContent = '新建预约'; $('edit-account').textContent = '选择账号和时间，其他设置按需调整。';
    $('replace-notice').textContent = '若所选账号已有任务，保存后会替换原预约并重置计数和记录。';
    const rows = accounts.map(account => {
      const item = el('label','create-account-row'), check = el('input'); check.type='checkbox'; check.value=account.id;
      check.disabled = !account.authenticated && !account.login_id || M.active.has(items.find(row=>row.id===account.id)?.job.state);
      check.dataset.blocked=String(check.disabled);
      check.checked = !check.disabled && account.id===preferred;
      check.addEventListener('change',updateCreateSelection);
      const info=el('span'); info.append(el('strong','',account.name),el('small','',account.phone || '请先在活动页连接账号'));
      item.append(check,info,el('small','',check.disabled ? '暂不可选' : items.some(row=>row.id===account.id && row.job.enabled) ? '将替换已有预约' : '可预约')); return item;
    });
    $('create-account-list').replaceChildren(...rows);
    const job=items.find(row=>row.id===preferred)?.job;
    $('edit-run-at').value = Date.parse(job?.next_run_at)>Date.now() ? M.beijingInput(job.next_run_at) : M.nextWednesday();
    $('edit-run-at').min=M.beijingInput(Date.now());
    $('edit-min').value=job?.interval_min || 0.2; $('edit-max').value=job?.interval_max || 1;
    $('edit-attempts').value=job?.max_attempts || 20; $('edit-weekly').checked=job ? job.weekly : true;
    $('edit-error').hidden=true; $('save-task').textContent='保存预约';
    $('edit-dialog').querySelector('.retry-settings').open=false;
    updateCreateSelection(); $('edit-dialog').showModal();
  }
  function updateCreateSelection() {
    const checks=[...$('create-account-list').querySelectorAll('input:not(:disabled)')];
    $('create-all').checked=!!checks.length && checks.every(check=>check.checked);
    $('create-all').indeterminate=!$('create-all').checked && checks.some(check=>check.checked);
    $('create-all').disabled=!checks.length;
  }
  function settings() {
    const ids=editingId ? [editingId] : [...$('create-account-list').querySelectorAll('input:checked')].map(input=>input.value);
    return {account_ids:ids,run_at:$('edit-run-at').value,weekly:$('edit-weekly').checked,
      interval_min:Number($('edit-min').value),interval_max:Number($('edit-max').value),max_attempts:Number($('edit-attempts').value)};
  }
  function openStop(ids) {
    stoppingIds = ids.filter(id => items.some(row => row.id === id && M.stoppable(row.job)));
    if (!stoppingIds.length) return;
    const names = stoppingIds.map(id => items.find(row => row.id === id).account.name);
    $('stop-title').textContent = `停止 ${stoppingIds.length} 个任务`;
    $('stop-description').textContent = `将停止：${names.join('、')}`;
    $('stop-error').hidden = true; $('stop-dialog').showModal();
  }
  async function mutate(dialog, errorId, request) {
    if (mutation) return; mutation = true; $(errorId).hidden = true;
    $(dialog).querySelectorAll('button,input').forEach(node => node.disabled = true);
    try { await ensureCsrf(); const result = await request(); $(dialog).close(); selected.clear(); notice(result.message || '已保存'); await refresh(); }
    catch (error) {
      $(errorId).textContent = ['TypeError', 'TimeoutError'].includes(error.name) ? '未能确认操作结果，请刷新任务列表核对后再重试。' : error.message;
      $(errorId).hidden = false;
    }
    finally {
      mutation = false; $(dialog).querySelectorAll('button,input').forEach(node => node.disabled = node.dataset.blocked === 'true');
      if (dialog==='edit-dialog' && !editingId) updateCreateSelection(); render();
    }
  }
  $('edit-form').addEventListener('submit', event => {
    event.preventDefault();
    const value = settings();
    const error = !value.account_ids.length ? '请至少选择一个已连接的活动账号。' : M.validate(value);
    if (error) { $('edit-error').textContent = error; $('edit-error').hidden = false; return; }
    mutate('edit-dialog', 'edit-error', () => api('/api/schedules', value));
  });
  $('new-task-button').addEventListener('click',()=>openCreate());
  $('empty-create').addEventListener('click',()=>openCreate());
  $('create-all').addEventListener('change',event=>{
    $('create-account-list').querySelectorAll('input:not(:disabled)').forEach(input=>input.checked=event.target.checked); updateCreateSelection();
  });
  $('run-now-task').addEventListener('click',()=>{
    const value=settings();
    const error=!value.account_ids.length ? '请至少选择一个已连接的活动账号。' : M.validate({...value,run_at:M.nextWednesday()});
    if (error) { $('edit-error').textContent=error; $('edit-error').hidden=false; return; }
    if (!confirm(`立即为 ${value.account_ids.length} 个账号执行领取？每账号最多尝试 ${value.max_attempts} 轮，成功即停。`)) return;
    mutate('edit-dialog','edit-error',()=>api('/api/schedules',{...value,immediate:true,weekly:false}));
  });
  $('stop-form').addEventListener('submit', event => { event.preventDefault(); mutate('stop-dialog', 'stop-error', () => api('/api/schedules/cancel', {account_ids:stoppingIds})); });
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('cancel', event => { if (mutation) event.preventDefault(); }));
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => { filter = button.dataset.filter; selected.clear(); render(); }));
  $('task-search').addEventListener('input', () => { selected.clear(); render(); });
  $('task-sort').addEventListener('change', render);
  $('select-visible').addEventListener('change', event => { selected = new Set(event.target.checked ? shown.filter(row => M.stoppable(row.job)).map(row => row.id) : []); updateSelection(); });
  $('stop-selected').addEventListener('click', () => openStop([...selected]));
  async function refresh() {
    if (loading) return; loading = true; clearTimeout(timer); $('refresh-tasks').disabled = true;
    try {
      const data = await api('/api/accounts'); accounts=data.accounts; items = M.rows(data); loaded = true; setOnline(true);
      $('storage-warning').hidden = !data.warning; $('storage-warning').textContent = data.warning || '';
      $('last-sync').textContent = '最近同步 ' + time(new Date().toISOString());
      // Preserve focused row buttons and checkboxes when a poll has no changes.
      const version = JSON.stringify([data, mutation]);
      if (version !== lastVersion) { render(); lastVersion = version; }
      else updateSelection();
      if (!queryApplied) {
        queryApplied=true;
        const params=new URLSearchParams(location.search), edit=params.get('edit'), detail=params.get('detail');
        if (params.has('create')) openCreate(params.get('account_id') || '');
        else if (edit && items.some(row=>row.id===edit)) {
          if (M.active.has(items.find(row=>row.id===edit).job.state)) openDetails(edit); else openEditor(edit);
        } else if (detail && items.some(row=>row.id===detail)) openDetails(detail);
      }
    } catch (error) {
      setOnline(false, error.name === 'TimeoutError' ? '连接本地服务超时。' : error.name === 'TypeError' ? '连接不到本地服务，请确认程序仍在运行。' : error.message);
      lastVersion = ''; render();
    }
    finally {
      loading = false; $('refresh-tasks').disabled = false;
      if (!document.hidden) timer = setTimeout(refresh, online && items.some(row => M.active.has(row.job.state)) ? 1000 : online ? 4000 : 8000);
    }
  }
  $('refresh-tasks').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden) refresh(); });
  window.addEventListener('pageshow', refresh);
  window.addEventListener('pagehide', () => clearTimeout(timer));
  refresh();
})();
