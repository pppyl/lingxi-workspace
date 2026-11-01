(() => {
  'use strict';
  const page = document.body.dataset.workspace;
  if (!page) return;
  const workspaceFetch=window.fetch.bind(window);
  window.fetch=async(...args)=>{
    const response=await workspaceFetch(...args);
    if(response.headers.get('X-Workspace-Access'))location.replace('/access?next='+encodeURIComponent(location.pathname+location.search));
    return response;
  };
  const localHost=['localhost','127.0.0.1','[::1]'].includes(location.hostname);
  if(new URLSearchParams(location.search).get('desktop')==='1')sessionStorage.setItem('desktop-client','1');
  if(localHost&&sessionStorage.getItem('desktop-client')==='1')import('/static/desktop-window.js');
  const favicon=document.querySelector('link[rel="icon"]')||document.createElement('link');
  favicon.rel='icon';favicon.href='/favicon.ico';document.head.append(favicon);
  const paths = {
    grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    chat:'M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5a9.5 9.5 0 0 1 19 0Z M7 9h10 M7 13h6',
    ticket:'M3 5h18v5a2 2 0 0 0 0 4v5H3v-5a2 2 0 0 0 0-4V5Z M15 6v2 M15 11v2 M15 16v2',
    star:'m12 3 2.8 5.7 6.3.9-4.6 4.4 1.1 6.3-5.6-3-5.6 3 1.1-6.3L3 9.6l6.2-.9Z',
    calendar:'M4 5h16v16H4z M8 3v4 M16 3v4 M4 10h16 M8 15l3 3 5-5',
    users:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75',
    menu:'M4 6h16 M4 12h16 M4 18h16',
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]}"/></svg>`;
  const pages = [
    ['home','/','工作台'], ['chat','/lingxi','即时对话'],
    ['lingxi-tasks','/lingxi/tasks','定时发送'],
    ['star-day','/star-day','星动日'],
    ['recharge','/recharge/','周三充值日'],
    ['recharge-tasks','/recharge/?tab=tasks','周三充值日'],
    ['checkin','/checkin','签到有礼'],
    ['monthly-coupon','/monthly-coupon','每月话费券'],
    ['coupons','/coupons','我的卡券'],
    ['platforms','/platforms','平台管理'],
  ];
  const groups = [
    {title:'灵犀对话',icon:'chat',keys:['chat','lingxi-tasks']},
  ];
  const checkinChannelTitle = {'/checkin/web':'网页渠道签到','/checkin/wechat':'微信渠道签到'}[location.pathname];
  const currentGroup = groups.find(group => group.keys.includes(page)) || (checkinChannelTitle ? {title:'签到有礼'} : null);
  const nav = document.createElement('aside'); nav.className = 'workspace-nav'; nav.id = 'workspace-navigation';
  nav.innerHTML = `<a class="workspace-brand" href="/" aria-label="本地工具台首页"><span class="workspace-logo"><svg viewBox="0 0 32 32" aria-hidden="true"><path fill="#84beff" d="m16 2 13 7-13 7L3 9Z"/><path fill="#398cff" d="m3 12 11 6v13L3 25Z"/><path fill="#b9daff" d="m18 18 11-6v13l-11 6Z"/></svg></span><span>本地工具台<small>LOCAL WORKSPACE</small></span></a>
    <div class="workspace-nav-label">工作空间</div><nav aria-label="主导航"><a href="/" class="workspace-nav-item${page === 'home' ? ' active' : ''}"${page === 'home' ? ' aria-current="page"' : ''}>${icon('grid')}<span>工作台</span></a>
    ${groups.map(group => `<details class="workspace-nav-group${currentGroup === group ? ' contains-current' : ''}" open><summary>${icon(group.icon)}<span>${group.title}</span><svg class="workspace-group-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m8 10 4 4 4-4"/></svg></summary><div class="workspace-subnav">${pages.filter(item => group.keys.includes(item[0])).map(([key,url,label]) => `<a href="${url}" class="workspace-subnav-item${page === key ? ' active' : ''}"${page === key ? ' aria-current="page"' : ''}>${label}</a>`).join('')}</div></details>${group.keys.includes('chat') ? `<a href="/star-day" class="workspace-nav-item${page === 'star-day' ? ' active' : ''}"${page === 'star-day' ? ' aria-current="page"' : ''}>${icon('star')}<span>星动日</span></a><a href="/recharge/" class="workspace-nav-item${['recharge','recharge-tasks'].includes(page) ? ' active' : ''}"${['recharge','recharge-tasks'].includes(page) ? ' aria-current="page"' : ''}>${icon('ticket')}<span>周三充值日</span></a>` : ''}`).join('')}
    <a href="/checkin" class="workspace-nav-item${page === 'checkin' ? ' active' : ''}"${page === 'checkin' ? ' aria-current="page"' : ''}>${icon('calendar')}<span>签到有礼</span></a>
    <a href="/monthly-coupon" class="workspace-nav-item${page === 'monthly-coupon' ? ' active' : ''}"${page === 'monthly-coupon' ? ' aria-current="page"' : ''}>${icon('ticket')}<span>每月话费券</span></a>
    <a href="/coupons" class="workspace-nav-item${page === 'coupons' ? ' active' : ''}"${page === 'coupons' ? ' aria-current="page"' : ''}>${icon('ticket')}<span>我的卡券</span></a>
    <div id="workspace-activity-navigation"></div></nav>
    <div class="workspace-nav-divider"></div><button type="button" class="workspace-nav-item${page === 'accounts' ? ' active' : ''}" data-account-window>${icon('users')}<span>账号管理</span></button>
    <a href="/platforms" class="workspace-nav-item${page === 'platforms' ? ' active' : ''}"${page === 'platforms' ? ' aria-current="page"' : ''}>${icon('grid')}<span>平台管理</span></a>
    <div class="workspace-nav-footer"><span class="workspace-service-dot"></span><div>本地服务<small id="workspace-host"></small></div><span class="workspace-private">本机</span></div>`;
  nav.querySelector('#workspace-host').textContent = location.host;
  nav.querySelector('.workspace-private').textContent = localHost?'本机':'远程';
  const networkAddresses = document.createElement('div');networkAddresses.id='workspace-addresses';
  nav.querySelector('.workspace-nav-footer').append(networkAddresses);
  const current = pages.find(item => item[0] === page);
  const topbar = document.createElement('header'); topbar.className = 'workspace-topbar';
  topbar.innerHTML = `<div class="workspace-location"><button class="workspace-menu" aria-label="展开导航" aria-controls="workspace-navigation" aria-expanded="false" type="button">${icon('menu')}</button><span class="workspace-location-root">工作空间</span><span class="workspace-location-divider">/</span>${currentGroup ? `<span class="workspace-location-parent">${currentGroup.title}</span><span class="workspace-location-divider">/</span>` : ''}<strong></strong></div><div class="workspace-topbar-actions"><span class="workspace-local"><i></i>本地运行</span><button type="button" class="workspace-account-button" data-account-window><span class="workspace-account-avatar">${icon('users')}</span><span>共享账号</span><span class="workspace-chevron">⌄</span></button></div>`;
  topbar.querySelector('.workspace-location strong').textContent = checkinChannelTitle || (current ? current[2] : document.querySelector('h1')?.textContent || '账号管理');
  const shade = document.createElement('button'); shade.type = 'button'; shade.className = 'workspace-nav-shade'; shade.setAttribute('aria-label','收起导航'); shade.hidden = true;
  function toggleNavigation() {
    const open = document.body.classList.toggle('workspace-nav-open');
    topbar.querySelector('.workspace-menu').setAttribute('aria-expanded',String(open));
    topbar.querySelector('.workspace-menu').setAttribute('aria-label',open ? '收起导航' : '展开导航');
    shade.hidden = !open;
  }
  topbar.querySelector('.workspace-menu').addEventListener('click',toggleNavigation);
  shade.addEventListener('click',toggleNavigation);
  nav.addEventListener('click',event => { if(event.target.closest('[data-account-window]') && document.body.classList.contains('workspace-nav-open')) toggleNavigation(); });
  document.addEventListener('keydown',event => { if(event.key === 'Escape' && document.body.classList.contains('workspace-nav-open')) toggleNavigation(); });
  document.body.prepend(nav, topbar, shade);
  import('/static/updates.js');
  import('/static/network.js');

  const seasonalNavigation = nav.querySelector('#workspace-activity-navigation');
  let seasonalTimer;
  function showSeasonalCampaign(platforms) {
    clearTimeout(seasonalTimer);
    const active = platforms.filter(platform => platform.active);
    seasonalNavigation.replaceChildren(...active.filter(platform => platform.navigation !== 'checkin').map(platform => {
      const link = document.createElement('a'); link.href = platform.home;
      link.className = 'workspace-nav-item'+(location.pathname === platform.home ? ' active' : '');
      if (location.pathname === platform.home) link.setAttribute('aria-current','page');
      link.innerHTML = icon('calendar');
      const label = document.createElement('span'); label.textContent = platform.title; link.append(label);
      return link;
    }));
    const remaining = active.map(platform => Date.parse(platform.end_at)-Date.parse(platform.server_time)).filter(Number.isFinite);
    if (remaining.length) seasonalTimer = setTimeout(refreshSeasonalCampaign, Math.min(Math.max(0, Math.min(...remaining)), 2147483647));
  }
  async function refreshSeasonalCampaign() {
    try {
      const response = await fetch('/api/platforms');
      if (response.ok) showSeasonalCampaign((await response.json()).platforms);
    } catch { /* Keep the last confirmed end timer while the local server restarts. */ }
  }
  refreshSeasonalCampaign();
  window.addEventListener('platforms-changed', refreshSeasonalCampaign);
  window.addEventListener('focus', refreshSeasonalCampaign);
  setInterval(() => { if (!document.hidden) refreshSeasonalCampaign(); }, 60000);

  if (page !== 'home') return;
  const date = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',weekday:'long'});
  document.getElementById('dashboard-date').textContent = date.format(new Date());
  const activeStates = new Set(['preparing','running','queued','receiving','stopping','armed','cancelling','settling']);
  const labels = {waiting:'待执行',preparing:'准备中',running:'执行中',queued:'排队中',receiving:'接收中',stopping:'停止中',completed:'已完成',partial:'部分完成',failed:'失败',cancelled:'已停止',missed:'已错过',interrupted:'已中断'};
  const time = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  function element(tag, className, text) {
    const node = document.createElement(tag); node.className = className; node.textContent = text; return node;
  }
  async function refreshDashboard() {
    const results = await Promise.allSettled(['/api/lingxi/tasks','/recharge/api/accounts'].map(async url => {
      const response = await fetch(url);
      if (!response.ok) throw new Error('暂时无法读取任务');
      return response.json();
    }));
    const [lingxi,recharge] = results;
    document.getElementById('overview-lingxi-waiting').textContent = lingxi.status === 'fulfilled' ? lingxi.value.tasks.filter(job => job.enabled && !activeStates.has(job.state)).length : '—';
    document.getElementById('overview-recharge-waiting').textContent = recharge.status === 'fulfilled' ? Object.values(recharge.value.jobs).filter(job => job.enabled && !activeStates.has(job.state)).length : '—';
    const rows = document.getElementById('dashboard-task-rows');
    if (lingxi.status !== 'fulfilled') {
      const cell = element('td','dashboard-empty','暂时无法读取，进入定时发送页查看。'); cell.colSpan = 4;
      const row = document.createElement('tr'); row.append(cell); rows.replaceChildren(row); return;
    }
    const tasks = [...lingxi.value.tasks].sort((a,b) => Number(b.enabled)-Number(a.enabled) || (b.updated_at || '').localeCompare(a.updated_at || '')).slice(0,5);
    const rendered = tasks.map(job => {
      const row = document.createElement('tr'), account = document.createElement('td');
      account.append(element('strong','',job.account_name),element('small','',job.content));
      const state = document.createElement('td');
      state.append(element('span','dashboard-badge ' + (activeStates.has(job.state) ? 'running' : job.state === 'completed' ? 'success' : job.state === 'waiting' ? 'waiting' : 'attention'),labels[job.state] || job.state));
      const reply = document.createElement('td'), text = element('p','dashboard-reply',job.last_reply?.text || job.message || '暂无回复'); text.title = text.textContent; reply.append(text);
      row.append(account,element('td','dashboard-time',time.format(new Date(job.enabled ? job.next_run_at : job.run_at))),state,reply); return row;
    });
    if (!rendered.length) { const cell = element('td','dashboard-empty','还没有定时任务，先创建第一项预约。'); cell.colSpan = 4; const row = document.createElement('tr'); row.append(cell); rendered.push(row); }
    rows.replaceChildren(...rendered);
    document.getElementById('dashboard-task-total').textContent = `${lingxi.value.tasks.length} 项任务`;
  }
  refreshDashboard();
  window.addEventListener('focus',refreshDashboard);
  setInterval(() => { if (!document.hidden) refreshDashboard(); },15000);
})();
