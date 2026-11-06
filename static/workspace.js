(() => {
  'use strict';
  const page = document.body.dataset.workspace;
  if (!page) return;
  const currentFeature=document.body.dataset.checkinChannel?document.body.dataset.checkinChannel+'-checkin':({'chat':'lingxi','lingxi-tasks':'lingxi','recharge-tasks':'recharge'}[page]||page);
  const workspaceFetch=window.fetch.bind(window);
  let featureReloading=false;
  window.fetch=async(...args)=>{
    const response=await workspaceFetch(...args);
    if(response.headers.get('X-Workspace-Access'))location.replace('/access?next='+encodeURIComponent(location.pathname+location.search));
    if(response.headers.get('X-Workspace-Feature-Disabled')===currentFeature&&!featureReloading){featureReloading=true;location.reload();}
    return response;
  };
  const localHost=['localhost','127.0.0.1','[::1]'].includes(location.hostname);
  if(new URLSearchParams(location.search).get('desktop')==='1')sessionStorage.setItem('desktop-client','1');
  if(localHost&&sessionStorage.getItem('desktop-client')==='1')import('/static/desktop-window.js');
  const favicon=document.querySelector('link[rel="icon"]')||document.createElement('link');
  favicon.rel='icon';favicon.href='/favicon.ico';document.head.append(favicon);
  const symbols={grid:'layout-dashboard',chat:'message-circle',star:'star',recharge:'calendar-clock',
    checkin:'calendar-check-2',monthly:'ticket-percent',wallet:'wallet-cards',rights:'badge-check',
    information:'file-search',users:'users-round',settings:'sliders-horizontal',menu:'menu',
    calendar:'calendar-days',coins:'coins'};
  const icon=name=>`<svg class="workspace-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="/static/navigation-icons.svg#${symbols[name]}"/></svg>`;
  const pages = [
    ['home','/','工作台'], ['chat','/lingxi','即时对话'],
    ['lingxi-tasks','/lingxi/tasks','定时发送'],
    ['star-day','/star-day','星动日'],
    ['recharge','/recharge/','周三充值日'],
    ['recharge-tasks','/recharge/?tab=tasks','周三充值日'],
    ['checkin','/checkin','签到有礼'],
    ['monthly-coupon','/monthly-coupon','每月话费券'],
    ['coupons','/coupons','我的卡券'],
    ['mobile-rights','/mobile-rights','移动权益'],
    ['bills','/information','信息查询'],
    ['platforms','/platforms','平台管理'],
  ];
  const groups = [
    {title:'灵犀对话',icon:'chat',keys:['chat','lingxi-tasks']},
  ];
  const checkinChannelTitle = {'/checkin/wechat':'微信渠道签到'}[location.pathname];
  const currentGroup = groups.find(group => group.keys.includes(page)) || (checkinChannelTitle ? {title:'签到有礼'} : null);
  const nav = document.createElement('aside'); nav.className = 'workspace-nav'; nav.id = 'workspace-navigation';
  nav.innerHTML = `<a class="workspace-brand" href="/" aria-label="本地工具台首页"><span class="workspace-logo"><svg viewBox="0 0 32 32" aria-hidden="true"><path fill="#84beff" d="m16 2 13 7-13 7L3 9Z"/><path fill="#398cff" d="m3 12 11 6v13L3 25Z"/><path fill="#b9daff" d="m18 18 11-6v13l-11 6Z"/></svg></span><span>本地工具台<small>移动账号与任务</small></span></a>
    <div class="workspace-nav-label">工作空间</div><nav aria-label="主导航"><a href="/" class="workspace-nav-item${page === 'home' ? ' active' : ''}"${page === 'home' ? ' aria-current="page"' : ''}>${icon('grid')}<span>工作台</span></a>
    ${groups.map(group => `<details class="workspace-nav-group${currentGroup === group ? ' contains-current' : ''}"><summary>${icon(group.icon)}<span>${group.title}</span><svg class="workspace-group-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m8 10 4 4 4-4"/></svg></summary><div class="workspace-subnav">${pages.filter(item => group.keys.includes(item[0])).map(([key,url,label]) => `<a href="${url}" class="workspace-subnav-item${page === key ? ' active' : ''}"${page === key ? ' aria-current="page"' : ''}>${label}</a>`).join('')}</div></details>${group.keys.includes('chat') ? `<a href="/star-day" class="workspace-nav-item${page === 'star-day' ? ' active' : ''}"${page === 'star-day' ? ' aria-current="page"' : ''}>${icon('star')}<span>星动日</span></a><a href="/recharge/" class="workspace-nav-item${['recharge','recharge-tasks'].includes(page) ? ' active' : ''}"${['recharge','recharge-tasks'].includes(page) ? ' aria-current="page"' : ''}>${icon('recharge')}<span>周三充值日</span></a>` : ''}`).join('')}
    <a href="/checkin" class="workspace-nav-item${page === 'checkin' ? ' active' : ''}"${page === 'checkin' ? ' aria-current="page"' : ''}>${icon('checkin')}<span>签到有礼</span></a>
    <a href="/monthly-coupon" class="workspace-nav-item${page === 'monthly-coupon' ? ' active' : ''}"${page === 'monthly-coupon' ? ' aria-current="page"' : ''}>${icon('monthly')}<span>每月话费券</span></a>
    <a href="/coupons" class="workspace-nav-item${page === 'coupons' ? ' active' : ''}"${page === 'coupons' ? ' aria-current="page"' : ''}>${icon('wallet')}<span>我的卡券</span></a>
    <a href="/mobile-rights" class="workspace-nav-item${page === 'mobile-rights' ? ' active' : ''}"${page === 'mobile-rights' ? ' aria-current="page"' : ''}>${icon('rights')}<span>移动权益</span></a>
    <a href="/information" class="workspace-nav-item${page === 'bills' ? ' active' : ''}"${page === 'bills' ? ' aria-current="page"' : ''}>${icon('information')}<span>信息查询</span></a>
    <div id="workspace-activity-navigation"></div></nav>
    <div class="workspace-nav-divider"></div><button type="button" class="workspace-nav-item${page === 'accounts' ? ' active' : ''}" data-account-window>${icon('users')}<span>账号管理</span></button>
    <a href="/platforms" class="workspace-nav-item${page === 'platforms' ? ' active' : ''}"${page === 'platforms' ? ' aria-current="page"' : ''}>${icon('settings')}<span>平台管理</span></a>
    <div class="workspace-nav-footer"><span class="workspace-service-dot"></span><div>本地服务<small id="workspace-host"></small></div><span class="workspace-private">本机</span></div>`;
  nav.querySelector('#workspace-host').textContent = location.host;
  nav.querySelector('.workspace-private').textContent = localHost?'本机':'远程';
  const closeNavigation=document.createElement('button');closeNavigation.type='button';closeNavigation.className='workspace-nav-close';closeNavigation.setAttribute('aria-label','收起导航');
  closeNavigation.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12 M18 6 6 18"/></svg>';
  nav.append(closeNavigation);
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
  closeNavigation.addEventListener('click',toggleNavigation);
  nav.addEventListener('click',event => { if(event.target.closest('[data-account-window]') && document.body.classList.contains('workspace-nav-open')) toggleNavigation(); });
  document.addEventListener('keydown',event => { if(event.key === 'Escape' && document.body.classList.contains('workspace-nav-open')) toggleNavigation(); });
  document.body.prepend(nav, topbar, shade);
  for (const link of nav.querySelectorAll('a.workspace-nav-item, a.workspace-subnav-item')) {
    link.dataset.issueKey = link.getAttribute('href');
  }
  nav.querySelector('.workspace-nav-group > summary').dataset.issueKey = '/lingxi/tasks';
  nav.querySelector('button[data-account-window]').dataset.issueKey = 'accounts';
  import('/static/updates.js');
  import('/static/network.js');
  import('/static/navigation-issues.js');
  import('/static/features.js');

  const seasonalNavigation = nav.querySelector('#workspace-activity-navigation');
  let seasonalTimer;
  function showSeasonalCampaign(platforms) {
    clearTimeout(seasonalTimer);
    const active = platforms.filter(platform => platform.active);
    const shown=active.filter(platform=>platform.navigation!=='checkin');
    const existing=new Map([...nav.querySelectorAll('a[data-platform-id]')].map(link=>[link.dataset.platformId,link]));
    for(const [id,link] of existing)if(!shown.some(platform=>platform.id===id))link.remove();
    for(const platform of shown){
      const present=existing.get(platform.id);
      if(present){present.querySelector('.workspace-activity-title').textContent=platform.title;continue;}
      const link = document.createElement('a'); link.href = platform.home;
      link.dataset.platformId=platform.id;
      link.dataset.issueKey = platform.home;
      link.className = 'workspace-nav-item'+(location.pathname === platform.home ? ' active' : '');
      if (location.pathname === platform.home) link.setAttribute('aria-current','page');
      link.innerHTML = icon(platform.home==='/daily-gold'?'coins':'calendar');
      const label = document.createElement('span');label.className='workspace-activity-title';label.textContent = platform.title; link.append(label);
      seasonalNavigation.parentElement.insertBefore(link,seasonalNavigation);
    }
    window.dispatchEvent(new CustomEvent('workspace-navigation-changed'));
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
