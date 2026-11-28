"use strict";
import('/static/portal-controller.js?v=app-login-independent-1').then(({mountPortal}) => {
  mountPortal(document.body, {manager: document.body.dataset.page === 'accounts'});
}).catch(() => {
  document.getElementById('portal-feedback').textContent = '页面加载失败，请刷新重试。';
});
