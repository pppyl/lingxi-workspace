"use strict";
import('/static/portal-controller.js').then(({mountPortal}) => {
  mountPortal(document.body, {manager: document.body.dataset.page === 'accounts'});
}).catch(() => {
  document.getElementById('portal-feedback').textContent = '页面加载失败，请刷新重试。';
});
