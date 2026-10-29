"use strict";
const accountChannel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('shared-app-accounts') : null;
const managerStyles = document.createElement('link');
managerStyles.rel = 'stylesheet'; managerStyles.href = '/static/account-manager.css';
document.head.append(managerStyles);
let managerDialog, managerReady;
document.addEventListener('click', event => {
  const button = event.target.closest('[data-account-window]');
  if (!button) return;
  event.preventDefault();
  const reloginId = button.dataset.accountRelogin;
  if (!managerDialog) {
    managerDialog = document.createElement('dialog');
    managerDialog.className = 'account-manager-dialog';
    managerDialog.setAttribute('aria-labelledby', 'account-manager-title');
    const loading = document.createElement('p'); loading.className = 'account-manager-loading'; loading.textContent = '正在读取账号管理…';
    managerDialog.append(loading); document.body.append(managerDialog);
    managerDialog.addEventListener('click', event => {
      if (event.target === managerDialog) managerDialog.close();
    });
    managerDialog.addEventListener('close', () => window.dispatchEvent(new Event('accounts-changed')));
    managerReady = Promise.all([
      fetch('/accounts').then(response => {
        if (!response.ok) throw new Error('账号管理加载失败，请刷新页面重试。');
        return response.text();
      }),
      import('/static/portal-controller.js'),
    ]).then(([html, {mountPortal}]) => {
      const root = new DOMParser().parseFromString(html, 'text/html').querySelector('.account-shell');
      const header = root.querySelector('.portal-header');
      header.replaceChildren();
      const title = document.createElement('h2'); title.id = 'account-manager-title'; title.textContent = '账号管理';
      const close = document.createElement('button'); close.type = 'button'; close.className = 'account-manager-close';
      close.textContent = '×'; close.setAttribute('aria-label', '关闭账号管理');
      close.addEventListener('click', () => managerDialog.close());
      header.append(title, close);
      root.querySelector('.manager-intro').textContent = '所有功能共用账号，登录、修改备注和退出都在这里完成。';
      managerDialog.replaceChildren(root);
      const manager = mountPortal(root, {manager: true, embedded: true});
      managerDialog.setAttribute('aria-labelledby', 'modal-account-manager-title');
      return manager;
    }).catch(error => { loading.textContent = error.message; });
  } else if (!reloginId) {
    managerReady.then(manager => { if (manager) manager.refresh(); });
  }
  managerDialog.showModal();
  if (reloginId) managerReady.then(manager => { if (manager) manager.relogin(reloginId); });
});
window.publishAccountsChanged = () => {
  accountChannel?.postMessage({type: 'accounts-changed'});
  window.dispatchEvent(new Event('accounts-changed'));
};
if (accountChannel) accountChannel.onmessage = event => {
  if (event.data?.type === 'accounts-changed') window.dispatchEvent(new Event('accounts-changed'));
};
