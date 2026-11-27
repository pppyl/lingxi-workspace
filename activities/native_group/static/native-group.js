(() => {
  'use strict';
  const el=id=>document.getElementById(id), root='/api/native-group';
  let model=null, selected=null, size=10, busy=false, again=false, accountsChanged=false, operation=null, plan=null, phoneAccountId=null;
  const text=(id,value)=>{el(id).textContent=value;};
  const node=(tag,value)=>{const n=document.createElement(tag);n.textContent=value;return n;};
  function feedback(message,error=false){text('ng-feedback',message);el('ng-feedback').hidden=!message;el('ng-feedback').classList.toggle('error',error);}
  async function call(path,payload){
    const response=await fetch(root+path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败');return data;
  }
  const current=()=>model?.accounts.find(a=>a.id===selected);
  const label=id=>{const a=model.accounts.find(a=>a.id===id);return a?(a.name||a.phone_masked):id;};
  function render(){
    if(!model)return;
    if(!model.accounts.some(a=>a.id===selected))selected=model.accounts.find(a=>a.status==='active')?.id||null;
    if(phoneAccountId&&!model.accounts.some(a=>a.id===phoneAccountId))el('ng-call-dialog').close();
    text('ng-count',model.accounts.length);text('ng-period',`${model.campaign.start_at} 至 ${model.campaign.end_at}`);
    el('ng-accounts').replaceChildren(...model.accounts.map(account=>{
      const b=node('button','');b.type='button';b.className='ng-account';b.setAttribute('aria-pressed',String(account.id===selected));
      b.append(node('strong',account.name||account.phone_masked),node('small',account.phone_masked));
      b.addEventListener('click',()=>{selected=account.id;render();refresh();});return b;
    }));
    for(const button of document.querySelectorAll('[data-size]'))button.setAttribute('aria-pressed',String(Number(button.dataset.size)===size));
    const account=current(), state=account?.observation, campaign=state?.campaign, group=campaign?.userGroupInfo;
    const members=group?.groupMembers||[], available=account?.status==='active'&&campaign?.showGroup&&!account.pending&&!busy&&!model.campaign.ended;
    el('ng-content').setAttribute('aria-busy',String(busy));
    el('ng-empty').hidden=!!group;el('ng-team').hidden=!group;
    text('ng-team-state',group?`${members.length} / ${group.groupSize} 人`:state?'尚未组队':'请选择账号查询');
    text('ng-qualification',state?(campaign.showGroup?'官方已开放该账号组队，选择人数后创建或加入邀请队伍。':'官方当前未开放该账号的组队资格。'):'查询后显示官方资格及当前队伍。');
    text('ng-create',`创建 ${size} 人队`);el('ng-create').disabled=!available||!!group;el('ng-join').disabled=!available||!!group;
    el('ng-invite').disabled=!available||!group;
    el('ng-quit').disabled=!available||members.length!==1||members[0]?.phone!==state?.identity.phone;
    el('ng-claim').disabled=!available||campaign?.groupPrizeBtnStatus!=='1'||!group?.groupTaskStatus;
    text('ng-member-count',members.length);text('ng-team-size',`队伍目标 ${group?.groupSize||'—'} 人`);
    text('ng-done-count',members.filter(m=>String(m.authTaskStatus)==='1'&&String(m.callTaskStatus)==='1').length);
    el('ng-members').replaceChildren(...members.map(member=>{const row=document.createElement('tr');row.append(node('td',member.phone),node('td',String(member.authTaskStatus)==='1'?'已完成':'待官方确认'),node('td',String(member.callTaskStatus)==='1'?'已完成':'尚未完成'));return row;}));
    const own=members.find(member=>member.phone===state?.identity.phone), callDone=String(own?.callTaskStatus)==='1';
    el('ng-call-panel').hidden=!group;
    text('ng-call-summary',callDone?'官方已确认当前账号的通话任务，无需重复拨打。':account?.pending?'上次提交等待官方确认，请刷新核对。':account?.app_login_required?'请先在账号管理恢复 APP 登录，再准备拨号。':String(own?.authTaskStatus)==='1'?'在手机浏览器中拨打电话，完成真实通话后同步并核对。':model.call_authorization_available?'准备拨号时将自动同步本账号的既有通话授权。':'尚未接通本账号的通话授权，请先配置已验证的默认电话设备。');
    text('ng-call-open',callDone?'通话已完成':account?.phone_call?'继续通话任务':'去拨打电话');
    el('ng-call-open').disabled=busy||account?.status!=='active'||!group||callDone||model.campaign.ended;
    text('ng-draw-count',`${campaign?.drawTimes??'—'} 次机会 · 今日已抽 ${campaign?.todayAlreadyDrawTimes??0} / 2`);
    text('ng-prepare',account?.risk_ready?'抽奖认证已准备':'准备抽奖认证');
    el('ng-prepare').disabled=busy||account?.status!=='active'||!!account?.pending||!!account?.risk_ready;
    el('ng-draw').disabled=busy||account?.status!=='active'||!!account?.pending||!(campaign?.drawTimes>0)||Number(campaign?.todayAlreadyDrawTimes||0)>=2||model.campaign.ended;
    el('ng-tasks').replaceChildren(...(state?.tasks||[]).map(task=>{const item=node('li','');item.append(node('strong',task.title),node('small',`${task.subtitle} · ${task.status===1?'官方已完成':'尚未完成'}`));return item;}));
    el('ng-prizes').replaceChildren(...(state?.prizes?.length?state.prizes.map(prize=>{const item=node('li','');item.append(node('strong',prize.prizeName||'官方奖品'),node('small',`${prize.time||''} · ${{0:'已使用',1:'可使用',2:'已过期'}[prize.status]||'以官方状态为准'}`));return item;}):[node('li',state?'官方暂无奖品记录。':'查询后显示官方奖品记录。')]));
    text('ng-rules',model.rules||'查询后读取官方规则。');text('ng-checked',state?`最近核对 ${state.checked_at.replace('T',' ').slice(0,19)}`:'');
    el('ng-records').replaceChildren(...(account?.records||[]).map(record=>node('li',`${record.at.replace('T',' ').slice(0,19)} · ${record.message}`)));
    el('ng-refresh').disabled=!selected;el('ng-plan').disabled=busy;
    renderPhoneCall();
  }
  function renderPhoneCall(){
    if(!phoneAccountId)return;
    const account=model.accounts.find(a=>a.id===phoneAccountId), state=account?.observation;
    const own=state?.campaign.userGroupInfo?.groupMembers?.find(m=>m.phone===state.identity.phone);
    const done=String(own?.callTaskStatus)==='1', authorized=String(own?.authTaskStatus)==='1';
    const draft=account?.phone_call, matching=draft?.destination===el('ng-call-number').value;
    const ready=matching&&draft?.stage==='prepared'&&!account?.pending&&!done;
    const available=account?.status==='active'&&!model.campaign.ended;
    const appReady=available&&!account?.app_login_required, canAuthorize=!!model.call_authorization_available;
    text('ng-call-account',`任务账号 ${account?.name||''} · ${account?.phone_masked||''}`);
    el('ng-call-return').hidden=!ready;
    if(ready)el('ng-call-again').href='tel:'+draft.destination;else el('ng-call-again').removeAttribute('href');
    el('ng-call-authorization').hidden=done||authorized||!canAuthorize;
    el('ng-call-authorize').disabled=busy||!appReady||!!account?.pending;
    el('ng-call-number').disabled=busy||!appReady||done||!!account?.pending||(!authorized&&!canAuthorize);
    el('ng-call-dial').disabled=busy||!appReady||done||!!account?.pending||(!authorized&&!canAuthorize)||!/^1[3-9][0-9]{9}$/.test(el('ng-call-number').value);
    el('ng-call-connected').disabled=busy||!ready;
    el('ng-call-sync').disabled=busy||!appReady||!authorized||!ready||!el('ng-call-connected').checked;
    el('ng-call-refresh').disabled=busy||!available;
    text('ng-call-message',done?'官方已确认通话任务完成。':account?.pending?'上次提交结果待官方确认，仅核对进度，不会重复同步。':!appReady?'请先在账号管理恢复 APP 登录，再准备拨号。':!authorized?(canAuthorize?'点击拨号后自动同步本账号的既有授权，官方确认后再打开拨号。':'本账号默认通话授权尚未接通，缺少已验证的授权设备资料。'):ready?'结束通话后再确认同步。仅打开拨号页或返回浏览器不代表通话完成。':'拨打熟悉且同意接听的号码，通话结束后回到此处。');
    el('ng-call-message').classList.remove('error');
  }
  el('ng-call-open').addEventListener('click',()=>{
    phoneAccountId=selected;
    el('ng-call-number').value=current()?.phone_call?.destination||'';
    el('ng-call-connected').checked=false;
    renderPhoneCall();el('ng-call-dialog').showModal();
  });
  el('ng-call-number').addEventListener('input',()=>{
    el('ng-call-number').value=el('ng-call-number').value.replace(/[^0-9]/g,'').slice(0,11);
    el('ng-call-connected').checked=false;renderPhoneCall();
  });
  el('ng-call-connected').addEventListener('change',renderPhoneCall);
  el('ng-call-dialog').addEventListener('close',()=>{phoneAccountId=null;el('ng-call-connected').checked=false;});
  el('ng-call-authorize').addEventListener('click',async()=>{
    if(busy||el('ng-call-authorize').disabled||!phoneAccountId)return;
    const id=phoneAccountId;busy=true;render();let failure='';
    try{model=await call('/run',{login_id:id,kind:'call_authorize',confirmed:true});feedback('本账号通话授权已获官方确认，可以继续拨号。');}
    catch(error){failure=error.message;feedback(failure,true);model=await call('/status');}
    finally{busy=false;render();if(failure){text('ng-call-message',failure);el('ng-call-message').classList.add('error');}if(again){again=false;refresh();}}
  });
  el('ng-call-dial-form').addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!phoneAccountId||!el('ng-call-dial-form').reportValidity())return;
    const id=phoneAccountId;busy=true;render();let failure='';
    try{
      model=await call('/run',{login_id:id,kind:'call_prepare',destination:el('ng-call-number').value,confirmed:true});
      const account=model.accounts.find(a=>a.id===id), draft=account?.phone_call;
      const done=account?.observation?.campaign.userGroupInfo?.groupMembers?.some(m=>m.phone===account.observation.identity.phone&&String(m.callTaskStatus)==='1');
      if(!done&&draft?.stage==='prepared')window.location.href='tel:'+draft.destination;
    }catch(error){failure=error.message;feedback(failure,true);}
    finally{busy=false;render();if(failure){text('ng-call-message',failure);el('ng-call-message').classList.add('error');}if(again){again=false;refresh();}}
  });
  el('ng-call-sync').addEventListener('click',async()=>{
    if(busy||!phoneAccountId||!el('ng-call-connected').checked)return;
    const id=phoneAccountId, draft=model.accounts.find(a=>a.id===id)?.phone_call;
    busy=true;render();let failure='';
    try{
      model=await call('/run',{login_id:id,kind:'call_confirm',call_id:draft.id,connected:true,confirmed:true});
      el('ng-call-connected').checked=false;feedback('通话任务已获官方确认。');
    }catch(error){failure=error.message;feedback(failure,true);model=await call('/status');}
    finally{
      busy=false;render();
      if(failure){text('ng-call-message',failure);el('ng-call-message').classList.add('error');}
      if(again){again=false;refresh();}
    }
  });
  el('ng-call-refresh').addEventListener('click',()=>refresh(phoneAccountId));
  async function refresh(target=selected){
    if(busy){again=true;return;}
    let id=target,failure='';busy=true;render();
    try{
      if(accountsChanged||!model||!id){
        const firstLoad=!model;accountsChanged=false;
        model=await call('/status');
        if(firstLoad){const count=model.accounts.filter(a=>a.status==='active').length;size=count>=10?10:count>=5?5:3;}
        render();
        if(!model.accounts.some(a=>a.id===id))id=selected;
      }
      if(id){model=await call('/status?login_id='+encodeURIComponent(id));if(selected===id)feedback(current()?.pending?'上次提交待官方确认，已暂停重复提交。':'');}
    }catch(error){failure=error.message;feedback(failure,true);}
    finally{busy=false;render();if(failure&&phoneAccountId){text('ng-call-message',failure);el('ng-call-message').classList.add('error');}if(again||accountsChanged||selected!==id){again=false;refresh();}}
  }
  function confirm(kind){
    operation={kind,loginId:selected,size};plan=null;
    text('ng-confirm-title',({create:'确认创建队伍',join:'确认加入队伍',quit:'确认退出队伍',claim:'确认领取组队奖励',invite:'生成官方邀请信息',draw:'确认抽奖',prepare:'准备官方抽奖认证'})[kind]);
    const description=kind==='draw'?`本次将消耗 1 次机会，当前剩余 ${current()?.observation?.campaign.drawTimes??0} 次。`:
      kind==='prepare'?'获取官方设备认证，执行抽奖时将复用有效凭据。':
      kind==='invite'?'获取当前队伍的官方邀请链接。':
      kind==='quit'?'退出后需重新选择队伍。':`${kind==='create'?`${size} 人队。`:''}每个号码活动期间仅能加入一个队伍，满员且全员完成任务后方可领奖。`;
    text('ng-confirm-text',`${label(selected)} · ${description}`);
    text('ng-confirm-error','');el('ng-plan-preview').replaceChildren();el('ng-invitation').value='';el('ng-invitation-label').hidden=kind!=='join';el('ng-confirm').showModal();
  }
  for(const kind of ['create','join','quit','claim','invite','draw','prepare'])el('ng-'+kind).addEventListener('click',()=>confirm(kind));
  for(const b of document.querySelectorAll('[data-size]'))b.addEventListener('click',()=>{size=Number(b.dataset.size);render();});
  for(const b of document.querySelectorAll('[data-close]'))b.addEventListener('click',()=>el(b.dataset.close).close());
  el('ng-refresh').addEventListener('click',()=>refresh());
  el('ng-plan').addEventListener('click',async()=>{
    if(busy)return;busy=true;render();feedback('正在逐号核对资格与已有队伍…');
    try{
      plan=await call('/plan',{});model=await call('/status');operation={kind:'assemble',teams:plan.teams};
      text('ng-confirm-title',plan.resume?'继续上次组队方案':'确认按号池组队');text('ng-confirm-text',plan.resume?'将先核对已有队伍，继续处理原方案中尚未入队的号码。':'优先安排 10 人队，余号再组成 5 人或 3 人队。每号仅能加入一个队伍，请核对名单。');
      el('ng-invitation-label').hidden=true;text('ng-confirm-error','');
      const lines=plan.teams.map(team=>node('p',`${team.size} 人队 · ${team.members.map(label).join('、')}`));
      if(plan.waiting.length)lines.push(node('p','人数不足，待组队 · '+plan.waiting.map(label).join('、')));
      for(const item of plan.excluded)lines.push(node('p',`${label(item.id)} · ${item.reason}`));
      el('ng-plan-preview').replaceChildren(...lines);el('ng-confirm-submit').disabled=!plan.teams.length;el('ng-confirm').showModal();feedback('');
    }catch(error){feedback(error.message,true);}finally{busy=false;render();if(again){again=false;refresh();}}
  });
  el('ng-confirm-form').addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!operation)return;busy=true;el('ng-confirm-submit').disabled=true;render();
    try{
      const result=operation.kind==='assemble'?await call('/assemble',{confirmed:true,teams:operation.teams}):
        await call('/run',{login_id:operation.loginId,kind:operation.kind,size:operation.size,invitation:el('ng-invitation').value,confirmed:true});
      el('ng-confirm').close();
      if(result.invitation){if(result.status)model=result.status;text('ng-share-title',result.invitation.title||'将链接发给符合条件的好友');el('ng-share-url').value=result.invitation.url;text('ng-copy-status','');el('ng-share').showModal();}
      else{model=result;feedback('官方操作结果已核对。');}
    }catch(error){text('ng-confirm-error',error.message);model=await call('/status');}
    finally{busy=false;el('ng-confirm-submit').disabled=false;render();if(again){again=false;refresh();}}
  });
  el('ng-confirm').addEventListener('close',()=>{el('ng-confirm-submit').disabled=false;});
  el('ng-copy').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(el('ng-share-url').value);text('ng-copy-status','已复制邀请链接');}catch(error){text('ng-copy-status','请选择链接后复制');}});
  window.addEventListener('accounts-changed',()=>{accountsChanged=true;refresh();});
  refresh();
})();
