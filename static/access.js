(() => {
  'use strict';
  const $=id=>document.getElementById(id), form=$('access-form');
  if(!form)return;
  const settings=form.dataset.view==='settings', submit=$('access-submit');
  let configured=false, local=false, busy=false, deadline=0, timer;
  function destination(){
    const target=new URL(new URLSearchParams(location.search).get('next')||'/',location.origin);
    return target.origin===location.origin&&target.pathname!=='/access'?target.pathname+target.search+target.hash:'/';
  }
  function feedback(message,error=true){const node=$('access-feedback');node.textContent=message;node.hidden=!message;node.classList.toggle('success',!error);}
  function countdown(seconds=0){
    if(seconds)deadline=Date.now()+seconds*1000;
    clearTimeout(timer);
    const remaining=Math.max(0,Math.ceil((deadline-Date.now())/1000));
    submit.disabled=busy||remaining>0;
    submit.textContent=remaining?`${remaining} 秒后可重试`:busy?'正在处理…':settings?'保存访问密码':configured?'登录':'设置密码并进入';
    if(remaining)timer=setTimeout(()=>countdown(),250);
  }
  function render(state){
    configured=state.configured;local=state.local;
    if(!settings&&configured&&(state.local||state.authenticated)){location.replace(destination());return;}
    $('access-title').textContent=settings?'访问密码':configured?'远程访问登录':'设置访问密码';
    $('access-description').textContent=settings?'非本机访问需要密码。本机回环访问可直接使用。':configured?'请输入访问密码，继续使用本地工具台。':'首次访问尚未设置密码。请先完成设置，再进入工作台。';
    $('access-current-field').hidden=!settings||!configured;
    $('access-current-password').required=settings&&configured;
    $('access-confirmation-field').hidden=!settings&&configured;
    $('access-confirmation').required=settings||!configured;
    $('access-password-label').textContent=settings&&configured?'新访问密码':configured?'访问密码':'访问密码（至少 8 位）';
    $('access-password').autocomplete=settings||!configured?'new-password':'current-password';
    $('access-password').minLength=settings||!configured?8:1;
    if($('access-logout'))$('access-logout').hidden=state.local||!state.authenticated;
    form.hidden=false;countdown(state.retry_after);
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||Date.now()<deadline)return;
    busy=true;countdown();feedback('');
    const login=!settings&&configured;
    const payload=login?{password:$('access-password').value}:{password:$('access-password').value,confirmation:$('access-confirmation').value,current_password:$('access-current-password').value};
    try{
      const response=await fetch(login?'/api/access/login':'/api/access/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result=await response.json();
      if(!response.ok){if(result.retry_after)deadline=Date.now()+result.retry_after*1000;throw new Error(result.error||'暂时无法完成，请稍后重试。');}
      if(!settings){location.replace(destination());return;}
      form.reset();render({configured:true,local,authenticated:true,retry_after:0});feedback(result.message,false);
    }catch(error){feedback(error.message);}
    finally{busy=false;countdown();}
  });
  $('access-logout')?.addEventListener('click',async()=>{
    await fetch('/api/access/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    location.replace('/access');
  });
  fetch('/api/access/status').then(async response=>{if(!response.ok)throw new Error('无法读取访问设置，请刷新页面。');render(await response.json());})
    .catch(error=>{$('access-description').textContent=error.message;});
})();
