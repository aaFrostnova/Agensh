/** Optional host-only experimental evidence. Never mount its output in a worker. */
import { appendFileSync, closeSync, constants, openSync } from "node:fs";
import { randomUUID } from "node:crypto";
export type HostEvent = { event: string; [key: string]: unknown };
export type HostEventSink = (event: HostEvent) => void;
export function createHostEventSink(path: string | undefined, agent: string): HostEventSink | undefined {
  if (!path) return undefined;
  const fd = openSync(path, constants.O_CREAT | constants.O_WRONLY | constants.O_APPEND | constants.O_NOFOLLOW, 0o600);
  closeSync(fd);
  return (event) => {
    const out = openSync(path, constants.O_WRONLY | constants.O_APPEND | constants.O_NOFOLLOW);
    try {
      appendFileSync(out, JSON.stringify({ ...event, schema_version: 1,
        event_id: randomUUID(), t: Date.now() / 1000, agent_id: agent }) + "\n", "utf8");
    } finally { closeSync(out); }
  };
}
/** Only public output and tool lifecycle are retained; thought updates are ignored. */
export function projectSessionUpdate(params: unknown): HostEvent | null {
  if (!params || typeof params !== "object") return null;
  const envelope = params as Record<string, unknown>;
  const update = envelope.update;
  if (!update || typeof update !== "object") return null;
  const u = update as Record<string, unknown>;
  const session_id = typeof envelope.sessionId === "string" ? envelope.sessionId : undefined;
  if (u.sessionUpdate === "agent_message_chunk") {
    const c = u.content;
    if (!c || typeof c !== "object") return null;
    const content = c as Record<string, unknown>;
    if (content.type !== "text" || typeof content.text !== "string") return null;
    // Copilot emits this exact single-line SDK notice as agent_message_chunk.
    // Keep the notice as evidence, but do not forward it as model feedback.
    const sdkNotice = /^Info: Disabled tools: [A-Za-z0-9_.-]+(?:, [A-Za-z0-9_.-]+)+$/.test(content.text);
    return { event: sdkNotice ? "acp_runtime_notice" : "acp_visible_message", session_id, text: content.text };
  }
  if (u.sessionUpdate !== "tool_call" && u.sessionUpdate !== "tool_call_update") return null;
  if (typeof u.toolCallId !== "string") return null;
  const result: HostEvent = {
    event: u.sessionUpdate === "tool_call" ? "acp_tool_started" : "acp_tool_updated",
    session_id, tool_call_id: u.toolCallId,
  };
  for (const key of ["status", "kind", "title"] as const) {
    if (typeof u[key] === "string") result[key] = u[key];
  }
  if (u.rawInput !== undefined) {
    const raw = JSON.stringify(u.rawInput);
    if (raw.length <= 65536) result.raw_input = u.rawInput;
    else result.raw_input_truncated = true;
  }
  return result;
}
