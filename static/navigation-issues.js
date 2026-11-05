(() => {
  'use strict';
  const nav = document.getElementById('workspace-navigation');
  if (!nav) return;
  let counts = {}, loading = false, pending = false;

  function render() {
    for (const item of nav.querySelectorAll('[data-issue-key]')) {
      const count = counts[item.dataset.issueKey] || 0;
      let badge = item.querySelector('.workspace-issue-badge');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'workspace-issue-badge';
        badge.setAttribute('role', 'status');
        badge.setAttribute('aria-live', 'polite');
        item.insertBefore(badge, item.querySelector('.workspace-group-chevron'));
      }
      badge.hidden = count === 0;
      badge.textContent = String(count);
      badge.title = `${count} 个失败或待处理问题`;
      badge.setAttribute('aria-label', badge.title);
    }
  }

  async function refresh() {
    if (document.hidden) return;
    if (loading) { pending = true; return; }
    loading = true;
    try {
      const response = await fetch('/api/navigation/issues');
      if (!response.ok) return;
      counts = (await response.json()).counts;
      render();
    } catch { /* Retain the last confirmed counts while reconnecting. */ }
    finally {
      loading = false;
      if (pending) { pending = false; refresh(); }
    }
  }

  window.addEventListener('workspace-navigation-changed', render);
  window.addEventListener('focus', refresh);
  window.addEventListener('shared-accounts-changed', refresh);
  window.addEventListener('accounts-changed', refresh);
  document.addEventListener('visibilitychange', refresh);
  setInterval(refresh, 10000);
  refresh();
})();
