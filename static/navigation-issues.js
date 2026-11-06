(() => {
  'use strict';
  const nav = document.getElementById('workspace-navigation');
  if (!nav) return;
  let counts = {}, couponTotal = 0, loading = false, pending = false;

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
      if(item.dataset.issueKey === '/coupons'){
        let coupons=item.querySelector('.workspace-coupon-badge');
        if(!coupons){
          coupons=document.createElement('span');coupons.className='workspace-coupon-badge';
          coupons.setAttribute('role','status');coupons.setAttribute('aria-live','polite');
          item.insertBefore(coupons,badge);
        }
        coupons.hidden=couponTotal===0;coupons.textContent=String(couponTotal);
        coupons.title=`${couponTotal} 张可用优惠话费券（兑换券及充值低于9.1折的券），按最近查询统计`;
        coupons.setAttribute('aria-label',coupons.title);
      }
    }
  }

  async function refresh() {
    if (document.hidden) return;
    if (loading) { pending = true; return; }
    loading = true;
    try {
      const response = await fetch('/api/navigation/issues');
      if (!response.ok) return;
      const result=await response.json();counts=result.counts;couponTotal=result.coupon_reminders?.total||0;
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
  window.addEventListener('coupon-reminders-changed', refresh);
  document.addEventListener('visibilitychange', refresh);
  setInterval(refresh, 10000);
  refresh();
})();
