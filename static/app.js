"use strict";
const $ = id => document.getElementById(id);
let hasCredentials = false, active = null, switching = false;
let selectedLoginId = '';
let chosenLoginId = '';
let accountRecords = [], lastAccountState = {};
const histories = new Map();
const pickerPanel = $('chat-account-panel');
const pickerStatuses = {active:{label:'已登录',tone:'ready'},login_required:{label:'需重新登录',tone:'expired'},restoring:{label:'自动恢复中',tone:'pending'},retry:{label:'等待重连',tone:'pending'}};
const checkTime = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false});
function pickerNode(tag,className,text) {
  const node = document.createElement(tag); node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function renderAccountPicker() {
  const list = $('chat-account-list'), scrollTop = list.scrollTop, focused = document.activeElement;
  const focusedId = list.contains(focused) ? focused.dataset.loginId || focused.dataset.accountRelogin : '';
  const focusedRelogin = focused.classList.contains('account-choice-login');
  const ready = accountRecords.filter(record => record.status === 'active').length;
  const expired = accountRecords.filter(record => record.status === 'login_required').length;
  $('chat-account-count').textContent = `共 ${accountRecords.length} 个 · ${ready} 个已登录${expired ? ` · ${expired} 个需验证` : ''}`;
  const query = $('chat-account-search').value.trim().toLocaleLowerCase();
  const shown = accountRecords.filter(record => `${record.name} ${record.phone_masked}`.toLocaleLowerCase().includes(query));
  shown.sort((a,b) => Number(a.status !== 'active') - Number(b.status !== 'active'));
  const rows = shown.map(record => {
    const state = switching && record.id === chosenLoginId ? {label:'连接中',tone:'pending'} : pickerStatuses[record.status] || {label:'待校验',tone:'pending'};
    const row = pickerNode('div','account-choice ' + state.tone); row.setAttribute('role','listitem');
    const choose = pickerNode('button','account-choice-select'); choose.type = 'button'; choose.dataset.loginId = record.id;
    choose.dataset.loginRequired = String(record.status === 'login_required');
    choose.disabled = record.status === 'login_required' || !!active || switching;
    const name = record.name || record.phone_masked;
    choose.setAttribute('aria-label',`${name}，${record.phone_masked}，${state.label}`);
    if (record.id === selectedLoginId) { row.classList.add('selected'); choose.setAttribute('aria-current','true'); }
    const main = pickerNode('span','account-choice-main'), title = pickerNode('span','account-choice-title');
    title.append(pickerNode('strong','',name),pickerNode('span','account-choice-badge ' + state.tone,state.label));
    main.append(title,pickerNode('small','account-choice-phone',record.phone_masked));
    if (record.checked_at) main.append(pickerNode('small','account-choice-checked','最近校验 ' + checkTime.format(new Date(record.checked_at * 1000))));
    choose.append(pickerNode('span','account-choice-avatar',name.slice(0,2)),main);
    if (record.id === selectedLoginId) {
      const check = pickerNode('span','account-choice-check'); check.setAttribute('aria-hidden','true');
      check.innerHTML = '<svg viewBox="0 0 24 24"><path d="m5 12 4 4 10-10"/></svg>'; choose.append(check);
    }
    choose.addEventListener('click',() => connectAccount(record.id));
    row.append(choose);
    if (record.status !== 'active') {
      const note = pickerNode('div','account-choice-note');
      note.append(pickerNode('span','',record.message || (record.status === 'login_required' ? '登录已失效，需要短信验证。' : '后台正在恢复，也可重新连接。')));
      if (record.status === 'login_required') {
        const relogin = pickerNode('button','account-choice-login','重新登录'); relogin.type = 'button';
        relogin.setAttribute('data-account-window',''); relogin.dataset.accountRelogin = record.id;
        relogin.disabled = !!active || switching;
        note.append(relogin);
      }
      row.append(note);
    }
    return row;
  });
  if (!rows.length) rows.push(pickerNode('p','account-choice-empty',query ? '没有匹配的账号，试试备注或号码尾号。' : '暂无登录账号，请从左侧账号管理添加。'));
  list.replaceChildren(...rows); list.scrollTop = scrollTop;
  if (focusedId) {
    const target = [...list.querySelectorAll(focusedRelogin ? '.account-choice-login' : '.account-choice-select')].find(button => (button.dataset.loginId || button.dataset.accountRelogin) === focusedId);
    if (target && !target.disabled) target.focus({preventScroll:true});
    else $('chat-account-search').focus({preventScroll:true});
  }
}
$('chat-account-search').addEventListener('input',renderAccountPicker);
$('refresh-chat-accounts').addEventListener('click',() => loadAccounts(false).catch(error => $('account-feedback').textContent = error.message));
pickerPanel.addEventListener('keydown',event => {
  if (!['ArrowDown','ArrowUp'].includes(event.key)) return;
  event.preventDefault();
  const choices = [...$('chat-account-list').querySelectorAll('.account-choice-select:not(:disabled),.account-choice-login')];
  if (!choices.length) return;
  const current = choices.indexOf(document.activeElement);
  choices[(current + (event.key === 'ArrowDown' ? 1 : current < 0 ? 0 : -1) + choices.length) % choices.length].focus();
});
function busy(value) {
  $('send').disabled = value || switching || !hasCredentials;
  $('stop').hidden = !value;
  $('new-chat').disabled = value || switching || !hasCredentials;
  pickerPanel.setAttribute('aria-busy',String(value || switching));
  pickerPanel.querySelectorAll('.account-choice-select').forEach(button => { button.disabled = value || switching || button.dataset.loginRequired === 'true'; });
  pickerPanel.querySelectorAll('.account-choice-login').forEach(button => { button.disabled = value || switching; });
  const chosen = accountRecords.find(record => record.id === chosenLoginId);
  $('connect-account').hidden = hasCredentials || !chosen || chosen.status === 'login_required';
  $('connect-account').textContent = switching ? '正在连接…' : '重新连接';
  $('connect-account').disabled = value || switching || !chosen || chosen.status === 'login_required';
}
function accountState(state) {
  lastAccountState = state;
  const chosen = accountRecords.find(record => record.id === (selectedLoginId || chosenLoginId));
  const unavailable = chosen && chosen.status !== 'active';
  hasCredentials = !!selectedLoginId && !unavailable && !!state.has_credentials;
  const connected = hasCredentials && state.verified;
  $('account-state').className = 'status ' + (connected ? 'connected' : chosen?.status === 'login_required' ? 'expired' : unavailable ? 'pending' : '');
  $('account-state').querySelector('span').textContent = connected ? '已连接 · ' + (state.phone_masked || chosen.phone_masked) : unavailable ? (pickerStatuses[chosen.status]?.label || '待校验') + ' · ' + chosen.phone_masked : '尚未连接账号';
  busy(!!active);
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败，请重试。');
  return data;
}
async function status(loginId = selectedLoginId) {
  const state = await api('/api/status' + (loginId ? '?login_id=' + encodeURIComponent(loginId) : ''));
  if (loginId === selectedLoginId) accountState(state);
}
function welcome() {
  const box = document.createElement('div'); box.className = 'welcome'; box.id = 'welcome';
  const icon = document.createElement('span'); icon.className = 'welcome-icon'; icon.textContent = '✦';
  const title = document.createElement('h2'); title.textContent = '有什么想问灵犀？';
  const note = document.createElement('p'); note.textContent = '对话按账号独立保存，可在左侧切换账号。';
  box.append(icon,title,note); return box;
}
function showConversation(loginId) {
  if (selectedLoginId && selectedLoginId !== loginId) histories.set(selectedLoginId, [...$('messages').childNodes]);
  if (selectedLoginId !== loginId) $('messages').replaceChildren(...(histories.get(loginId) || [welcome()]));
  selectedLoginId = loginId;
  sessionStorage.setItem('lingxi-login', loginId);
}
async function connectAccount(loginId) {
  const chosen = accountRecords.find(record => record.id === loginId);
  if (!chosen || chosen.status === 'login_required' || active || switching) return;
  chosenLoginId = loginId; switching = true; busy(false); renderAccountPicker(); $('account-feedback').textContent = '正在连接所选账号…';
  try {
    const state = await api('/api/login/select', {id: loginId});
    chosen.status = 'active'; chosen.checked_at = Date.now()/1000;
    showConversation(loginId); accountState(state); $('account-feedback').textContent = '';
  } catch (error) { $('account-feedback').textContent = error.message; chosenLoginId = selectedLoginId || loginId; accountState(lastAccountState); }
  finally { switching = false; busy(!!active); renderAccountPicker(); }
  loadAccounts(false).catch(error => $('account-feedback').textContent = error.message);
}
async function loadAccounts(autoConnect = true) {
  const data = await api('/api/logins');
  accountRecords = data.accounts;
  const previous = selectedLoginId || chosenLoginId || sessionStorage.getItem('lingxi-login');
  const valid = new Set(data.accounts.map(row => row.id));
  const chosen = accountRecords.find(record => record.id === previous) || accountRecords.find(record => record.status === 'active') || accountRecords.find(record => record.status !== 'login_required') || accountRecords[0];
  chosenLoginId = chosen?.id || '';
  if (selectedLoginId && !valid.has(selectedLoginId)) {
    showConversation(''); lastAccountState = {}; $('account-feedback').textContent = '该账号已退出，请重新选择。';
  }
  renderAccountPicker(); accountState(lastAccountState);
  if (autoConnect && !selectedLoginId && chosen && chosen.status !== 'login_required') await connectAccount(chosen.id);
  busy(!!active);
}
$('connect-account').addEventListener('click', () => connectAccount(chosenLoginId));
$('new-chat').addEventListener('click', async () => {
  try {
    await api('/api/conversation/new', {login_id:selectedLoginId});
    histories.delete(selectedLoginId); $('messages').replaceChildren(welcome()); $('chat-feedback').textContent = '已开始新对话。';
  } catch(error) { $('chat-feedback').textContent = error.message; }
});
window.addEventListener('accounts-changed', () => loadAccounts().catch(error => $('account-feedback').textContent = error.message));
window.addEventListener('focus', () => {
  if (!active && !switching) loadAccounts().then(() => status()).catch(() => {});
});
setInterval(() => { if (!document.hidden && !active && !switching) loadAccounts(false).catch(error => $('account-feedback').textContent = error.message); },15000);
function message(role, text) {
  $("welcome")?.remove();
  const wrap = document.createElement("div"); wrap.className = "message " + role;
  const avatar = document.createElement("div"); avatar.className = "avatar"; avatar.textContent = role === "user" ? "我" : "灵";
  const content = document.createElement("div"); content.className = "message-content";
  const body = document.createElement("span"); body.textContent = text;
  content.append(body); wrap.append(avatar, content); $("messages").append(wrap);
  return {body, content};
}
function scroll() { $("messages").scrollTop = $("messages").scrollHeight; }
$("stop").addEventListener("click", () => active?.abort());
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("chat-form").requestSubmit(); }
});
$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  const requestLoginId = selectedLoginId;
  if (!text || active || switching || !hasCredentials) return;
  message("user", text); $("message").value = "";
  const reply = message("assistant", "正在连接灵犀…");
  let received = false, ended = false;
  active = new AbortController(); busy(true); scroll(); $("chat-feedback").textContent = "";
  function packet(raw) {
    const lines = raw.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart());
    if (!lines.length) return;
    const part = JSON.parse(lines.join("\n"));
    if (part.type === "error") throw new Error(part.message);
    if (part.type === "clear") {
      reply.body.textContent = ""; reply.content.querySelectorAll("details").forEach((el) => el.remove()); received = false;
    } else if (part.type === "warning") {
      $("chat-feedback").textContent = part.message;
    } else if (part.type === "progress") {
      if (!received) reply.body.textContent = part.text;
    } else if (part.type === "delta") {
      if (!received) reply.body.textContent = "";
      received = true; reply.body.textContent += part.text;
    } else if (part.type === "card") {
      if (!received) reply.body.textContent = "";
      received = true;
      const details = document.createElement("details"), summary = document.createElement("summary"), pre = document.createElement("pre");
      summary.textContent = part.kind === "Bubble" ? "查看建议内容" : "查看卡片内容";
      pre.textContent = JSON.stringify(part.content, null, 2); details.append(summary, pre); reply.content.append(details);
    } else if (part.type === "done") { ended = true; }
    scroll();
  }
  try {
    const response = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({message: text, login_id: requestLoginId}), signal: active.signal});
    if (!response.ok) { const data = await response.json(); throw new Error(data.error || "发送失败。"); }
    if (!response.body) throw new Error("浏览器不支持流式接收。");
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
    try {
      while (true) {
        const {value, done} = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, {stream: true});
        buffer = buffer.replace(/\r\n/g, "\n");
        let cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) { packet(buffer.slice(0, cut)); buffer = buffer.slice(cut + 2); }
        if (done) break;
      }
      if (buffer.trim()) packet(buffer);
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    if (!ended) throw new Error("连接已中断，回复可能不完整。");
  } catch (error) {
    const hint = error.name === "AbortError" ? "已停止接收。" : error.message;
    if (!received) reply.body.textContent = hint;
    $("chat-feedback").textContent = hint;
  } finally {
    active = null; busy(false);
    try { await loadAccounts(false); await status(requestLoginId); } catch (_) { $("chat-feedback").textContent = "本地服务未连接，请检查程序是否仍在运行。"; }
    scroll();
  }
});

loadAccounts().catch(error => $('account-feedback').textContent = error.message);
