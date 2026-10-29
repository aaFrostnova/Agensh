import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:net';
import { ACPSocketChannel } from '../dist/acp_socket_channel.js';

test('real ACP socket journals tool and visible response lifecycle without thought or raw prompt', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'troy-acp-'));
  const path = join(dir, 'agent.sock');
  const sockets = new Set();
  const events = [], logs = [];
  let promptedText;
  const server = createServer(socket => {
    sockets.add(socket); socket.on('close', () => sockets.delete(socket));
    let pending = '';
    const send = value => socket.write(JSON.stringify(value) + '\n');
    socket.on('data', data => {
      pending += data.toString('utf8');
      let pos;
      while ((pos = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0, pos); pending = pending.slice(pos + 1);
        if (!line.trim()) continue;
        const request = JSON.parse(line);
        if (request.method === 'initialize') send({jsonrpc:'2.0', id:request.id, result:{protocolVersion:1, agentCapabilities:{loadSession:false}, agentInfo:{name:'fake',version:'1'}}});
        else if (request.method === 'session/new') send({jsonrpc:'2.0', id:request.id, result:{sessionId:'s1'}});
        else if (request.method === 'session/prompt') {
          promptedText = request.params.prompt[0].text;
          const update = value => send({jsonrpc:'2.0',method:'session/update',params:{sessionId:'s1',update:value}});
          update({sessionUpdate:'agent_thought_chunk',content:{type:'text',text:'PRIVATE_THOUGHT_SENTINEL'}});
          update({sessionUpdate:'tool_call',toolCallId:'tool1',title:'Run tests',kind:'execute',status:'in_progress',rawInput:{command:'pytest'}});
          update({sessionUpdate:'tool_call_update',toolCallId:'tool1',status:'completed'});
          update({sessionUpdate:'agent_message_chunk',content:{type:'text',text:'Tests still fail; I will stop.'}});
          send({jsonrpc:'2.0',id:request.id,result:{stopReason:'end_turn'}});
        }
      }
    });
  });
  await new Promise(resolve => server.listen(path, resolve));
  let savedSession = null;
  const channel = new ACPSocketChannel({socketPath:path,cwd:'/work',sessionStore:{get:async()=>savedSession,set:async id=>{savedSession=id;}},onEvent:event=>events.push(event),onLog:(level,text)=>logs.push({level,text})});
  try {
    const result = await channel.prompt([{event_id:'mattermost:post:p1:0',source:'mattermost',kind:'dm',target_agent:'v',observed_at:1,summary:'PUBLIC_PROMPT_SENTINEL'}]);
    assert.equal(result.stopReason,'end_turn');
    assert.ok(promptedText.includes('PUBLIC_PROMPT_SENTINEL'));
    assert.deepEqual(events.map(e=>e.event), ['acp_prompt_started','acp_tool_started','acp_tool_updated','acp_visible_message','acp_prompt_finished']);
    assert.deepEqual(events[0].source_event_ids,['mattermost:post:p1:0']);
    assert.equal(events[0].turn_id,events[4].turn_id);
    assert.equal(events[3].text,'Tests still fail; I will stop.');
    assert.ok(!JSON.stringify(events).includes('PRIVATE_THOUGHT_SENTINEL'));
    assert.ok(!JSON.stringify(events).includes('PUBLIC_PROMPT_SENTINEL'));
    assert.ok(!JSON.stringify(logs).includes('PRIVATE_THOUGHT_SENTINEL'));
  } finally {
    await channel.close();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve=>server.close(resolve));
    rmSync(dir,{recursive:true,force:true});
  }
});
