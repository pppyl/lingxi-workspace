export const activeStates=new Set(['preparing','running','queued','receiving','stopping']);
export const stateLabels={waiting:'待执行',preparing:'校验账号',running:'执行中',queued:'排队中',receiving:'接收回复',stopping:'正在停止',completed:'已完成',partial:'部分完成',failed:'执行失败',cancelled:'已停止',missed:'已错过',interrupted:'已中断'};
export function taskGroup(task){return activeStates.has(task.state)?'active':task.enabled?'waiting':'ended';}
export function canBatchAdjust(task){return task.enabled&&task.state==='waiting';}
export function canDeleteTask(task){return !activeStates.has(task.state);}
export function canStopTask(task){return task.enabled||activeStates.has(task.state);}
export function filterTasks(tasks,{group='all',state='',query=''}={}){
  const words=query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return tasks.filter(task=>(group==='all'||taskGroup(task)===group)&&(!state||task.state===state)
    &&words.every(word=>`${task.account_name} ${task.phone_masked} ${task.content}`.toLocaleLowerCase().includes(word)));
}
export function beijingInput(value=new Date()){
  const date=new Date(value);if(!Number.isFinite(date.getTime()))return '';
  return new Date(date.getTime()+8*3600000).toISOString().slice(0,19);
}
export function batchPlanPayload(ids,changes,now=Date.now()){
  if(!ids.length)throw new Error('请勾选要调整的任务。');
  if(!Object.keys(changes).length)throw new Error('请勾选要修改的字段。');
  const values={...changes};
  if('content' in values){
    values.content=values.content.trim();
    if(!values.content||values.content.length>8000)throw new Error('内容须为 1～8000 个字符。');
  }
  if('run_at' in values){
    values.run_at+='+08:00';
    if(!Number.isFinite(Date.parse(values.run_at))||Date.parse(values.run_at)<=now)throw new Error('请选择晚于当前时间的发送时间。');
  }
  if('repeat_count' in values){
    values.repeat_count=Number(values.repeat_count);
    if(!Number.isInteger(values.repeat_count)||values.repeat_count<1||values.repeat_count>20)throw new Error('发送次数须为 1～20 的整数。');
  }
  if('interval' in values){
    values.interval=Number(values.interval);
    if(!Number.isFinite(values.interval)||values.interval<.2||values.interval>3600)throw new Error('发送间隔须为 0.2～3600 秒。');
  }
  return {ids,changes:values};
}
export function planPayload({id,loginIds,content,runAt,count=3,interval=1,weekly=false},now=Date.now()){
  const date=new Date(runAt+'+08:00');count=Number(count);interval=Number(interval);
  if(!Array.isArray(loginIds)||!loginIds.length||loginIds.some(id=>typeof id!=='string'||!id))throw new Error('请至少选择一个账号。');
  if(!content.trim()||content.trim().length>8000)throw new Error('请填写 1～8000 个字符的内容。');
  if(!Number.isInteger(count)||count<1||count>20)throw new Error('发送次数须为 1～20 的整数。');
  if(!Number.isFinite(interval)||interval<.2||interval>3600||(count-1)*interval>3600)throw new Error('间隔须为 0.2～3600 秒，一轮总时长不超过 1 小时。');
  if(!Number.isFinite(date.getTime())||date.getTime()<=now)throw new Error('请选择晚于当前时间的发送时间。');
  return {id:id||undefined,login_ids:loginIds,content:content.trim(),run_at:runAt+'+08:00',repeat_count:count,interval,weekly:!!weekly};
}
