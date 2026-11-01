import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(){this.hidden=false;this.value='';this.handlers={};this.dataset={};this.classList={toggle(){}};}
  addEventListener(name,fn){this.handlers[name]=fn;}
  reset(){this.resetCalled=true;}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function fixture({configured=false,local=false,authenticated=false,retry_after=0,settings=false,next='/coupons'}={}){
  const elements=new Map(),calls=[],replies=[],timers=[],redirects=[],get=id=>{
    if(!elements.has(id))elements.set(id,new Element());return elements.get(id);
  };
  get('access-form').dataset.view=settings?'settings':'login';
  let now=1000000;
  class Clock extends Date {static now(){return now;}}
  const fetch=async(url,options)=>{
    calls.push({url,options});
    return url==='/api/access/status'?{ok:true,json:async()=>({configured,local,authenticated,retry_after})}:replies.shift();
  };
  const context={document:{getElementById:get},location:{origin:'http://192.168.31.176:5500',search:'?next='+encodeURIComponent(next),replace:value=>redirects.push(value)},
    URL,URLSearchParams,Date:Clock,fetch,setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){}};
  vm.runInNewContext(fs.readFileSync(new URL('../static/access.js',import.meta.url),'utf8'),context);
  await flush();
  return {get,calls,replies,timers,redirects,advance(seconds){now+=seconds*1000;timers.at(-1)?.();},
    submit:()=>get('access-form').handlers.submit({preventDefault(){}})};
}

test('first remote visit forces password setup and continues to requested business page',async()=>{
  const ui=await fixture();
  assert.equal(ui.get('access-title').textContent,'设置访问密码');
  assert.equal(ui.get('access-confirmation-field').hidden,false);
  assert.equal(ui.get('access-password').minLength,8);
  ui.get('access-password').value='test-password-123';ui.get('access-confirmation').value='test-password-123';
  ui.replies.push({ok:true,json:async()=>({message:'saved'})});
  await ui.submit();
  assert.equal(ui.calls.at(-1).url,'/api/access/password');
  assert.equal(JSON.parse(ui.calls.at(-1).options.body).confirmation,'test-password-123');
  assert.deepEqual(ui.redirects,['/coupons']);
});

test('configured remote access logs in and persists the server cooldown across page refresh',async()=>{
  const ui=await fixture({configured:true,retry_after:10});
  assert.equal(ui.get('access-confirmation-field').hidden,true);
  assert.equal(ui.get('access-submit').disabled,true);
  assert.match(ui.get('access-submit').textContent,/10 秒/);
  await ui.submit();assert.equal(ui.calls.length,1);
  ui.advance(10);assert.equal(ui.get('access-submit').disabled,false);
  ui.get('access-password').value='wrong';
  ui.replies.push({ok:false,json:async()=>({error:'访问密码错误',retry_after:30})});
  await ui.submit();assert.equal(ui.calls.at(-1).url,'/api/access/login');
  assert.equal(ui.get('access-submit').disabled,true);
  assert.match(ui.get('access-submit').textContent,/30 秒/);
  assert.deepEqual(ui.redirects,[]);
  ui.advance(30);ui.get('access-password').value='correct-password';
  ui.replies.push({ok:true,json:async()=>({})});await ui.submit();
  assert.deepEqual(ui.redirects,['/coupons']);
});

test('platform password settings require current password, retain the page and allow logout',async()=>{
  const ui=await fixture({configured:true,authenticated:true,settings:true});
  assert.equal(ui.get('access-current-password').required,true);
  assert.equal(ui.get('access-current-field').hidden,false);
  assert.equal(ui.get('access-logout').hidden,false);
  ui.get('access-current-password').value='old-password';ui.get('access-password').value='new-password';ui.get('access-confirmation').value='new-password';
  ui.replies.push({ok:true,json:async()=>({message:'密码已保存'})});await ui.submit();
  assert.equal(JSON.parse(ui.calls.at(-1).options.body).current_password,'old-password');
  assert.deepEqual(ui.redirects,[]);assert.equal(ui.get('access-form').resetCalled,true);
  assert.equal(ui.get('access-feedback').textContent,'密码已保存');
  ui.replies.push({ok:true});await ui.get('access-logout').handlers.click();
  assert.equal(ui.calls.at(-1).url,'/api/access/logout');assert.deepEqual(ui.redirects,['/access']);
});

test('authenticated local entry returns to work and external redirect targets are rejected',async()=>{
  const ui=await fixture({configured:true,local:true,next:'https://evil.test/'});
  assert.deepEqual(ui.redirects,['/']);
  const signed=await fixture({configured:true,authenticated:true,next:'/recharge/?tab=tasks'});
  assert.deepEqual(signed.redirects,['/recharge/?tab=tasks']);
});
