"use strict";
const accountChannel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('shared-app-accounts') : null;
const managerStyles = document.createElement('link');
managerStyles.rel = 'stylesheet'; managerStyles.href = '/static/account-manager.css';
document.head.append(managerStyles);
let managerDialog, managerReady, sharedRelogin;
let managerOpenSequence = 0;
function finishSharedRelogin(account=null) {
  const request=sharedRelogin;
  if(!request||account&&request.id!==account.id)return;
  sharedRelogin=null;
  if(account&&managerDialog.open)managerDialog.close();
  request.resolve(account);
}
function openAccountManager({reloginId = '', focusId = '', message = ''} = {}) {
  if (!managerDialog) {
    managerDialog = document.createElement('dialog');
    managerDialog.className = 'account-manager-dialog';
    managerDialog.setAttribute('aria-labelledby', 'account-manager-title');
    const loading = document.createElement('p'); loading.className = 'account-manager-loading'; loading.textContent = '正在读取账号管理…';
    managerDialog.append(loading); document.body.append(managerDialog);
    managerDialog.addEventListener('click', event => {
      if (event.target === managerDialog) managerDialog.close();
    });
    managerDialog.addEventListener('close', () => {
      if(!managerDialog.open)finishSharedRelogin();
      window.dispatchEvent(new Event('accounts-changed'));
    });
    managerReady = Promise.all([
      fetch('/accounts').then(response => {
        if (!response.ok) throw new Error('账号管理加载失败，请刷新页面重试。');
        return response.text();
      }),
      import('/static/portal-controller.js?v=app-login-independent-1'),
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
      const manager = mountPortal(root, {manager:true, embedded:true, onLogin:finishSharedRelogin,
        onReloginCancel:id=>{if(sharedRelogin?.id===id)managerDialog.close();}});
      managerDialog.setAttribute('aria-labelledby', 'modal-account-manager-title');
      return manager;
    }).catch(error => { loading.textContent = error.message; });
  } else if (!reloginId && !focusId) {
    managerReady.then(manager => { if (manager) manager.refresh(); });
  }
  if(!managerDialog.open)managerDialog.showModal();
  const sequence=++managerOpenSequence;
  managerReady.then(manager=>{
    if(!manager||!managerDialog.open||sequence!==managerOpenSequence)return;
    if(reloginId)manager.relogin(reloginId);
    else if(focusId)manager.focus(focusId,message);
  });
}
window.requestSharedRelogin = id => new Promise(resolve=>{
  finishSharedRelogin();
  sharedRelogin={id,resolve};
  openAccountManager({reloginId:id});
});
document.addEventListener('click', event => {
  const button = event.target.closest('[data-account-window]');
  if (!button) return;
  event.preventDefault();
  openAccountManager({reloginId:button.dataset.accountRelogin});
});
window.addEventListener('open-account-manager', event => {
  openAccountManager({reloginId:event.detail.reloginId,focusId:event.detail.loginId,message:event.detail.message});
});
window.publishAccountsChanged = () => {
  accountChannel?.postMessage({type: 'accounts-changed'});
  window.dispatchEvent(new Event('accounts-changed'));
};
if (accountChannel) accountChannel.onmessage = event => {
  if (event.data?.type === 'accounts-changed') window.dispatchEvent(new Event('accounts-changed'));
};
