import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element{
  constructor(){this.children=[];this.handlers={};this.checked=false;this.disabled=false;this.hidden=false;this.classList={toggle(){}};}
  addEventListener(name,handler){this.handlers[name]=handler;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=node=>[node,...node.children.flatMap(descendants)];
async function fixture({managed=true,ipv6=true}={}){
  const nodes=new Map(),calls=[],events={};
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  let state={managed,settings:{ipv4:true,ipv6},active:{ipv4:true,ipv6},restart_required:false,port:5500,
    ipv4:['http://192.168.31.176:5500/','http://10.1.1.2:5500/','http://172.16.1.2:5500/'],
    ipv6:['http://[2409:8a6a::1234]:5500/'],local:['http://127.0.0.1:5500/','http://[::1]:5500/']};
  const fetch=async(url,options)=>{
    calls.push({url,options});
    if(options?.method==='POST')state={...state,settings:JSON.parse(options.body),restart_required:true,message:'监听设置已保存，重启服务后生效。'};
    return {ok:true,json:async()=>state};
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/network.js',import.meta.url),'utf8'),{
    document:{getElementById:get,createElement:()=>new Element()},window:{addEventListener:(name,fn)=>{events[name]=fn;}},fetch});
  await flush();return {get,calls,events};
}

test('sidebar displays LAN and properly bracketed IPv6 URLs with additional IPv4 addresses',async()=>{
  const {get,calls}=await fixture();
  const links=descendants(get('workspace-addresses')).filter(node=>node.href).map(node=>node.href);
  assert.ok(links.includes('http://192.168.31.176:5500/'));
  assert.ok(links.includes('http://[2409:8a6a::1234]:5500/'));
  assert.ok(links.includes('http://172.16.1.2:5500/'));
  assert.equal(calls.length,1);
});

test('saving persists independent switches and explains the pending service restart',async()=>{
  const {get,calls}=await fixture();
  get('network-ipv4').checked=false;get('network-ipv4').handlers.change();
  await get('network-settings-form').handlers.submit({preventDefault(){}});
  const request=calls.find(call=>call.options?.method==='POST');
  assert.deepEqual(JSON.parse(request.options.body),{ipv4:false,ipv6:true});
  assert.match(get('network-description').textContent,/重启服务后生效/);
  assert.equal(get('network-save').disabled,false);
});

test('focus refresh keeps an unsaved checkbox draft and never submits settings',async()=>{
  const {get,calls,events}=await fixture();
  get('network-ipv4').checked=false;get('network-ipv4').handlers.change();
  await events.focus();
  assert.equal(get('network-ipv4').checked,false);
  assert.equal(calls.some(call=>call.options?.method==='POST'),false);
});

test('environment-controlled listeners disable settings and an off family is labelled correctly',async()=>{
  const {get,calls}=await fixture({managed:false,ipv6:false});
  assert.equal(get('network-ipv4').disabled,true);assert.equal(get('network-ipv6').disabled,true);
  assert.equal(get('network-save').disabled,true);
  await get('network-settings-form').handlers.submit({preventDefault(){}});
  assert.equal(calls.length,1);
  assert.equal(descendants(get('workspace-addresses')).some(node=>node.href?.includes('2409')),false);
});
