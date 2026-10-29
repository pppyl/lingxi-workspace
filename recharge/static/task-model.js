/* Task presentation rules shared by the management page and its offline tests. */
  'use strict';
  const active = new Set(['preparing', 'armed', 'running', 'cancelling']);
  const problems = new Set(['failed', 'interrupted', 'missed']);
  const labels = {waiting:'已预约', preparing:'正在准备', armed:'等待到点', running:'正在执行',
    cancelling:'正在停止', cancelled:'已停止', success:'领取成功', exhausted:'已达尝试上限',
    failed:'需要处理', interrupted:'执行中断', missed:'错过时间', completed:'本场已结束'};
  function group(job, account = {}) {
    if (active.has(job.state)) return 'running';
    if (job.enabled && !account.authenticated || problems.has(job.state) && !job.enabled) return 'attention';
    return job.enabled ? 'scheduled' : 'ended';
  }
  function status(job, account) {
    if (job.enabled && !active.has(job.state)) {
      if (account.authenticated) return '已预约';
      return {restoring:'正在自动恢复', retry:'等待自动重连', login_required:'需要短信验证'}[account.session_status] ||
        (account.mode === 'none' ? '待连接共享账号' : '待恢复登录');
    }
    return labels[job.state] || '未知状态';
  }
  function stoppable(job) { return job.state !== 'cancelling' && (job.enabled || active.has(job.state)); }
  function rows(data) {
    const accounts = new Map((data.accounts || []).map(a => [a.id, a]));
    return Object.entries(data.jobs || {}).map(([id, job]) => {
      const account = accounts.get(id) || {id, name:'原账号已移除', authenticated:false};
      return {id, job, account, group:group(job, account), status:status(job, account)};
    });
  }
  function counts(items) {
    const result = {all:items.length, scheduled:0, running:0, attention:0, ended:0};
    items.forEach(row => result[row.group]++); return result;
  }
  function visible(items, filter, query, sort) {
    const needle = query.trim().toLocaleLowerCase();
    const result = items.filter(row => (filter === 'all' || row.group === filter) &&
      (!needle || [row.account.name, row.account.phone, row.status, row.job.message, labels[row.job.state],
        row.job.weekly ? '每周三重复' : '单次执行'].join(' ').toLocaleLowerCase().includes(needle)));
    return result.sort((a, b) => {
      if (sort === 'name') return (a.account.name || '').localeCompare(b.account.name || '', 'zh-CN');
      if (sort === 'updated') return (Date.parse(b.job.updated_at) || 0) - (Date.parse(a.job.updated_at) || 0);
      const rank = r => active.has(r.job.state) ? 0 : r.job.enabled ? 1 : 2;
      return rank(a) - rank(b) || (a.job.enabled ?
        (Date.parse(a.job.next_run_at) || Infinity) - (Date.parse(b.job.next_run_at) || Infinity) :
        (Date.parse(b.job.updated_at) || 0) - (Date.parse(a.job.updated_at) || 0));
    });
  }
  function beijingInput(value) { return new Date(new Date(value).getTime() + 8 * 3600000).toISOString().slice(0,19); }
  function nextWednesday(now = Date.now(), previous) {
    const date = new Date(now + 8 * 3600000);
    const prior = previous ? new Date(Date.parse(previous) + 8 * 3600000) : null;
    date.setUTCDate(date.getUTCDate() + (3 - date.getUTCDay() + 7) % 7);
    date.setUTCHours(prior && Number.isFinite(prior.getTime()) ? prior.getUTCHours() : 8,
      prior && Number.isFinite(prior.getTime()) ? prior.getUTCMinutes() : 0, 0, 0);
    if (date.getTime() <= now + 8 * 3600000) date.setUTCDate(date.getUTCDate() + 7);
    return date.toISOString().slice(0,19);
  }
  function validate(settings, now = Date.now()) {
    const target = Date.parse(settings.run_at + '+08:00');
    if (!Number.isFinite(target) || target <= now || new Date(target + 8 * 3600000).getUTCDay() !== 3)
      return '请选择未来某个周三的执行时间（北京时间）。';
    if (!(settings.interval_min >= 0.2 && settings.interval_min <= settings.interval_max && settings.interval_max <= 1))
      return '间隔须在 0.2～1 秒内，最小间隔不能大于最大间隔。';
    if (!Number.isInteger(settings.max_attempts) || settings.max_attempts < 1 || settings.max_attempts > 20)
      return '最多尝试次数须为 1～20 的整数。';
    return '';
  }
export {active, labels, group, status, stoppable, rows, counts, visible, beijingInput, nextWednesday, validate};
