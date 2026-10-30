export function mountPortal(root, {manager = false, embedded = false} = {}) {
const isManager = manager;
const prefix = embedded ? 'modal-' : '';
if (embedded) {
  root.querySelectorAll('[id]').forEach(item => { item.id = prefix + item.id; });
  root.querySelectorAll('label[for]').forEach(item => { item.htmlFor = prefix + item.htmlFor; });
}
const byId = id => root.querySelector('#' + prefix + id);
let working = false, smsPhone = '', smsReadyAt = 0, reloginId = '';
let poolRecords = [];
let focusedLoginId = '';
const smsAttempts = new Map();
async function portalApi(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '操作失败，请重试。');
  return data;
}
function portalFeedback(text, success = false) {
  byId('portal-feedback').textContent = text;
  byId('portal-feedback').classList.toggle('success', success);
}
function node(tag, className, text) {
  const item = document.createElement(tag); item.className = className || '';
  if (text !== undefined) item.textContent = text;
  return item;
}
async function refreshPool() {
  const data = await portalApi('/api/logins');
  poolRecords = data.accounts;
  let storageNotice = byId('storage-notice');
  if (!storageNotice) {
    storageNotice = node('p','portal-feedback'); storageNotice.id = prefix + 'storage-notice'; storageNotice.setAttribute('role','status');
    root.querySelector('.portal-header').after(storageNotice);
  }
  storageNotice.textContent = data.warning || ''; storageNotice.hidden = !data.warning;
  byId('pool-count').textContent = data.accounts.length;
  if (!isManager) {
    byId('overview-account-count').textContent = data.accounts.length;
    byId('overview-login-required').textContent = data.accounts.filter(record => record.status === 'login_required').length;
  }
  const host = byId(isManager ? 'managed-accounts' : 'home-accounts');
  const rows = data.accounts.map(record => {
    if (!isManager) {
      const pill = node('div', 'account-pill');
      const title = node('span', '', record.name || record.phone_masked);
      pill.append(node('i'), title);
      pill.classList.toggle('needs-attention', !!record.status && record.status !== 'active');
      if (record.name) pill.append(node('small', '', record.phone_masked));
      if (record.status && record.status !== 'active') pill.append(node('small','',loginStatus(record)));
      return pill;
    }
    const row = node('tr', 'managed-account ' + (record.status === 'active' ? 'account-ready' : record.status === 'login_required' ? 'account-expired' : 'account-pending'));
    row.dataset.loginId = record.id; row.dataset.status = record.status;
    row.classList.toggle('account-focused', record.id === focusedLoginId);
    const identity = node('td', 'account-identity');
    identity.append(node('strong', '', record.phone_masked));
    const stamp = new Date(record.created_at * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false});
    identity.append(node('small', 'account-time', stamp));
    const name = node('input'); name.value = record.name || ''; name.placeholder = '添加账号备注'; name.maxLength = 40;
    name.setAttribute('aria-label', record.phone_masked + '的账号备注');
    const nameCell = node('td', 'account-name'); nameCell.append(name);
    const stateCell = node('td', 'account-state-cell');
    stateCell.append(node('span', 'account-state', loginStatus(record)));
    const message = node('td', 'session-message', record.message || '—');
    const actions = node('div', 'row-actions');
    const relogin = node('button', 'portal-secondary', '重新登录'); relogin.type = 'button';
    relogin.addEventListener('click', () => managerAction(() => reloginAccount(record)));
    actions.append(relogin);
    for (const [label, endpoint, style] of [['保存备注','rename','portal-quiet'],['校验并重连','check','portal-quiet'],['退出账号','logout','logout-action']]) {
      const button = node('button', style, label); button.type = 'button';
      button.addEventListener('click', () => managerAction(async () => {
        const result = await portalApi('/api/logins/' + endpoint, {id: record.id, name: name.value});
        portalFeedback(result.message || (endpoint === 'logout' ? '该账号已退出，两个功能中的对应会话已清除。' : '已保存。'), true);
        window.publishAccountsChanged(); await refreshPool();
      }));
      actions.append(button);
    }
    const actionCell = node('td', 'account-actions'); actionCell.append(actions);
    row.append(identity, nameCell, stateCell, message, actionCell); return row;
  });
  if (!rows.length) {
    if (isManager) {
      const row = node('tr'), cell = node('td', 'account-table-empty', '账号池还是空的，在登录区添加第一个账号。');
      cell.colSpan = 5; row.append(cell); rows.push(row);
    } else rows.push(node('p','muted', '尚无登录账号，打开账号管理即可添加。'));
  }
  host.replaceChildren(...rows);
}
function loginStatus(record) {
  return {active:'● 已登录 · 自动维护', restoring:'◌ 正在自动恢复', retry:'◌ 等待自动重试', login_required:'○ 需要短信验证'}[record.status] || '● 已登录';
}
function managerBusy(value) {
  working = value;
  root.querySelectorAll('form input, button[type="submit"], .row-actions button, #'+prefix+'cancel-relogin').forEach(item => item.disabled = value);
  smsCountdown();
}
async function managerAction(action) {
  if (working) return;
  managerBusy(true);
  try { await action(); } catch(error) { portalFeedback(error.message); }
  finally { managerBusy(false); }
}
function smsCountdown() {
  const button = byId('send-sms');
  const wait = Math.max(0,Math.ceil((smsReadyAt - Date.now())/1000));
  button.textContent = wait ? wait + ' 秒后重发' : (reloginId ? '重新发送验证码' : '获取验证码'); button.disabled = working || wait > 0;
}
function setRelogin(record = null) {
  reloginId = record ? record.id : '';
  byId('login-identity').hidden = !!reloginId;
  byId('login-identity').disabled = !!reloginId;
  byId('cancel-relogin').hidden = !reloginId;
  byId('login-panel-title').textContent = record ? '重新登录 · ' + (record.name || record.phone_masked) : '添加登录账号';
  byId('login-panel-description').textContent = record ? '验证码将发送至 ' + record.phone_masked + '，只需填写验证码。' : '已有登录会直接复用，不再重复发送验证码。';
  byId('manager-login-button').textContent = record ? '确认重新登录' : '登录并加入账号池';
  byId('code').value = '';
  const phone = byId('phone').value.trim(), attempt = smsAttempts.get(reloginId ? 'id:' + reloginId : phone);
  smsReadyAt = attempt?.readyAt || 0;
  if (!reloginId) smsPhone = attempt?.sent ? phone : '';
  smsCountdown();
}
async function reloginAccount(record) {
  setRelogin(record);
  byId('manager-login-form').scrollIntoView({block:'nearest', behavior:'smooth'});
  await sendManagerSms(); byId('code').focus();
}
async function sendManagerSms(resend = false) {
  const phone = byId('phone').value.trim(), key = reloginId ? 'id:' + reloginId : phone;
  smsReadyAt = Date.now() + 60000;
  smsAttempts.set(key, {readyAt:smsReadyAt, sent:false});
  portalFeedback(reloginId ? '正在向原手机号发送验证码…' : '正在检查账号并获取验证码…');
  const result = await portalApi(reloginId ? '/api/logins/relogin/sms' : '/api/logins/sms', reloginId ? {id:reloginId, resend, force:true} : {phone});
  if (result.reused) {
    const relogging = !!reloginId;
    smsAttempts.delete(key); smsPhone = ''; byId('code').value = '';
    if (relogging) setRelogin();
    portalFeedback(relogging ? '该账号登录已恢复，无需再次短信验证。' : '该手机号已在账号池中，已复用现有登录，无需再次验证码。', true);
    window.publishAccountsChanged(); await refreshPool(); return;
  }
  smsReadyAt = Date.now() + result.retry_after * 1000;
  smsAttempts.set(key, {readyAt:smsReadyAt, sent:true});
  if (reloginId) byId('login-panel-description').textContent = '验证码已发送至 ' + result.phone_masked + '，只需填写验证码。';
  else smsPhone = phone;
  portalFeedback('验证码已发送至 ' + result.phone_masked, true); byId('code').value = '';
}
if (isManager) {
  byId('cancel-relogin').addEventListener('click', () => { setRelogin(); portalFeedback(''); });
  byId('phone').addEventListener('input', () => {
    const phone = byId('phone').value.trim(), attempt = smsAttempts.get(phone);
    smsReadyAt = attempt?.readyAt || 0; smsPhone = attempt?.sent ? phone : ''; smsCountdown();
  });
  byId('refresh-accounts').addEventListener('click', () => refreshPool().catch(error => portalFeedback(error.message)));
  byId('send-sms').addEventListener('click', () => {
    if ((!reloginId && !byId('phone').reportValidity()) || Date.now() < smsReadyAt) return;
    managerAction(() => sendManagerSms(!!reloginId));
  });
  byId('manager-login-form').addEventListener('submit', event => {
    event.preventDefault(); const phone = byId('phone').value.trim(), relogging = !!reloginId;
    if (!relogging && phone !== smsPhone) { portalFeedback('请先向该手机号获取验证码。'); return; }
    managerAction(async () => {
      portalFeedback(relogging ? '正在重新登录并更新共享凭据…' : '正在登录并加入共享账号池…');
      const result = await portalApi(relogging ? '/api/logins/relogin' : '/api/logins/login', relogging ? {id:reloginId, code:byId('code').value.trim(), force:true} : {phone, code:byId('code').value.trim(), name:byId('login-name').value.trim()});
      if (relogging) { smsAttempts.delete('id:' + reloginId); setRelogin(); }
      else { byId('manager-login-form').reset(); smsPhone = ''; smsReadyAt = 0; }
      portalFeedback(relogging ? result.message : '账号已加入，灵犀和周三充值日均可选择使用。', true);
      window.publishAccountsChanged(); await refreshPool();
    });
  });
  setInterval(smsCountdown, 1000);
}
function reloadWhenIdle() {
  if (embedded && !root.closest('dialog').open || working || root.contains(document.activeElement) && document.activeElement.closest('.managed-account')) return;
  refreshPool().catch(error => portalFeedback(error.message));
}
window.addEventListener('accounts-changed', reloadWhenIdle);
window.addEventListener('focus', reloadWhenIdle);
refreshPool().catch(error => portalFeedback(error.message));
setInterval(() => { if (!document.hidden) reloadWhenIdle(); }, 10000);
return {refresh: reloadWhenIdle, focus: async (id, message) => {
  focusedLoginId = id; await refreshPool();
  const row = [...root.querySelectorAll('.managed-account')].find(item => item.dataset.loginId === id);
  if (!row) { portalFeedback('该账号已退出，请刷新账号列表。'); return; }
  row.tabIndex = -1; row.scrollIntoView({block:'center', behavior:'smooth'}); row.focus({preventScroll:true});
  if (message) portalFeedback(message);
}, relogin: id => managerAction(async () => {
  await refreshPool();
  const record = poolRecords.find(item => item.id === id);
  if (!record) { portalFeedback('该账号已退出，请刷新账号列表。'); return; }
  await reloginAccount(record);
})};
}
