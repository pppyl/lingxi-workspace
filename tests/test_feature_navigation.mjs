import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../static/workspace.js',import.meta.url),'utf8');
// Run the real response boundary independently from the sidebar DOM bootstrap.
const boundary=source.slice(source.indexOf('  const currentFeature='),source.indexOf('  const localHost='));
function fixture(page,channel){
  let blocked='star-day',reloads=0;
  const window={fetch:async()=>({headers:{get:key=>key==='X-Workspace-Feature-Disabled'?blocked:null}})};
  vm.runInNewContext(boundary,{page,document:{body:{dataset:{checkinChannel:channel}}},window,location:{reload(){reloads++;},replace(){}}});
  return {window,block(id){blocked=id;},reloads:()=>reloads};
}

test('a disabled dashboard dependency never causes a home reload loop',async()=>{
  const env=fixture('home');
  await env.window.fetch('/api/star-day/status');
  env.block('lingxi');await env.window.fetch('/api/lingxi/tasks');
  assert.equal(env.reloads(),0);
});

test('an open disabled business page reloads into its stopped page only once',async()=>{
  const env=fixture('star-day');
  await env.window.fetch('/api/star-day/status');await env.window.fetch('/api/star-day/status');
  assert.equal(env.reloads(),1);
});

test('the independent WeChat channel is not closed by the APP checkin switch',async()=>{
  const env=fixture('checkin','wechat');
  env.block('checkin');await env.window.fetch('/api/checkin/status');assert.equal(env.reloads(),0);
  env.block('wechat-checkin');await env.window.fetch('/api/checkin-wechat/status');assert.equal(env.reloads(),1);
});
