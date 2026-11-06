/* Presentation only: keep existing rows, controls and business handlers intact. */
(() => {
  'use strict';
  if (!document.body.classList.contains('workspace-app')) return;
  const strips = '#bills-accounts,#coupons-accounts,#home-accounts,#chat-account-list,#rights-accounts';
  const selection = new WeakMap(), pending = new Set();
  const phone = matchMedia('(max-width: 900px)');
  let frame = 0;

  function adaptTable(table) {
    const headers = [...table.querySelectorAll(':scope > thead > tr:first-child > th')];
    if (!headers.length) return;
    const records = headers.length >= 5 || table.classList.contains('dashboard-table');
    table.classList.toggle('mobile-record-table', records);
    table.classList.toggle('mobile-compact-table', !records);
    if (!records) return;
    const labels = headers.map(header => header.textContent.trim() || header.getAttribute('aria-label') || '选择');
    for (const row of table.querySelectorAll(':scope > tbody > tr')) {
      const cells = [...row.children];
      row.classList.toggle('mobile-empty-row', cells.length === 1 && cells[0].colSpan > 1);
      let primary = false;
      cells.forEach((cell, index) => {
        const label = labels[index] || '';
        cell.dataset.mobileLabel = label;
        const select = index === 0 && (label === '选' || label === '选择') && Boolean(cell.querySelector('input[type="checkbox"]'));
        cell.classList.toggle('mobile-select-cell', select);
        cell.classList.toggle('mobile-primary-cell', !primary && !select);
        if (!select) primary = true;
        cell.classList.toggle('mobile-wide-cell', /操作|内容|回复|结果|说明|会话信息|额外领任务|单账号设置/.test(label));
      });
    }
  }

  function adaptStrip(strip) {
    strip.classList.add('mobile-account-strip');
    if (!phone.matches) return;
    const active = strip.querySelector('[aria-pressed="true"],.selected');
    const key = active && (active.dataset.loginId || active.querySelector('[data-login-id]')?.dataset.loginId || active.querySelector('strong')?.textContent || active.textContent);
    if (!active || selection.get(strip) === key) return;
    selection.set(strip, key);
    const offset = active.getBoundingClientRect().left - strip.getBoundingClientRect().left;
    strip.scrollLeft += offset - (strip.clientWidth - active.clientWidth) / 2;
  }

  function flush() {
    frame = 0;
    for (const element of pending) {
      if (element.isConnected) element.matches('table') ? adaptTable(element) : adaptStrip(element);
    }
    pending.clear();
  }

  function collect(root) {
    if (root.nodeType !== 1) return;
    const parent = root.closest(`table,${strips}`);
    if (parent) pending.add(parent);
    for (const element of root.querySelectorAll(`table,${strips}`)) pending.add(element);
    if (!frame && pending.size) frame = requestAnimationFrame(flush);
  }
  collect(document.body);
  const observer = new MutationObserver(records => {
    for (const record of records) {
      collect(record.target);
      for (const added of record.addedNodes) collect(added);
    }
  });
  observer.observe(document.body, {childList:true, subtree:true});
  phone.addEventListener('change', () => document.querySelectorAll(strips).forEach(strip => {
    selection.delete(strip); adaptStrip(strip);
  }));
})();
