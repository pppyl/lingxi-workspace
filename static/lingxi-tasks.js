import {activeStates,stateLabels,filterTasks,canDeleteTask,canStopTask,beijingInput,planPayload,canBatchAdjust,batchPlanPayload} from './lingxi-task-model.js';
const $=id=>document.getElementById(id);
let tasks=[],accounts=[],filter='all',editingId='',detailId='',saving=false,loading=false,detailCache='';
const selectedTasks=new Set();let batchIds=[],deleteIds=[],deleting=false,stopping=false;
const format=(value,millis=false)=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,
  year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',...(millis?{fractionalSecondDigits:3}:{})}):'—';
function node(tag,className='',text){const el=document.createElement(tag);el.className=className;if(text!==undefined)el.textContent=text;return el;}
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const value=await response.json();if(!response.ok)throw new Error(value.error||'操作失败，请重试。');return value;}
function feedback(message,success=false){$('feedback').textContent=message;$('feedback').hidden=!message;$('feedback').classList.toggle('success',success);}
function badge(state){const color=activeStates.has(state)?'active':state==='completed'||state==='success'?'success':['failed','partial','interrupted','missed'].includes(state)?'error':state==='waiting'?'waiting':'';return node('span','badge '+color,stateLabels[state]||({planned:'待发送',success:'已收到回复'}[state])||state);}
function action(label,fn,style='quiet'){const el=node('button',style,label);el.type='button';el.addEventListener('click',()=>Promise.resolve(fn()).catch(error=>feedback(error.message)));return el;}
function visibleTasks(){return filterTasks(tasks,{group:filter,state:$('task-state-filter').value,query:$('task-search').value});}
function updateTaskSelection(){
  const visible=visibleTasks();
  const count=visible.filter(task=>selectedTasks.has(task.id)).length;
  $('select-visible-tasks').checked=visible.length>0&&count===visible.length;
  $('select-visible-tasks').indeterminate=count>0&&count<visible.length;
  $('select-visible-tasks').disabled=!visible.length;
  $('selected-task-count').textContent=`已选 ${selectedTasks.size} 项`;
  const selected=tasks.filter(task=>selectedTasks.has(task.id)),adjustable=selected.every(canBatchAdjust);
  $('batch-adjust').disabled=!selectedTasks.size||!adjustable;
  $('batch-adjust').title=adjustable?'统一修改所选任务':'仅支持批量调整待执行任务';
  $('batch-stop').disabled=stopping||!selected.some(canStopTask);
  $('batch-delete').disabled=!selectedTasks.size||deleting||!selected.every(canDeleteTask);
  $('batch-delete').title=selected.every(canDeleteTask)?'删除所选任务及执行记录':'请先停止执行中的任务，等待接收结束后删除';
  $('clear-task-selection').hidden=!selectedTasks.size;
}
function render(){
  const shown=visibleTasks(),available=new Set(shown.map(task=>task.id));
  for(const id of selectedTasks)if(!available.has(id))selectedTasks.delete(id);
  $('task-count').textContent=tasks.length;
  $('filter-result-count').textContent=`显示 ${shown.length} / ${tasks.length} 项`;
  $('reset-task-filters').hidden=filter==='all'&&!$('task-state-filter').value&&!$('task-search').value;
  const rows=shown.map(task=>{
    const row=node('tr'),selection=node('td','table-selection'),account=node('td','table-account');
    const select=node('input','task-checkbox');select.type='checkbox';select.checked=selectedTasks.has(task.id);
    select.setAttribute('aria-label',`选择 ${task.account_name} 在 ${format(task.next_run_at||task.run_at)} 的任务`);
    select.addEventListener('change',()=>{if(select.checked)selectedTasks.add(task.id);else selectedTasks.delete(task.id);updateTaskSelection();});
    selection.append(select);account.append(node('strong','',task.account_name));
    if(task.phone_masked&&task.phone_masked!==task.account_name)account.append(node('small','',task.phone_masked));
    const status=node('td');status.append(badge(task.state));
    const content=node('td'),contentText=node('p','table-content',task.content);contentText.title=task.content;content.append(contentText);
    const plan=node('td','table-plan');plan.append(node('time','',format(task.enabled?task.next_run_at:task.active_run_at||task.run_at).replace(' ','\n')),node('small','',task.weekly?'每周重复':'仅执行一次'));
    const configuration=node('td','table-settings');configuration.append(node('strong','',`${task.repeat_count} 次`),node('small','',`间隔 ${task.interval} 秒`));
    const progress=node('td','table-progress');progress.append(node('strong','',`${task.sent_count}/${task.repeat_count} 已发起`),node('small','',`完整回复 ${task.success_count} 次`));
    if(task.queued_count)progress.append(node('small','queued-count',`排队 ${task.queued_count} 次`));
    const reply=node('td','table-reply-cell'),operations=node('td'),buttons=node('div','task-actions');
    const needsLogin=['missing','login_required'].includes(task.account_status);
    const note=node('p','table-reply'+(task.last_reply?' valid-reply':needsLogin?' login-required':''),task.last_reply?task.last_reply.text:needsLogin?'账号需验证，请打开账号管理。':task.message);
    if(task.last_reply)note.title=`第 ${task.last_reply.index} 次有效回复 · ${format(task.last_reply.finished_at)}\n${task.last_reply.text}`;
    if(needsLogin)account.append(node('small','login-required','账号需验证'));reply.append(note);
    if(task.last_reply)reply.append(node('small','reply-stamp',`第 ${task.last_reply.index} 次有效回复`));
    buttons.append(action('查看回复',()=>openResults(task.id),'secondary'));
    const edit=action(task.enabled?'调整':'重新预约',()=>openEditor(task.id));edit.disabled=activeStates.has(task.state);buttons.append(edit);
    if(task.enabled||activeStates.has(task.state))buttons.append(action('停止',async()=>{await api('/api/lingxi/tasks/stop',{id:task.id});feedback('已停止后续发送，正在接收的回复会保留。',true);await refresh();},'danger'));
    else buttons.append(action('删除',()=>openDelete([task.id])));
    operations.append(buttons);row.append(selection,account,status,content,plan,configuration,progress,reply,operations);return row;
  });
  if(!rows.length){const empty=node('tr'),cell=node('td','table-empty',tasks.length?'当前筛选下没有任务。':'还没有定时任务。点击“新建任务”设置第一次预约。');cell.colSpan=9;empty.append(cell);rows.push(empty);}
  $('tasks').replaceChildren(...rows);
  updateTaskSelection();
}
async function refresh(){
  if(loading)return;loading=true;
  try{
    const data=await api('/api/lingxi/tasks');tasks=data.tasks;render();
    $('send-runtime').textContent=`并发 ${data.settings.concurrency} · 进行中 ${data.settings.running} · 排队 ${data.settings.queued}`;
    $('sync-state').textContent='北京时间 · 最近同步 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
    if(data.warning)feedback(data.warning);
    if($('results').open&&detailId)await refreshResults();
  }catch(error){feedback(error.message);$('sync-state').textContent='本地服务未连接';}
  finally{loading=false;}
}
function preview(){
  const count=Number($('repeat-count').value),interval=Number($('send-interval').value);
  $('plan-preview').textContent=`从预约时刻开始发 ${count||'—'} 次，每次间隔 ${interval||'—'} 秒。${count>1&&Number.isFinite(interval)?`最后一次在开始后 ${(count-1)*interval} 秒发起。`:''}`;
}
function updateAccountSelection(){
  const options=[...$('account-options').querySelectorAll('input:not(:disabled)')];
  const selected=options.filter(input=>input.checked).length;
  $('selected-account-count').textContent=`已选 ${selected}/${options.length}`;
  $('select-all-accounts').textContent=options.length>0&&selected===options.length?'取消全选':'全选';
  $('select-all-accounts').disabled=!options.length;
}
async function openEditor(id=''){
  const data=await api('/api/logins');accounts=data.accounts;editingId=id;
  let task=id?(await api('/api/lingxi/tasks/'+id)).task:null;
  $('task-form').reset();$('form-error').hidden=true;$('editor-title').textContent=id?'调整定时发送':'新建定时发送';
  $('account-selection-hint').textContent=id?'当前任务使用一个账号':'可多选';
  $('account-selection-tools').hidden=!!id;
  $('task-content').value=task?.content||'';
  $('repeat-count').value=task?.repeat_count??3;$('send-interval').value=task?.interval??1;$('weekly').checked=!!task?.weekly;
  const upcoming=task?.next_run_at&&new Date(task.next_run_at).getTime()>Date.now()?task.next_run_at:new Date(Date.now()+120000);
  $('run-at').value=beijingInput(upcoming);
  const options=accounts.map(account=>{
    const label=node('label','account-option'),check=node('input');check.type=id?'radio':'checkbox';check.name='task-account';check.value=account.id;
    check.checked=task?task.login_id===account.id:data.active_id===account.id;
    check.disabled=account.status==='login_required';if(check.disabled)check.checked=false;
    const name=node('span','',account.name||account.phone_masked);
    if(account.name)name.append(node('small','',account.phone_masked));
    if(check.disabled)name.append(node('small','','需重新验证'));
    label.append(check,name);return label;
  });
  if(!options.length)options.push(node('p','footnote','账号池为空，请先在账号管理中添加账号。'));
  $('account-options').replaceChildren(...options);updateAccountSelection();preview();$('editor').showModal();
}
$('account-options').addEventListener('change',updateAccountSelection);
$('select-all-accounts').addEventListener('click',()=>{
  const options=[...$('account-options').querySelectorAll('input:not(:disabled)')];
  const checked=!options.every(input=>input.checked);options.forEach(input=>input.checked=checked);updateAccountSelection();
});
$('select-visible-tasks').addEventListener('change',event=>{
  for(const task of visibleTasks()){
    if(event.target.checked)selectedTasks.add(task.id);else selectedTasks.delete(task.id);
  }
  render();
});
$('clear-task-selection').addEventListener('click',()=>{selectedTasks.clear();render();});
$('batch-stop').addEventListener('click',async()=>{
  if(stopping)return;
  stopping=true;$('batch-stop').textContent='正在停止…';updateTaskSelection();
  try{
    const ids=tasks.filter(task=>selectedTasks.has(task.id)&&canStopTask(task)).map(task=>task.id);
    const result=await api('/api/lingxi/tasks/stop-batch',{ids});feedback(result.message,true);await refresh();
  }catch(error){feedback(error.message);}
  finally{stopping=false;$('batch-stop').textContent='停止所选';updateTaskSelection();}
});
function openDelete(ids){
  deleteIds=ids;const selected=tasks.filter(task=>ids.includes(task.id));
  $('delete-title').textContent=`删除 ${selected.length} 项任务？`;
  $('confirm-delete').textContent=`确认删除 ${selected.length} 项`;
  $('delete-task-list').replaceChildren(...selected.map(task=>{
    const row=node('div','delete-task-row'),summary=node('div');
    summary.append(node('strong','',task.account_name),node('p','',task.content));
    row.append(summary,badge(task.state));return row;
  }));
  const scheduled=selected.filter(task=>task.enabled).length;
  $('delete-scheduled-note').hidden=!scheduled;
  $('delete-scheduled-note').textContent=`其中 ${scheduled} 项仍有预约，删除后将不再发送。`;
  $('delete-error').hidden=true;$('delete-confirm').showModal();
}
$('batch-delete').addEventListener('click',()=>openDelete([...selectedTasks]));
$('delete-confirm').addEventListener('cancel',event=>{if(deleting)event.preventDefault();});
$('delete-form').addEventListener('submit',async event=>{
  event.preventDefault();if(deleting)return;
  deleting=true;const controls=[...$('delete-form').querySelectorAll('button')];
  controls.forEach(button=>button.disabled=true);$('delete-error').hidden=true;$('confirm-delete').textContent='正在删除…';
  try{
    const result=await api('/api/lingxi/tasks/delete-batch',{ids:deleteIds});
    result.ids.forEach(id=>selectedTasks.delete(id));tasks=tasks.filter(task=>!result.ids.includes(task.id));
    $('delete-confirm').close();render();feedback(result.message,true);await refresh();
  }catch(error){$('delete-error').textContent=error.message;$('delete-error').hidden=false;}
  finally{deleting=false;controls.forEach(button=>button.disabled=false);$('confirm-delete').textContent=`确认删除 ${deleteIds.length} 项`;updateTaskSelection();}
});
const batchFields={content:'batch-content',run_at:'batch-run-at',repeat_count:'batch-repeat-count',interval:'batch-interval',weekly:'batch-weekly'};
const batchChecks=[...document.querySelectorAll('[data-batch-field]')];
batchChecks.forEach(check=>check.addEventListener('change',()=>{
  $(batchFields[check.dataset.batchField]).disabled=!check.checked;
  $('save-batch').disabled=!batchChecks.some(item=>item.checked);
}));
$('batch-adjust').addEventListener('click',()=>{
  batchIds=[...selectedTasks];const first=tasks.find(task=>task.id===batchIds[0]);
  $('batch-form').reset();batchChecks.forEach(check=>$(batchFields[check.dataset.batchField]).disabled=true);
  $('batch-editor-title').textContent=`批量调整 ${batchIds.length} 项任务`;
  $('batch-content').value='';$('batch-repeat-count').value=first.repeat_count;$('batch-interval').value=first.interval;
  $('batch-run-at').value=beijingInput(first.next_run_at);$('batch-weekly').value=String(first.weekly);
  $('batch-error').hidden=true;$('save-batch').disabled=true;$('batch-editor').showModal();
});
$('batch-form').addEventListener('submit',async event=>{
  event.preventDefault();if(saving)return;
  try{
    const changes={};
    for(const check of batchChecks.filter(item=>item.checked)){
      const field=check.dataset.batchField;
      changes[field]=field==='weekly'?$(batchFields[field]).value==='true':$(batchFields[field]).value;
    }
    const payload=batchPlanPayload(batchIds,changes);
    saving=true;$('save-batch').disabled=true;$('batch-error').hidden=true;
    const result=await api('/api/lingxi/tasks/adjust',payload);
    batchIds.forEach(id=>selectedTasks.delete(id));$('batch-editor').close();feedback(result.message,true);await refresh();
  }catch(error){$('batch-error').textContent=error.message;$('batch-error').hidden=false;}
  finally{saving=false;$('save-batch').disabled=!batchChecks.some(item=>item.checked);}
});
$('task-form').addEventListener('submit',async event=>{
  event.preventDefault();if(saving)return;
  try{
    const loginIds=[...$('account-options').querySelectorAll('input:checked')].map(input=>input.value);
    const payload=planPayload({id:editingId,loginIds,content:$('task-content').value,runAt:$('run-at').value,
      count:$('repeat-count').value,interval:$('send-interval').value,weekly:$('weekly').checked});
    saving=true;$('save-task').disabled=true;$('form-error').hidden=true;
    await api('/api/lingxi/tasks/save',payload);$('editor').close();feedback(`已保存 ${loginIds.length} 项预约。`,true);await refresh();
  }catch(error){$('form-error').textContent=error.message;$('form-error').hidden=false;}
  finally{saving=false;$('save-task').disabled=false;}
});
function renderAttempts(attempts){
  return attempts.map(attempt=>{
    const row=node('section','attempt'),head=node('div','attempt-header');head.append(node('strong','',`第 ${attempt.index} 次`),badge(attempt.status));
    let timing=`计划 ${format(attempt.planned_at,true)}\n发起 ${format(attempt.sent_at,true)}`;
    if(attempt.queued_at)timing+=`\n入队 ${format(attempt.queued_at,true)} · 队列等待 ${attempt.queue_wait_seconds===undefined?'中':attempt.queue_wait_seconds+' 秒'}`;
    row.append(head,node('p','attempt-time',timing));
    if(attempt.text)row.append(node('div','reply',attempt.text));
    else if(!attempt.cards?.length)row.append(node('p','reply-empty',attempt.status==='planned'?'等待发送。':attempt.status==='queued'?'已入队，等待空闲名额…':attempt.status==='receiving'?'正在接收回复…':'暂无回复内容。'));
    for(const content of attempt.cards||[]){const detail=node('details'),summary=node('summary','','查看卡片内容');detail.append(summary,node('pre','',content));row.append(detail);}
    if(attempt.error)row.append(node('p','attempt-error',attempt.error));
    if(attempt.truncated)row.append(node('p','reply-empty','回复较长，保存内容已截断。'));
    return row;
  });
}
async function openResults(id){detailId=id;detailCache='';$('results-body').replaceChildren(node('p','empty','正在读取执行记录…'));$('results').showModal();await refreshResults();}
async function refreshResults(){
  const id=detailId;const {task}=await api('/api/lingxi/tasks/'+id);if(id!==detailId||!$('results').open)return;
  const signature=JSON.stringify(task);if(signature===detailCache)return;detailCache=signature;
  const scroll=$('results').scrollTop;
  $('results-subtitle').textContent=`${task.account_name} · ${task.repeat_count} 次 · 间隔 ${task.interval} 秒 · ${task.message}`;
  const content=node('div','detail-content',task.content),rows=renderAttempts(task.attempts);
  if(!rows.length)rows.push(node('p','empty','任务尚未执行，回复会分别保存在这里。'));
  const history=task.history?.length?node('details'):null;
  if(history){history.append(node('summary','','往期记录'));for(const run of [...task.history].reverse()){history.append(node('p','attempt-time',format(run.run_at)),...renderAttempts(run.attempts));}}
  $('results-body').replaceChildren(content,...rows,...(history?[history]:[]));$('results').scrollTop=scroll;
}
$('repeat-count').addEventListener('input',preview);$('send-interval').addEventListener('input',preview);
$('open-send-settings').addEventListener('click',async()=>{
  try{
    const data=await api('/api/lingxi/settings');$('concurrency-limit').value=data.settings.concurrency;
    $('send-settings-error').hidden=true;$('send-settings').showModal();
  }catch(error){feedback(error.message);}
});
$('send-settings-form').addEventListener('submit',async event=>{
  event.preventDefault();const concurrency=Number($('concurrency-limit').value);
  $('save-send-settings').disabled=true;
  try{
    if(!Number.isInteger(concurrency)||concurrency<1||concurrency>1024)throw new Error('并发上限须为 1～1024 的整数。');
    const result=await api('/api/lingxi/settings',{concurrency});$('send-settings').close();feedback(result.message,true);await refresh();
  }catch(error){$('send-settings-error').textContent=error.message;$('send-settings-error').hidden=false;}
  finally{$('save-send-settings').disabled=false;}
});
$('create-task').addEventListener('click',()=>openEditor().catch(error=>feedback(error.message)));
$('refresh').addEventListener('click',refresh);
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.close).close()));
document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{filter=button.dataset.filter;$('task-state-filter').value='';selectedTasks.clear();document.querySelectorAll('[data-filter]').forEach(other=>{other.classList.toggle('selected',other===button);other.setAttribute('aria-pressed',String(other===button));});render();}));
for(const [state,label] of Object.entries(stateLabels)){const option=node('option','',label);option.value=state;$('task-state-filter').append(option);}
$('task-search').addEventListener('input',()=>{selectedTasks.clear();render();});
$('task-state-filter').addEventListener('change',()=>{selectedTasks.clear();render();});
$('reset-task-filters').addEventListener('click',()=>{$('task-search').value='';document.querySelector('[data-filter="all"]').click();});
$('results').addEventListener('close',()=>{detailId='';detailCache='';});
window.addEventListener('accounts-changed',refresh);window.addEventListener('focus',refresh);
refresh();
if(new URLSearchParams(location.search).get('new')==='1')openEditor().catch(error=>feedback(error.message));
setInterval(()=>{if(!document.hidden)refresh();},2000);
