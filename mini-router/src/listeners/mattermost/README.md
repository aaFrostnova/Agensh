# Mattermost messages

Chat activity is ingested live and recovered through REST after reconnects.

| File | Purpose |
|---|---|
| `ws.ts` | Opens `/api/v4/websocket`, authenticates, pings and reconnects with jitter |
| `listener.ts` | Filters events, ingests messages/reactions and performs staggered REST catch-up |
| `notification.ts` | Normalizes posts, edits, deletions and reactions; builds IDs and summaries |

New direct posts have kind `dm`; their summary bodies are untruncated and
normalized to one line for mid-turn delivery. Channel/group message bodies
in summaries are capped at 200 characters, with sender/channel prefixes added
outside that cap and full post text kept in the raw event. Edits and deletions
retain their own kinds, so even DM edits/deletions wait for a queued turn.
Self-authored and system posts are filtered where identity is available.
REST catch-up resolves the current user
and caches team channels, then fetches posts after its process-local timestamp;
a fresh router starts with a ten-minute lookback. This path recovers posts
from those cached channels; it does not replay WebSocket reaction/deletion
events or refresh the channel list on every reconnect.

The [dispatcher](../../dispatcher.ts) handles turn delivery. The
[MCP shim](../../../mcp-notice-shim.cjs) handles urgent DMs, while
[`group_messages.ts`](../../group_messages.ts) stores large channel backlogs.
