(() => {
  'use strict';
  const root=location.origin+'/coupon-verification/mgt/';
  const query=new URLSearchParams(location.search);
  const appId=query.get('app_id')||'e1f92650d20263d38dbe327e7237f516';
  const riskId=query.get('risk_id');
  const status=document.getElementById('status'),verify=document.getElementById('verify');
  let reloadTimer=null,finished=false;
  function renew(message,delay){
    if(finished||reloadTimer!==null)return;
    verify.replaceChildren();status.textContent=message;
    reloadTimer=setTimeout(()=>location.reload(),delay);
  }
  function failed(){renew('官方验证加载失败，正在自动重新获取…',3000);}
  const script=document.createElement('script');
  script.src=root+'decision-captcha/zw-captcha/index.js';script.onerror=failed;
  script.onload=()=>{
    status.textContent='请拖动官方组件中的滑块。';
    window._zw.Captcha(verify,{
      appId,customLanguage:{init_inform:'拖动一下'},
      apiServer:root+'decision-captcha',constIDServer:root+'decision-engine/udid/c1',
      constID_js:root+'decision-captcha/zw-captcha/libs/const-id.js',
      ua_js:root+'decision-captcha/zw-captcha/libs/greenseer.js?t='+Date.now(),
      path:root+'decision-captcha/zw-captcha/',style:'embed',isSaaS:false,showVoice:false,m:riskId?{riskId}:'',width:300,
      success(token){
        if(finished||reloadTimer!==null)return;
        finished=true;status.textContent=riskId?'验证成功，正在继续获取付款链接…':'验证成功，正在自动继续…';
        parent.postMessage({type:'coupon-captcha-success',token},location.origin);
      },
      fail(){
        renew('验证未通过，正在自动获取新的滑块…',500);
      },failSafe:failed,
    }).on('loadFail',failed);
  };
  document.head.append(script);
})();
