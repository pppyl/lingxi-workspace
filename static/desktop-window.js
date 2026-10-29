// A live connection identifies this client tab so a second EXE launch reuses it.
document.title='本地工具台 · 客户端 — '+document.title;
const id=crypto.randomUUID().replaceAll('-','');
let stream=null,revision=null;
function connect(){
  stream=new EventSource('/_desktop/window-events?id='+id);
  stream.onmessage=event=>{
    const next=JSON.parse(event.data).focus;
    if(revision!==null&&next!==revision)window.focus();
    revision=next;
  };
}
addEventListener('pageshow',()=>{if(!stream)connect();});
addEventListener('pagehide',()=>{
  stream.close();stream=null;
  navigator.sendBeacon('/_desktop/window-closed',new Blob([JSON.stringify({id})],{type:'application/json'}));
});
connect();
