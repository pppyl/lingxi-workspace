(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const dates = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const days = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'});
  const busy = new Set();
  const expandedRewards = new Set();
  let model = {accounts:[],settings:{enabled:true,start_time:'00:00:01',repeat_count:5,interval_seconds:1},campaign:{active:false,ended:false}};
  let loading = false, pendingQuery = false, loaded = false, saving = false, expiryTimer;

  function node(tag, className = '', text) {
    const element = document.createElement(tag); element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function date(value) { const stamp = Date.parse(value); return Number.isFinite(stamp) ? dates.format(stamp) : '—'; }
  function signedToday(account) {
    const stamp = Date.parse(model.server_time);
    return Number.isFinite(stamp) && account.observation?.date === days.format(stamp) ? account.observation.signed : undefined;
  }
  function badge(text, kind = '') { return node('span', 'gold-badge '+kind, text); }
  function feedback(message, success = false) {
    $('gold-feedback').textContent = message; $('gold-feedback').hidden = !message;
    $('gold-feedback').classList.toggle('success', success);
  }
  async function api(path, payload) {
    const response = await fetch(path, payload === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if (!(response.headers.get('Content-Type') || '').includes('application/json')) throw new Error(`本地服务返回异常页面（HTTP ${response.status}），请刷新或重启后重试。`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || result.message || '操作未完成，请稍后重试。');
    return result;
  }
  function prizeList(prizes, accountId) {
    if (!prizes) return node('small', '', '奖品待查询');
    if (!prizes.length) return node('small', '', '暂无已领奖品');
    if (accountId) {
      const today = days.format(Date.parse(model.server_time)), current = [], previous = [];
      for (const prize of prizes) {
        const value = (prize.received_at || '').replace(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}(?:\.\d+)?)$/, '$1T$2+08:00');
        const stamp = Date.parse(value);
        (Number.isFinite(stamp) && days.format(stamp) === today ? current : previous).push(prize);
      }
      const group = node('div', 'gold-prize-groups');
      if (current.length) group.append(prizeList(current));
      if (previous.length) {
        const history = node('details', 'gold-prize-history');
        history.open = expandedRewards.has(accountId);
        history.append(node('summary', '', `历史奖励（${previous.length}）`), prizeList(previous));
        history.addEventListener('toggle', () => {
          if (history.open) expandedRewards.add(accountId); else expandedRewards.delete(accountId);
        });
        group.append(history);
      }
      return group;
    }
    const list = node('div', 'gold-prizes');
    for (const prize of prizes) {
      const item = node('div', 'gold-prize'), meta = node('div', 'gold-prize-meta');
      item.append(node('div', 'gold-prize-name', prize.name || '奖品名称待核对'));
      if (prize.status) meta.append(node('span', '', prize.status));
      if (prize.received_at) meta.append(node('time', '', date(prize.received_at)));
      item.append(meta); list.append(item);
    }
    return list;
  }
  function showRecords(account) {
    $('gold-records-account').textContent = `${account.name || account.phone_masked} · ${account.phone_masked}`;
    const rows = (account.records || []).map(record => {
      const item = node('section', 'gold-record');
      item.append(node('time', '', date(record.at)), node('p', '', record.message || '官方未提供说明'));
      if (record.prizes?.length) item.append(prizeList(record.prizes));
      return item;
    });
    $('gold-records-list').replaceChildren(...(rows.length ? rows : [node('p','gold-record-empty','暂无执行记录。')]));
    $('gold-records').showModal();
  }
  function action(label, account, kind, disabled = false) {
    const button = node('button', kind === 'signin' ? 'secondary' : 'quiet', label); button.type = 'button';
    button.disabled = disabled || !model.campaign.active || account.status !== 'active' || account.running || busy.has(account.id);
    button.addEventListener('click', () => run(account, kind));
    return button;
  }
  function render() {
    const search = $('gold-search').value.trim().toLocaleLowerCase(), filter = $('gold-filter').value;
    const shown = model.accounts.filter(account => {
      if (!`${account.name || ''} ${account.phone_masked}`.toLocaleLowerCase().includes(search)) return false;
      const signed = signedToday(account);
      if (filter === 'pending') return account.status === 'active' && signed !== true;
      if (filter === 'signed') return signed === true;
      if (filter === 'prizes') return account.observation?.prizes?.length > 0;
      if (filter === 'attention') return account.status !== 'active' || ['failed','login_required','ineligible','rate_limited','verification_required','uncertain','unknown_result'].includes(account.last_result?.state);
      return true;
    });
    $('gold-total').textContent = model.accounts.length;
    $('gold-summary').textContent = `显示 ${shown.length} / ${model.accounts.length} 个 · 今日已打卡 ${model.accounts.filter(account => signedToday(account) === true).length} 个`;
    $('gold-auto-summary').textContent = model.settings.enabled ? `每天 ${model.settings.start_time} 起 · 最多 ${model.settings.repeat_count} 次 · 间隔 ${model.settings.interval_seconds} 秒 · 全部有效账号` : '每日自动打卡已关闭';
    $('gold-configure').disabled = !loaded || saving || model.campaign.ended;
    $('gold-settings-save').disabled = saving || model.campaign.ended;
    $('gold-refresh').disabled = loading;
    $('gold-ended').hidden = !model.campaign.ended;
    const rows = shown.map(account => {
      const row = node('tr'), identity = node('td'), signed = signedToday(account), status = node('td');
      identity.append(node('strong', '', account.name || account.phone_masked), node('small', '', account.phone_masked));
      if (account.status !== 'active') identity.append(node('small', 'gold-login-warning', '需要重新登录'));
      status.append(badge(signed === true ? '今日已打卡' : signed === false ? '今日待打卡' : '待查询', signed === true ? 'success' : signed === false ? 'pending' : ''));
      const prizes = node('td'); prizes.append(prizeList(account.observation?.prizes, account.id));
      const next = node('td'); next.append(node('time', '', model.campaign.ended ? '已停止' : model.settings.enabled ? date(account.next_run) : '未开启'));
      const recent = node('td', 'gold-recent');
      recent.append(node('p', '', account.running || busy.has(account.id) ? '正在核对并执行…' : account.last_result?.message || '尚未查询'), node('time', '', date(account.last_result?.at)));
      const operations = node('td'), buttons = node('div', 'gold-row-actions'), records = node('button', 'quiet', '记录');
      records.type = 'button'; records.addEventListener('click', () => showRecords(account));
      buttons.append(action('查询', account, 'refresh'), action('立即打卡', account, 'signin', signed === true || account.observation?.eligible === false), records);
      if (account.status !== 'active' || account.activity_login_required) {
        const login = node('button','quiet','重新登录'); login.type = 'button'; login.dataset.accountWindow = ''; login.dataset.accountRelogin = account.id; buttons.append(login);
      }
      operations.append(buttons); row.append(identity,status,prizes,next,recent,operations); return row;
    });
    if (!rows.length) {
      const row = node('tr'), cell = node('td','gold-empty',model.accounts.length ? '当前筛选下没有账号。' : '共享账号池为空，请从左侧“账号管理”登录。');
      cell.colSpan = 6; row.append(cell); rows.push(row);
    }
    $('gold-accounts').replaceChildren(...rows);
  }
  function updateCampaign() {
    clearTimeout(expiryTimer);
    $('gold-period').textContent = model.campaign.end_at ? `至 ${date(model.campaign.end_at)}` : '活动期限待官方确认';
    $('gold-warning').textContent = model.warning || ''; $('gold-warning').hidden = !model.warning;
    $('gold-rules-text').textContent = model.campaign.rules_text || ''; $('gold-rules').hidden = !model.campaign.rules_text;
    const remaining = Date.parse(model.campaign.end_at)-Date.parse(model.server_time);
    if (model.campaign.active && remaining > 0) {
      const deadline = Date.now()+remaining;
      const expire = () => {
        const delay = deadline-Date.now();
        if (delay > 0) { expiryTimer = setTimeout(expire, Math.min(delay,2147483647)); return; }
        model.campaign.active = false; model.campaign.ended = true; render();
        window.dispatchEvent(new CustomEvent('platforms-changed'));
      };
      expiryTimer = setTimeout(expire,Math.min(remaining,2147483647));
    }
  }
  async function refresh(query = false) {
    if (loading) { if (query) pendingQuery = true; return; }
    loading = true; render();
    try {
      model = await api('/api/daily-gold/status'); loaded = true;
      if (query && model.campaign.active) {
        await api('/api/daily-gold/refresh', {});
        model = await api('/api/daily-gold/status');
        feedback('');
      }
      updateCampaign(); $('gold-sync').textContent = '最近同步 '+date(model.server_time);
    } catch (error) { feedback(error.message); }
    finally {
      loading = false; render();
      if (pendingQuery) { pendingQuery = false; await refresh(true); }
    }
  }
  async function run(account, kind) {
    busy.add(account.id); render(); feedback('');
    try { const result = await api('/api/daily-gold/run', {id:account.id,kind}); feedback(result.message || '已加入执行队列。', true); }
    catch (error) { feedback(error.message); }
    finally { busy.delete(account.id); await refresh(); }
  }
  $('gold-configure').addEventListener('click', () => {
    $('gold-enabled').checked = model.settings.enabled;
    $('gold-start-time').value = model.settings.start_time;
    $('gold-repeat-count').value = model.settings.repeat_count;
    $('gold-interval').value = model.settings.interval_seconds;
    $('gold-settings-error').hidden = true; $('gold-settings').showModal();
  });
  $('gold-settings-form').addEventListener('submit', async event => {
    event.preventDefault(); if (saving || model.campaign.ended) return;
    saving = true; render(); $('gold-settings-error').hidden = true;
    try {
      const startTime = $('gold-start-time').value;
      await api('/api/daily-gold/settings', {enabled:$('gold-enabled').checked,start_time:startTime.length === 5 ? startTime+':00' : startTime,repeat_count:Number($('gold-repeat-count').value),interval_seconds:Number($('gold-interval').value)});
      $('gold-settings').close(); await refresh();
    } catch (error) { $('gold-settings-error').textContent = error.message; $('gold-settings-error').hidden = false; }
    finally { saving = false; render(); }
  });
  $('gold-settings-close').addEventListener('click', () => $('gold-settings').close());
  $('gold-settings-cancel').addEventListener('click', () => $('gold-settings').close());
  $('gold-records-close').addEventListener('click', () => $('gold-records').close());
  $('gold-refresh').addEventListener('click', () => refresh(true));
  $('gold-search').addEventListener('input', render); $('gold-filter').addEventListener('change', render);
  window.addEventListener('accounts-changed', () => refresh(true));
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
  refresh(true);
})();
