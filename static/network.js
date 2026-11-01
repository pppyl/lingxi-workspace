(() => {
  'use strict';
  const $=id=>document.getElementById(id),form=$('network-settings-form');
  let state=null,loading=false,saving=false,dirty=false;
  const element=(tag,className,text)=>{const node=document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;};
  function link(url){const node=element('a','network-address',url);node.href=url;node.target='_blank';node.rel='noopener noreferrer';node.title=url;return node;}
  function family(label,urls,enabled){
    const block=element('div','network-family');block.append(element('span','network-family-label',label));
    if(!enabled)block.append(element('small','','未开启对外监听'));
    else if(!urls.length)block.append(element('small','','未检测到可用地址'));
    else for(const url of urls)block.append(link(url));
    return block;
  }
  function render(value){
    state=value;const active=value.active||value.settings,footer=$('workspace-addresses');
    if(footer){
      const ipv4=family('内网 IPv4',value.ipv4,active.ipv4),ipv6=family('IPv6',value.ipv6,active.ipv6);
      if(value.ipv4.length>2&&active.ipv4){
        const extra=element('details','network-more'),summary=element('summary','',`其他 IPv4 地址（${value.ipv4.length-1}）`);
        extra.append(summary,...value.ipv4.slice(1).map(link));
        ipv4.replaceChildren(element('span','network-family-label','内网 IPv4'),link(value.ipv4[0]),extra);
      }
      footer.replaceChildren(ipv4,ipv6);
    }
    if(!form)return;
    if(!dirty){$('network-ipv4').checked=value.settings.ipv4;$('network-ipv6').checked=value.settings.ipv6;}
    for(const id of ['network-ipv4','network-ipv6','network-save'])$(id).disabled=saving||!value.managed;
    $('network-description').textContent=!value.managed?'当前监听由运行环境控制':value.restart_required?'设置已保存 · 重启服务后生效':`当前端口 ${value.port} · IPv4 ${active.ipv4?'对外开启':'仅本机'} · IPv6 ${active.ipv6?'对外开启':'仅本机'}`;
    $('network-address-list').replaceChildren(family('本机',value.local,true),family('IPv4',value.ipv4,active.ipv4),family('IPv6',value.ipv6,active.ipv6));
  }
  async function responseData(response){const value=await response.json();if(!response.ok)throw new Error(value.error||'网络设置读取失败');return value;}
  async function load(){
    if(loading||saving)return;loading=true;
    try{render(await responseData(await fetch('/api/network/status')));}
    catch(error){if(form){$('network-description').textContent=error.message;}}
    finally{loading=false;}
  }
  if(form){
    for(const id of ['network-ipv4','network-ipv6'])$(id).addEventListener('change',()=>{dirty=true;});
    $('network-refresh').addEventListener('click',load);
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(saving||!state?.managed)return;saving=true;$('network-save').disabled=true;
      try{
        const value=await responseData(await fetch('/api/network/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ipv4:$('network-ipv4').checked,ipv6:$('network-ipv6').checked})}));
        dirty=false;render(value);$('network-feedback').textContent=value.message;$('network-feedback').classList.toggle('success',true);$('network-feedback').hidden=false;
      }catch(error){$('network-feedback').textContent=error.message;$('network-feedback').classList.toggle('success',false);$('network-feedback').hidden=false;}
      finally{saving=false;$('network-save').disabled=!state?.managed;for(const id of ['network-ipv4','network-ipv6'])$(id).disabled=!state?.managed;}
    });
  }
  window.addEventListener('focus',load);
  load();
})();
