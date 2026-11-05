import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element{
  constructor(){this.children=[];this.handlers={};this.checked=false;this.disabled=false;this.hidden=false;this.style={};this.classList={toggle(){}};}
  addEventListener(name,handler){this.handlers[name]=handler;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  select(){this.selected=true;}
  remove(){this.removed=true;}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=node=>[node,...node.children.flatMap(descendants)];
async function fixture({managed=true,ipv6=true,clipboard=true}={}){
  const nodes=new Map(),calls=[],events={},copied=[],timers=[],body=new Element();
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  let state={managed,settings:{ipv4:true,ipv6,custom:false,addresses:[]},active:{ipv4:true,ipv6},restart_required:false,port:5500,
    ipv4:['http://192.168.31.176:5500/','http://10.1.1.2:5500/','http://172.16.1.2:5500/'],
    ipv6:['http://[2409:8a6a::1234]:5500/'],local:['http://127.0.0.1:5500/','http://[::1]:5500/']};
  state.listening={ipv4:state.ipv4,ipv6:ipv6?state.ipv6:[]};state.unavailable=[];
  state.addresses=[...state.ipv4.map(url=>({url,address:new URL(url).hostname,family:'ipv4',available:true,listening:true})),
    {url:state.ipv6[0],address:'2409:8a6a::1234',family:'ipv6',available:true,listening:ipv6}];
  const fetch=async(url,options)=>{
    calls.push({url,options});
    if(options?.method==='POST')state={...state,settings:JSON.parse(options.body),applying:true,restart_required:false,message:'监听设置已保存，正在更新监听，无需重启。'};
    return {ok:true,json:async()=>state};
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/network.js',import.meta.url),'utf8'),{
    document:{getElementById:get,createElement:()=>new Element(),body,execCommand(command){assert.equal(command,'copy');assert.ok(body.children.at(-1).selected);copied.push(body.children.at(-1).value);return true;}},window:{addEventListener:(name,fn)=>{events[name]=fn;}},fetch,
    navigator:clipboard?{clipboard:{async writeText(value){copied.push(value);}}}:{},setTimeout(){},setInterval(fn,ms){timers.push({fn,ms});}});
  await flush();return {get,calls,events,copied,body,timers,setState(update){state={...state,...update};}};
}

test('sidebar displays LAN and properly bracketed IPv6 URLs with additional IPv4 addresses',async()=>{
  const {get,calls}=await fixture();
  const links=descendants(get('workspace-addresses')).filter(node=>node.href).map(node=>node.href);
  assert.ok(links.includes('http://192.168.31.176:5500/'));
  assert.ok(links.includes('http://[2409:8a6a::1234]:5500/'));
  assert.ok(links.includes('http://172.16.1.2:5500/'));
  assert.equal(calls.length,1);
});

test('sidebar uses compact addresses while copying the full IPv6 URL with its port',async()=>{
  const {get,copied}=await fixture();
  const rows=descendants(get('workspace-addresses')).filter(node=>node.className==='network-endpoint');
  const row=rows.find(node=>node.children[1].children[0].textContent==='IPv6');
  const [,info,copy]=row.children,link=info.children[1];
  assert.equal(link.textContent,'[2409:8a6a::1234]:5500');
  assert.equal(link.title,'http://[2409:8a6a::1234]:5500/');
  assert.equal(copy.ariaLabel,'复制 IPv6 访问地址');
  await copy.handlers.click();
  assert.deepEqual(copied,['http://[2409:8a6a::1234]:5500/']);
  assert.equal(copy.textContent,'已复制');
  assert.ok(descendants(get('workspace-addresses')).some(node=>node.href==='/platforms#network-settings-form'));
});

test('copy works on HTTP LAN pages without the secure clipboard API',async()=>{
  const {get,copied,body}=await fixture({clipboard:false});
  const copy=descendants(get('workspace-addresses')).find(node=>node.ariaLabel==='复制 IPv4 访问地址');
  await copy.handlers.click();
  assert.deepEqual(copied,['http://192.168.31.176:5500/']);
  assert.equal(copy.textContent,'已复制');
  assert.equal(body.children.at(-1).removed,true);
});

test('saving persists independent switches and reports live reconfiguration',async()=>{
  const {get,calls}=await fixture();
  get('network-ipv4').checked=false;get('network-ipv4').handlers.change();
  await get('network-settings-form').handlers.submit({preventDefault(){}});
  const request=calls.find(call=>call.options?.method==='POST');
  assert.deepEqual(JSON.parse(request.options.body),{ipv4:false,ipv6:true,custom:false,addresses:[]});
  assert.match(get('network-description').textContent,/正在更新监听 · 无需重启/);
  assert.equal(get('network-save').disabled,false);
});

test('periodic local refresh updates changed IPs and selected checkboxes without saving or restarting',async()=>{
  const {get,calls,timers,setState}=await fixture();
  const url='http://192.168.50.20:5500/';
  setState({settings:{ipv4:true,ipv6:true,custom:true,addresses:['192.168.50.20']},
    ipv4:[url],listening:{ipv4:[url],ipv6:[]},
    addresses:[{address:'192.168.50.20',url,family:'ipv4',interface_name:'WLAN',available:true,listening:true}]});
  await timers.find(t=>t.ms===5000).fn();await flush();
  const links=descendants(get('workspace-addresses')).filter(n=>n.href).map(n=>n.href);
  assert.ok(links.includes(url));assert.ok(!links.includes('http://192.168.31.176:5500/'));
  const selected=descendants(get('network-address-list')).filter(n=>n.type==='checkbox'&&n.checked);
  assert.deepEqual(selected.map(n=>n.value),['192.168.50.20']);
  assert.equal(calls.some(c=>c.options?.method==='POST'),false);
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

test('custom selection submits only checked addresses while keeping IPv6 independently selected',async()=>{
  const {get,calls}=await fixture();
  let inputs=descendants(get('network-address-list')).filter(node=>node.type==='checkbox');
  assert.ok(inputs.every(input=>input.disabled));
  get('network-custom').checked=true;get('network-custom').handlers.change();
  for(const input of inputs)if(input.value!=='192.168.31.176'&&input.value!=='2409:8a6a::1234'){
    input.checked=false;input.handlers.change();
  }
  await get('network-settings-form').handlers.submit({preventDefault(){}});
  const body=JSON.parse(calls.find(call=>call.options?.method==='POST').options.body);
  assert.equal(body.custom,true);
  assert.deepEqual(body.addresses.sort(),['192.168.31.176','2409:8a6a::1234']);
});

test('an intentionally empty custom selection is preserved instead of enabling all addresses',async()=>{
  const {get,calls,events}=await fixture();
  get('network-custom').checked=true;get('network-custom').handlers.change();
  for(const input of descendants(get('network-address-list')).filter(node=>node.type==='checkbox')){
    input.checked=false;input.handlers.change();
  }
  await get('network-settings-form').handlers.submit({preventDefault(){}});
  await events.focus();
  assert.ok(descendants(get('network-address-list')).filter(node=>node.type==='checkbox').every(input=>!input.checked));
  assert.deepEqual(JSON.parse(calls.find(call=>call.options?.method==='POST').options.body).addresses,[]);
});
