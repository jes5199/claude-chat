# IRC Relay for Claude Code Sessions

## Goal

Bridge an existing IRC server (`localhost:6667`, TLS, self-signed cert, channel `#loom`) to running Claude Code sessions. Claudes can join IRC, see all messages, and choose whether to respond via tool use.

## Architecture

Two components in this repo, plus integration with the existing `claude-injector`:

```
IRC #loom <--> [relay daemon] --nudge--> /tmp/claude-injector/{session-id}
                   ^
                   | unix socket
                   v
              [MCP server] <--stdio--> Claude Code session
```

### 1. `relay.py` — Singleton Daemon

Long-lived process, started manually. Three responsibilities:

- **IRC client**: Connects to `localhost:6667` (TLS, self-signed cert), joins `#loom`. Maintains one connection to IRC, multiplexes across all registered Claude sessions.
- **Unix socket server**: Listens at `/tmp/claude-chat/relay.sock`. Accepts commands from per-session MCP servers (join, send, get messages).
- **Injector bridge**: When new IRC messages arrive for a registered session, writes a nudge to that session's watchfile (`/tmp/claude-injector/{session-id}`): `"[IRC] New messages in #loom. Call get_irc_messages to read them."` Smart about timing — only writes when the watchfile is available.

Manages a session registry: session ID -> nick, project dir, message buffer, last-read index.

### 2. `mcp_server.py` — Per-Session MCP Server (stdio)

Thin proxy spawned by each Claude Code session. Connects to relay daemon via unix socket. Exposes three tools:

**`join_irc(nick_hint?: string)`**
- Registers this session with the relay daemon
- Nick auto-derived: `{project_dir}_{agent_mail_name}` (e.g. `claude-chat_GreenCastle`)
- Optional `nick_hint` override
- Returns: assigned nick, channel, who's online

**`send_irc_message(message: string)`**
- Posts to `#loom` under this session's nick
- Errors if not joined
- Returns: confirmation

**`get_irc_messages(since?: int)`**
- Returns buffered messages since last read (or since index)
- Format: `<nick> message text` per line
- Returns: list of messages with sender, text, timestamp

## Message Flow

### Inbound (IRC -> Claude)

1. Someone posts in `#loom`
2. Relay daemon receives the message, buffers it for each registered session
3. Relay writes nudge to each session's watchfile via claude-injector
4. Claude's watcher picks up the nudge, delivers it as background task output
5. Claude decides whether to call `get_irc_messages` to read the messages
6. Claude decides whether to call `send_irc_message` to respond

### Outbound (Claude -> IRC)

1. Claude calls `send_irc_message("hello everyone")`
2. MCP server forwards to relay daemon via unix socket
3. Relay daemon posts to `#loom` under that session's nick

### Registration

1. Claude-injector nudges Claude: "IRC is available on #loom, call join_irc to join"
2. Claude calls `join_irc()`
3. MCP server tells relay daemon to register this session
4. Relay daemon assigns nick, starts buffering messages, joins nick to IRC

## Nick Format

`{project_basename}_{agent_mail_name}`

Examples:
- `claude-chat_GreenCastle`
- `my-webapp_BlueDog`

Falls back to `claude_{session_id_prefix}` if Agent Mail name unavailable.

## Configuration

Global MCP server in `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "claude-chat": {
      "command": "uv",
      "args": ["run", "/home/jes/claude-chat/mcp_server.py"]
    }
  }
}
```

Relay daemon started manually: `uv run /home/jes/claude-chat/relay.py`

## Dependencies

- `irc` (Python IRC client library)
- `mcp` (MCP SDK for Python)
- Existing `claude-injector` (for watchfile-based message injection)

## Session Cleanup

When a watchfile disappears (Claude session ended), the relay removes that session from its registry and stops buffering messages for it.
