(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const dateFormatter = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
  const busy = new Set(), smsExpires = new Map();
  let model = {campaign:{active:false, ended:false}, settings:{signin_enabled:true, signin_time:'00:01', draw_enabled:true, burst_count:5, burst_interval:0.2,retry_interval_minutes:5,max_attempts:5}, accounts:[]};
  let loading = false, loaded = false, saving = false, claiming = false, sending = false;
  let claimAccount = null, claimGeneration = 0, expiryTimer, toastTimer;
  function node(tag, className = '', text) {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function date(value) {
    const timestamp = Date.parse(value);
    return Number.isFinite(timestamp) ? dateFormatter.format(timestamp) : '—';
  }
  function badge(text, kind = '') { return node('span', 'seven-badge '+kind, text); }
  function prizeName(value) { return typeof value === 'string' ? value : value?.name || value?.skuName || ''; }
  function available(account) { return account.status === 'active' && account.observation.eligible !== false; }
  async function api(path, payload) {
    const response = await fetch(path, payload === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const text = await response.text();
    let value;
    try { value = JSON.parse(text); }
    catch { throw new Error(response.ok ? '服务返回了异常页面，请刷新后重试。' : `本地服务暂未完成请求（HTTP ${response.status}），请稍后重试。`); }
    if (!response.ok) throw new Error(value.error || '操作未完成，请重试。');
    return value;
  }
  function feedback(message, success = false) {
    $('seven-feedback').textContent = message;
    $('seven-feedback').hidden = !message;
    $('seven-feedback').classList.toggle('success', success);
  }
  function toast(message) {
    clearTimeout(toastTimer);
    const element = $('seven-toast');
    element.hidden = true;
    $('seven-toast-text').textContent = message;
    requestAnimationFrame(() => { element.hidden = false; });
    toastTimer = setTimeout(() => { element.hidden = true; }, 3200);
  }
  function visibleAccounts() {
    const search = $('seven-search').value.trim().toLocaleLowerCase(), filter = $('seven-filter').value;
    return model.accounts.filter(account => {
      if (!`${account.name} ${account.phone_masked}`.toLocaleLowerCase().includes(search)) return false;
      const progress = account.observation;
      if (filter === 'signin') return available(account) && !progress.checked_today && !progress.completed && !progress.claimed;
      if (filter === 'claim') return progress.completed && !progress.claimed;
      if (filter === 'draw') return available(account) && (progress.chances === null || progress.chances === undefined || progress.chances > 0);
      if (filter === 'attention') return account.status !== 'active' || progress.eligible === false || account.retry_exhausted || progress.error || account.error;
      return true;
    });
  }
  function action(label, account, kind, disabled = false, primary = false) {
    const button = node('button', primary ? 'secondary' : 'quiet', label);
    button.type = 'button';
    button.disabled = disabled || !model.campaign.active || account.status !== 'active' ||
      (kind !== 'refresh' && account.observation.eligible === false) || account.running || busy.has(account.id);
    button.addEventListener('click', () => kind === 'claim' ? openClaim(account) : run(account, kind));
    return button;
  }
  function render() {
    const shown = visibleAccounts();
    $('seven-total').textContent = model.accounts.length;
    const signed = model.accounts.filter(account => account.observation.checked_today).length;
    const rewards = model.accounts.filter(account => account.observation.claimed).length;
    $('seven-summary').textContent = `显示 ${shown.length} / ${model.accounts.length} 个 · 今日已打卡 ${signed} 个 · 红包已领 ${rewards} 个`;
    $('seven-auto-summary').textContent = `打卡 ${model.settings.signin_enabled ? model.settings.signin_time+' 起自动检查' : '未开启'} · 夺宝 ${model.settings.draw_enabled ? '每天 00:00 自动参与' : '未开启'} · 失败最多 ${model.settings.max_attempts} 次/日，间隔 ${model.settings.retry_interval_minutes} 分钟 · 全部有效账号`;
    $('seven-configure').disabled = !loaded || saving || model.campaign.ended;
    $('seven-ended').hidden = !model.campaign.ended;
    const rows = shown.map(account => {
      const row = node('tr'), progress = account.observation;
      const identity = node('td');
      identity.append(node('strong', '', account.name || account.phone_masked), node('small', '', account.phone_masked));
      if (account.status !== 'active') identity.append(node('small', 'seven-login-warning', '需要重新登录'));
      const signin = node('td');
      if (progress.checked_today) signin.append(badge('今日已打卡', 'success'));
      else if (progress.completed || progress.claimed) signin.append(badge('本轮已完成', 'success'));
      else if (progress.eligible === false) signin.append(badge('资格未通过', 'attention'));
      else signin.append(badge(progress.checked_today === false ? '今日待打卡' : '待查询', progress.checked_today === false ? 'pending' : ''));
      const sequence = node('td'), days = node('div', 'seven-days');
      days.append(node('b', '', progress.days === null || progress.days === undefined ? '—' : progress.days), document.createTextNode(' / 7 天'));
      sequence.append(days);
      const meter = node('span', 'seven-meter');
      meter.setAttribute('aria-label', `连续打卡 ${progress.days ?? '尚未查询'} 天`);
      for (let day = 0; day < 7; day++) meter.append(node('i', day < progress.days ? 'done' : ''));
      sequence.append(meter, node('small', '', `累计 ${progress.total_amount === null || progress.total_amount === undefined ? '—' : progress.total_amount} 元`));
      const reward = node('td');
      reward.append(badge(progress.claimed ? '已领取' : progress.completed ? '待短信领取' : progress.eligible === false ? '暂不可参与' : '待满 7 天', progress.claimed ? 'success' : progress.completed || progress.eligible === false ? 'attention' : ''));
      if (progress.claimed) reward.append(node('small', '', '前往“我的卡券”使用'));
      const draw = node('td');
      draw.append(badge(progress.chances === 0 ? '今日机会已用完' : progress.eligible === false ? '暂不可参与' : progress.chances > 0 ? `剩余 ${progress.chances} 次` : '待查询', progress.chances === 0 ? 'success' : progress.eligible === false ? 'attention' : progress.chances > 0 ? 'pending' : ''));
      const prize = prizeName(progress.draw_prize);
      if (prize) draw.append(node('p', 'seven-result seven-result-prize', prize));
      else if (progress.eligible !== false) draw.append(node('small', '', '每天零点刷新一次机会'));
      const recent = node('td', 'seven-recent');
      recent.append(node('time', '', date(account.last_run)), node('p', 'seven-result', account.running || busy.has(account.id) ? '正在核对并执行…' : account.message || progress.message || '等待自动检查'));
      if (account.retry_message) recent.append(node('small', 'seven-login-warning', account.retry_message));
      if (account.next_run) recent.append(node('small', '', '下次 '+date(account.next_run)));
      const operations = node('td'), buttons = node('div', 'seven-row-actions');
      buttons.append(action('打卡', account, 'signin', progress.checked_today || progress.completed || progress.claimed));
      if (progress.completed && !progress.claimed) buttons.append(action('领红包', account, 'claim', false, true));
      buttons.append(action('夺宝', account, 'draw', progress.chances === 0), action('查询', account, 'refresh'));
      if (account.status !== 'active') {
        const login = node('button', 'quiet', '重新登录');
        login.type = 'button'; login.dataset.accountWindow = ''; login.dataset.accountRelogin = account.id;
        buttons.append(login);
      }
      operations.append(buttons);
      row.append(identity, signin, sequence, reward, draw, recent, operations);
      return row;
    });
    if (!rows.length) {
      const row = node('tr'), cell = node('td', 'seven-empty', model.accounts.length ? '当前筛选下没有账号。' : '共享账号池为空，请从左侧“账号管理”登录账号。');
      cell.colSpan = 7; row.append(cell); rows.push(row);
    }
    $('seven-accounts').replaceChildren(...rows);
  }
  function updateCampaign() {
    clearTimeout(expiryTimer);
    const campaign = model.campaign;
    if (campaign.ended) window.dispatchEvent(new CustomEvent('platforms-changed'));
    $('seven-period').textContent = campaign.end_at ? `至 ${date(campaign.end_at)}` : '活动时间暂未读取';
    $('seven-warning').textContent = [campaign.notice, model.warning].filter(Boolean).join('\n');
    $('seven-warning').hidden = !$('seven-warning').textContent;
    $('seven-rules-text').textContent = campaign.rules_text || '';
    $('seven-official-rules').hidden = !campaign.rules_text;
    $('seven-expiry-note').textContent = campaign.end_at ? `实际下线时间：${date(campaign.end_at)}。入口和后台任务跟随官方页面配置；如红包库存耗尽，整个活动会提前结束。` : '入口与自动任务跟随官方页面的实际下线时间。';
    const remaining = Date.parse(campaign.end_at)-Date.parse(campaign.server_time);
    if (campaign.active && remaining > 0) {
      const deadline = Date.now()+remaining;
      const expire = () => {
      const delay = deadline-Date.now();
      if (delay > 0) { expiryTimer = setTimeout(expire, Math.min(delay, 2147483647)); return; }
      model.campaign.active = false; model.campaign.ended = true; render();
      window.dispatchEvent(new CustomEvent('platforms-changed'));
      if ($('seven-claim').open) {
        $('seven-claim-submit').disabled = true; $('seven-send-code').disabled = true;
        $('seven-claim-error').textContent = '活动已结束，本次不再提交。'; $('seven-claim-error').hidden = false;
      }
      refresh();
      };
      expiryTimer = setTimeout(expire, Math.min(remaining, 2147483647));
    }
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      model = await api('/api/seven-day/status');
      loaded = true; updateCampaign(); render();
      $('seven-sync').textContent = '北京时间 · 最近同步 '+date(model.campaign.server_time);
    } catch (error) {
      feedback('读取失败：'+error.message);
      $('seven-sync').textContent = '本地服务暂未连接';
      if (!loaded) {
        $('seven-summary').textContent = '暂时无法读取';
        const row = node('tr'), cell = node('td', 'seven-empty', '暂时无法读取，稍后点击“刷新”重试。');
        cell.colSpan = 7; row.append(cell); $('seven-accounts').replaceChildren(row);
      }
    } finally { loading = false; }
  }
  async function run(account, kind) {
    if (busy.has(account.id)) return;
    busy.add(account.id); render();
    try {
      const value = await api('/api/seven-day/run', {id:account.id, kind});
      feedback(value.message || '已安排检查，请查看表格中的官方结果。', true);
      await refresh();
    } catch (error) { feedback(error.message); }
    finally { busy.delete(account.id); render(); }
  }
  function updateSettingsFields() {
    $('seven-signin-time-field').hidden = !$('seven-signin-enabled').checked;
    $('seven-signin-time').required = $('seven-signin-enabled').checked;
    $('seven-burst-fields').hidden = !$('seven-draw-enabled').checked;
  }
  function openSettings() {
    $('seven-settings-error').hidden = true;
    $('seven-signin-enabled').checked = model.settings.signin_enabled;
    $('seven-signin-time').value = model.settings.signin_time;
    $('seven-draw-enabled').checked = model.settings.draw_enabled;
    $('seven-burst-count').value = model.settings.burst_count;
    $('seven-burst-interval').value = model.settings.burst_interval;
    $('seven-retry-interval').value = model.settings.retry_interval_minutes;
    $('seven-max-attempts').value = model.settings.max_attempts;
    updateSettingsFields(); $('seven-settings').showModal();
  }
  async function saveSettings(event) {
    event.preventDefault();
    if (saving) return;
    saving = true; $('seven-settings-save').disabled = true;
    try {
      const value = await api('/api/seven-day/settings', {signin_enabled:$('seven-signin-enabled').checked, signin_time:$('seven-signin-time').value, draw_enabled:$('seven-draw-enabled').checked, burst_count:Number($('seven-burst-count').value), burst_interval:Number($('seven-burst-interval').value),retry_interval_minutes:Number($('seven-retry-interval').value),max_attempts:Number($('seven-max-attempts').value)});
      $('seven-settings').close(); feedback(value.message || '自动设置已保存，已应用于所有有效账号。', true); await refresh();
    } catch (error) { $('seven-settings-error').textContent = error.message; $('seven-settings-error').hidden = false; }
    finally { saving = false; $('seven-settings-save').disabled = false; render(); }
  }
  function updateCodeButton() {
    const remaining = claimAccount ? Math.max(0, Math.ceil(((smsExpires.get(claimAccount.id) || 0)-Date.now())/1000)) : 0;
    $('seven-send-code').textContent = sending ? '正在获取…' : remaining ? `${remaining} 秒后重新获取` : '重新获取验证码';
    $('seven-send-code').disabled = sending || claiming || remaining > 0 || !model.campaign.active;
  }
  function openClaim(account) {
    claimGeneration++; claimAccount = account;
    $('seven-claim-form').reset(); $('seven-claim-error').hidden = true;
    $('seven-claim-account').textContent = `${account.name || ''} · ${account.phone_masked}`;
    $('seven-claim-submit').disabled = false; $('seven-code').disabled = false;
    $('seven-sms-status').textContent = '正在自动获取验证码…';
    $('seven-claim').showModal(); updateCodeButton();
    if ((smsExpires.get(account.id) || 0) > Date.now()) $('seven-sms-status').textContent = '验证码已发送，请填写刚收到的验证码。';
    else sendCode();
    $('seven-code').focus();
  }
  async function sendCode() {
    if (sending || claiming || !claimAccount) return;
    const generation = claimGeneration, id = claimAccount.id;
    sending = true; updateCodeButton(); $('seven-claim-error').hidden = true;
    try {
      const value = await api('/api/seven-day/sms', {id});
      smsExpires.set(id, Date.now()+60000);
      if (generation === claimGeneration) $('seven-sms-status').textContent = value.message || '验证码已发送，请填写刚收到的验证码。';
    } catch (error) {
      if (generation === claimGeneration) { $('seven-claim-error').textContent = error.message; $('seven-claim-error').hidden = false; $('seven-sms-status').textContent = '验证码未发送，请稍后重试。'; }
    } finally { sending = false; updateCodeButton(); }
  }
  async function claim(event) {
    event.preventDefault();
    if (claiming || sending || !claimAccount) return;
    claiming = true;
    $('seven-claim-submit').disabled = true; $('seven-code').disabled = true;
    $('seven-claim-error').hidden = true; updateCodeButton();
    try {
      const value = await api('/api/seven-day/claim', {id:claimAccount.id, code:$('seven-code').value.trim()});
      $('seven-claim').close(); claimGeneration++; claimAccount = null;
      toast(value.message || '已受理，话费红包领取成功。'); await refresh();
    } catch (error) { $('seven-claim-error').textContent = error.message; $('seven-claim-error').hidden = false; }
    finally { claiming = false; $('seven-claim-submit').disabled = !model.campaign.active; $('seven-code').disabled = false; updateCodeButton(); }
  }
  $('seven-refresh').addEventListener('click', refresh);
  $('seven-search').addEventListener('input', render);
  $('seven-filter').addEventListener('change', render);
  $('seven-configure').addEventListener('click', openSettings);
  $('seven-settings-form').addEventListener('submit', saveSettings);
  $('seven-signin-enabled').addEventListener('change', updateSettingsFields);
  $('seven-draw-enabled').addEventListener('change', updateSettingsFields);
  $('seven-send-code').addEventListener('click', sendCode);
  $('seven-claim-form').addEventListener('submit', claim);
  document.querySelectorAll('[data-seven-close]').forEach(button => button.addEventListener('click', () => {
    const kind = button.dataset.sevenClose;
    if (kind === 'settings' && !saving) $('seven-settings').close();
    if (kind === 'claim' && !claiming) { $('seven-claim').close(); claimGeneration++; claimAccount = null; }
  }));
  $('seven-settings').addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  $('seven-claim').addEventListener('cancel', event => { if (claiming) event.preventDefault(); else { claimGeneration++; claimAccount = null; } });
  window.addEventListener('accounts-changed', refresh);
  window.addEventListener('focus', refresh);
  refresh();
  setInterval(() => { if (!document.hidden && !saving && !claiming) refresh(); }, 5000);
  setInterval(updateCodeButton, 1000);
})();
