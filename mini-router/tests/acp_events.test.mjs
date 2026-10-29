import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHostEventSink, projectSessionUpdate } from '../dist/acp_events.js';
import { defaultRenderUEM } from '../dist/uem_render.js';

test('thought and unknown ACP updates are never journaled', () => {
  assert.equal(projectSessionUpdate({sessionId: 's', update: {sessionUpdate: 'agent_thought_chunk', content: {type: 'text', text: 'private'}}}), null);
  assert.equal(projectSessionUpdate({sessionId: 's', update: {sessionUpdate: 'plan'}}), null);
});
test('exact SDK notice is distinct from model feedback without discarding later prose', () => {
  const project = text => projectSessionUpdate({sessionId:'s',update:{sessionUpdate:'agent_message_chunk',content:{type:'text',text}}});
  assert.equal(project('Info: Disabled tools: list_agents, gitea-delete_file').event,'acp_runtime_notice');
  assert.equal(project('Info: Disabled tools: list_agents, gitea-delete_file\nI will inspect the source.').event,'acp_visible_message');
  assert.equal(project('Info: I will inspect the source.').event,'acp_visible_message');
});
test('visible text and tool lifecycle are projected with stable IDs', () => {
  assert.deepEqual(projectSessionUpdate({sessionId:'s', update:{sessionUpdate:'agent_message_chunk',content:{type:'text',text:'public'}}}), {event:'acp_visible_message',session_id:'s',text:'public'});
  assert.deepEqual(projectSessionUpdate({sessionId:'s', update:{sessionUpdate:'tool_call',toolCallId:'tool1',status:'pending',rawInput:{command:'pytest'}}}), {event:'acp_tool_started',session_id:'s',tool_call_id:'tool1',status:'pending',raw_input:{command:'pytest'}});
  assert.equal(projectSessionUpdate({update:{sessionUpdate:'tool_call_update',toolCallId:'tool1',status:'completed'}}).status, 'completed');
});
test('journal is opt in, host stamped, and refuses symlink destinations', () => {
  assert.equal(createHostEventSink(undefined,'victim'), undefined);
  const dir=mkdtempSync(join(tmpdir(),'troy-journal-'));
  try {
    const path=join(dir,'events.jsonl');
    const sink=createHostEventSink(path,'victim');
    sink({event:'acp_prompt_started',turn_id:'turn1',agent_id:'forged'});
    const row=JSON.parse(readFileSync(path,'utf8'));
    assert.equal(row.agent_id,'victim');
    assert.equal(row.turn_id,'turn1');
    assert.equal(row.schema_version,1);
    assert.ok(row.event_id && row.t);
    symlinkSync(path,join(dir,'alias'));
    assert.throws(()=>createHostEventSink(join(dir,'alias'),'victim'));
  } finally {rmSync(dir,{recursive:true,force:true});}
});
test('Troy optional renderer preserves exact multiline stimulus; default unchanged', () => {
  const text='First paragraph.\n\nSecond paragraph.\n  Preserve indentation.';
  const uem={event_id:'mattermost:post:p1:0',source:'mattermost',kind:'dm',observed_at:1,target_agent:'victim',summary:'peer in DM: First paragraph. Second paragraph. Preserve indentation.',raw:{data:{post:JSON.stringify({id:'p1',message:text})}}};
  const prior=process.env.TROY_PRESERVE_MESSAGE_TEXT;
  try {
    delete process.env.TROY_PRESERVE_MESSAGE_TEXT;
    assert.ok(defaultRenderUEM(uem).includes(uem.summary));
    process.env.TROY_PRESERVE_MESSAGE_TEXT='1';
    assert.ok(defaultRenderUEM(uem).includes(text));
    assert.ok(defaultRenderUEM(uem).includes('event_id=mattermost:post:p1:0'));
  } finally {if(prior===undefined)delete process.env.TROY_PRESERVE_MESSAGE_TEXT;else process.env.TROY_PRESERVE_MESSAGE_TEXT=prior;}
});
