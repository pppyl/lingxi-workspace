import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this.hidden=false;this.flags={};this.classList={toggle:(key,value)=>this.flags[key]=value};}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...items){this.children.push(...items);}
  prepend(...items){this.children.unshift(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(key,value){this.attrs[key]=value;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  click(){if(!this.disabled)return this.handlers.click?.();}
}
const find=(node,predicate)=>predicate(node)?node:node.children.map(child=>find(child,predicate)).find(Boolean);
const cls=(node,name)=>find(node,item=>item.className?.split(' ').includes(name));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function status(available=true){
  const asset={platform:'windows',name:'Windows.exe',url:'https://github.com/pppyl/lingxi-workspace/releases/download/v1.4.0/Windows.exe'};
  return {current_version:'1.3.0',platform:'windows',available,checking:false,checked_at:1000,retry_after:0,error:'',
    release:{version:available?'1.4.0':'1.3.0',notes:'<script>unsafe()</script>\n更新说明',assets:[asset],url:'https://github.com/pppyl/lingxi-workspace/releases/tag/v1.4.0'},
    recommended:asset,releases_url:'https://github.com/pppyl/lingxi-workspace/releases'};
}
async function fixture(initial=status(),store=new Map()){
  const body=new Element('body'),head=new Element('head'),top=new Element(),panel=new Element();
  const calls=[],replies=[initial],timers=[];
  const fetch=async(url,options)=>{calls.push({url,options});return{ok:true,json:async()=>replies.shift()};};
  vm.runInNewContext(fs.readFileSync(new URL('../static/updates.js',import.meta.url),'utf8'),{
    document:{body,head,hidden:false,createElement:tag=>new Element(tag),querySelector:()=>top,getElementById:()=>panel},
    localStorage:{getItem:key=>store.get(key),setItem:(key,value)=>store.set(key,value)},
    fetch,Date,setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout(){},
  });
  await flush();
  return {body,head,top,panel,calls,replies,timers,store,notice:cls(body,'update-notice'),dialog:cls(body,'update-dialog')};
}

test('new release adds a visible notice, current version badge and correct download button',async()=>{
  const ui=await fixture();
  assert.equal(ui.top.children[0].textContent,'v1.3.0');
  assert.equal(ui.top.children[0].flags.available,true);
  assert.equal(ui.notice.hidden,false);
  assert.match(ui.notice.children[1].textContent,/1.4.0/);
  const download=cls(ui.dialog,'update-primary');
  assert.match(download.href,/github\.com\/pppyl\/lingxi-workspace\/releases\/download/);
  assert.match(download.textContent,/下载更新/);
  assert.match(cls(ui.dialog,'update-guide').textContent,/先从托盘退出/);
  assert.equal(cls(ui.dialog,'update-notes').textContent,'<script>unsafe()</script>\n更新说明');
  assert.equal(cls(ui.dialog,'update-notes').innerHTML,undefined);
  assert.equal(ui.calls.length,1);assert.equal(ui.calls[0].url,'/api/updates');
});

test('no update does not show a prompt or update download action',async()=>{
  const ui=await fixture(status(false));
  assert.equal(ui.notice.hidden,true);
  assert.equal(ui.top.children[0].flags.available,false);
  assert.match(cls(ui.panel,'update-message').textContent,/最新正式版本/);
  assert.equal(cls(ui.dialog,'update-downloads').children.length,0);
});

test('dismissal is remembered by version while the version badge remains available',async()=>{
  const store=new Map(),ui=await fixture(status(),store);
  cls(ui.notice,'update-icon-close').click();
  assert.equal(ui.notice.hidden,true);
  const again=await fixture(status(),store);
  assert.equal(again.notice.hidden,true);
  assert.equal(again.top.children[0].flags.available,true);
  again.top.children[0].click();assert.equal(again.dialog.open,true);
});

test('manual check uses one POST, then polls only cached local status until complete',async()=>{
  const ui=await fixture(status(false));
  ui.replies.push({...status(false),checking:true});
  cls(ui.panel,'update-secondary').click();await flush();
  assert.equal(ui.calls.at(-1).url,'/api/updates/check');
  assert.equal(ui.calls.at(-1).options.method,'POST');
  assert.equal(cls(ui.panel,'update-secondary').disabled,true);
  ui.replies.push(status());
  await ui.timers.at(-1).fn();await flush();
  assert.equal(ui.calls.at(-1).url,'/api/updates');
  assert.equal(ui.notice.hidden,false);
});

test('failed checks show a useful network message and never claim to be current',async()=>{
  const ui=await fixture({...status(false),release:null,checked_at:0,error:'暂时无法连接 GitHub，请检查网络后重试。'});
  const message=cls(ui.panel,'update-message');
  assert.match(message.textContent,/无法连接 GitHub/);
  assert.doesNotMatch(message.textContent,/已是最新/);
  assert.equal(message.flags.error,true);
});
