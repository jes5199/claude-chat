import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import { basename } from 'path'
import { connect } from 'net'

// --- Config ---

const SOCKET_PATH = '/tmp/claude-chat/relay.sock'
const POLL_INTERVAL_MS = 2000
const projectDir = process.env.CLAUDE_PROJECT_DIR || process.cwd()
const nickHint = basename(projectDir)

// --- Relay client ---

interface RelayResponse {
  ok: boolean
  error?: string
  nick?: string
  channel?: string
  online?: string[]
  message_count?: number
  messages?: Array<{ index: number; nick: string; message: string; timestamp: number }>
  next_index?: number
  chunks?: number
  confirmed?: boolean
  warning?: string
  old_nick?: string
  new_nick?: string
  sessions?: Array<{
    nick: string
    session_id: string
    connected: boolean
    channel_joined: boolean
    unread: number
    nudges_pending: number
    last_active_ago: number
  }>
}

function relayCommand(cmd: Record<string, unknown>): Promise<RelayResponse> {
  return new Promise((resolve, reject) => {
    const sock = connect(SOCKET_PATH)
    let data = ''

    sock.on('connect', () => {
      sock.write(JSON.stringify(cmd) + '\n')
    })

    sock.on('data', (chunk) => {
      data += chunk.toString()
      if (data.includes('\n')) {
        sock.destroy()
        try {
          resolve(JSON.parse(data.trim()))
        } catch (e) {
          reject(new Error(`Bad relay response: ${data.trim()}`))
        }
      }
    })

    sock.on('error', (err) => {
      reject(new Error(`Relay connection failed: ${err.message}. Is irc-relay.service running?`))
    })

    sock.setTimeout(5000, () => {
      sock.destroy()
      reject(new Error('Relay timeout'))
    })
  })
}

// --- Session state ---

let sessionId = ''
let currentNick = ''
let joined = false
let lastIndex = 0

// Generate a session ID from project dir (deterministic so reconnects work)
function deriveSessionId(): string {
  // Use a hash of the project dir as a stable session ID
  const hash = new Bun.CryptoHasher('md5')
  hash.update(projectDir)
  const hex = hash.digest('hex')
  // Format as UUID-like string
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20, 32)].join('-')
}

// --- Auto-join ---

async function ensureJoined(): Promise<string | null> {
  if (joined) return null
  try {
    // We need a lockfile for the session ID. Create one if it doesn't exist.
    const lockDir = '/tmp/friendly-claude-message-alerts'
    const lockPath = `${lockDir}/${sessionId}.lock`
    const { mkdirSync, writeFileSync, existsSync } = await import('fs')
    mkdirSync(lockDir, { recursive: true })
    if (!existsSync(lockPath)) {
      writeFileSync(lockPath, String(process.pid))
    }

    const resp = await relayCommand({ cmd: 'join', session_id: sessionId, nick: nickHint })
    if (!resp.ok) return resp.error || 'join failed'
    currentNick = resp.nick || nickHint
    joined = true
    // Start reading from current position (don't replay old messages)
    if (resp.message_count !== undefined) {
      lastIndex = resp.message_count
    }
    process.stderr.write(`claude-chat: joined #loom as ${currentNick} (session ${sessionId.slice(0, 8)})\n`)
    return null
  } catch (e) {
    return e instanceof Error ? e.message : String(e)
  }
}

// --- MCP Server ---

const mcp = new Server(
  { name: 'claude-chat', version: '0.0.1' },
  {
    capabilities: {
      tools: {},
      experimental: {
        'claude/channel': {},
      },
    },
    instructions: [
      `IRC relay tools for #loom channel. Messages arrive as <channel source="claude-chat" from="..." message_id="...">`,
      `You are automatically connected to IRC. Use send_irc_message to talk, get_irc_messages to read history.`,
      `Call join_irc only if you need to reconnect or change nick.`,
    ].join('\n'),
  },
)

// --- Tools ---

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'join_irc',
      description: 'Join or rejoin IRC channel #loom. Usually auto-joined on startup.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          nick_hint: { type: 'string', description: 'Preferred nickname (defaults to project dir name)' },
        },
      },
    },
    {
      name: 'send_irc_message',
      description: 'Send a message to #loom IRC channel.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          message: { type: 'string', description: 'Message to send' },
        },
        required: ['message'],
      },
    },
    {
      name: 'get_irc_messages',
      description: 'Get recent IRC messages (usually delivered via notifications, but useful for history).',
      inputSchema: {
        type: 'object' as const,
        properties: {
          since: { type: 'number', description: 'Message index to read from (default: last read position)' },
        },
      },
    },
    {
      name: 'irc_status',
      description: 'Show all connected IRC sessions and their status.',
      inputSchema: { type: 'object' as const, properties: {} },
    },
    {
      name: 'change_nick',
      description: 'Change your IRC nickname.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          nick: { type: 'string', description: 'New nickname' },
        },
        required: ['nick'],
      },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  const args = (req.params.arguments ?? {}) as Record<string, unknown>
  try {
    switch (req.params.name) {
      case 'join_irc': {
        const nick = (args.nick_hint as string) || nickHint
        const lockDir = '/tmp/friendly-claude-message-alerts'
        const lockPath = `${lockDir}/${sessionId}.lock`
        const { mkdirSync, writeFileSync } = await import('fs')
        mkdirSync(lockDir, { recursive: true })
        writeFileSync(lockPath, String(process.pid))

        const resp = await relayCommand({ cmd: 'join', session_id: sessionId, nick })
        if (!resp.ok) {
          return { content: [{ type: 'text', text: `Failed to join: ${resp.error}` }], isError: true }
        }
        currentNick = resp.nick || nick
        joined = true
        const online = resp.online?.join(', ') || 'unknown'
        const buffered = resp.message_count ?? 0
        return {
          content: [{
            type: 'text',
            text: `Joined #loom as ${currentNick}\nOnline: ${online}\nBuffered messages: ${buffered}\nUse get_irc_messages to read messages, send_irc_message to talk.`,
          }],
        }
      }

      case 'send_irc_message': {
        const err = await ensureJoined()
        if (err) return { content: [{ type: 'text', text: `Not connected: ${err}` }], isError: true }

        const message = args.message as string
        const resp = await relayCommand({ cmd: 'send', session_id: sessionId, message })
        if (!resp.ok) {
          if (resp.error === 'not joined') {
            joined = false
            return { content: [{ type: 'text', text: 'Session lost — call join_irc to reconnect.' }], isError: true }
          }
          return { content: [{ type: 'text', text: `Send failed: ${resp.error}` }], isError: true }
        }
        let text = `Sent as <${currentNick}>`
        if (resp.chunks && resp.chunks > 1) text += ` (split into ${resp.chunks} messages due to IRC length limits)`
        if (resp.confirmed === false) text += `\nWARNING: ${resp.warning}`
        return { content: [{ type: 'text', text }] }
      }

      case 'get_irc_messages': {
        const err = await ensureJoined()
        if (err) return { content: [{ type: 'text', text: `Not connected: ${err}` }], isError: true }

        const since = args.since as number | undefined
        const cmd: Record<string, unknown> = { cmd: 'get', session_id: sessionId }
        if (since !== undefined && since >= 0) cmd.since = since

        const resp = await relayCommand(cmd)
        if (!resp.ok) {
          if (resp.error?.includes('not joined') || resp.error?.includes('relay lost')) {
            joined = false
            return { content: [{ type: 'text', text: 'Session lost — call join_irc to reconnect.' }], isError: true }
          }
          return { content: [{ type: 'text', text: `Failed: ${resp.error}` }], isError: true }
        }

        if (!resp.messages || resp.messages.length === 0) {
          return { content: [{ type: 'text', text: 'No new messages.' }] }
        }

        if (resp.next_index !== undefined) lastIndex = resp.next_index
        const lines = resp.messages.map(m => `[${m.index}] <${m.nick}> ${m.message}`)
        return { content: [{ type: 'text', text: lines.join('\n') }] }
      }

      case 'irc_status': {
        const resp = await relayCommand({ cmd: 'status' })
        if (!resp.ok) {
          return { content: [{ type: 'text', text: `Failed: ${resp.error}` }], isError: true }
        }
        if (!resp.sessions || resp.sessions.length === 0) {
          return { content: [{ type: 'text', text: 'No sessions connected.' }] }
        }
        const lines = resp.sessions
          .filter(s => s.nick !== 'relay')
          .map(s => {
            const parts: string[] = []
            if (!s.connected || !s.channel_joined) parts.push('DISCONNECTED')
            if (s.unread > 0) parts.push(`${s.unread} unread`)
            if (s.nudges_pending > 0) parts.push(`${s.nudges_pending} nudges pending`)
            const status = parts.length ? parts.join(', ') : 'ok'
            return `  ${s.nick} (${s.session_id.slice(0, 8)}): ${status} [active ${Math.round(s.last_active_ago)}s ago]`
          })
        return { content: [{ type: 'text', text: `IRC Sessions:\n${lines.join('\n')}` }] }
      }

      case 'change_nick': {
        const err = await ensureJoined()
        if (err) return { content: [{ type: 'text', text: `Not connected: ${err}` }], isError: true }

        const nick = args.nick as string
        const resp = await relayCommand({ cmd: 'nick', session_id: sessionId, nick })
        if (!resp.ok) {
          return { content: [{ type: 'text', text: `Nick change failed: ${resp.error}` }], isError: true }
        }
        currentNick = resp.new_nick || nick
        return { content: [{ type: 'text', text: `Nick changed: ${resp.old_nick} → ${resp.new_nick}` }] }
      }

      default:
        return { content: [{ type: 'text', text: `Unknown tool: ${req.params.name}` }], isError: true }
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return { content: [{ type: 'text', text: `${req.params.name} failed: ${msg}` }], isError: true }
  }
})

// --- Polling loop: push IRC messages as channel notifications ---

let polling = true

const pollInterval = setInterval(async () => {
  if (!polling) return

  // Auto-rejoin if session was lost (e.g. relay restarted)
  if (!joined) {
    const err = await ensureJoined()
    if (err) {
      process.stderr.write(`claude-chat: auto-rejoin failed: ${err}\n`)
      return
    }
    process.stderr.write('claude-chat: auto-rejoined after session loss\n')
  }

  try {
    const resp = await relayCommand({ cmd: 'get', session_id: sessionId, since: lastIndex })
    if (!resp.ok) {
      if (resp.error?.includes('not joined') || resp.error?.includes('relay lost')) {
        joined = false
        process.stderr.write('claude-chat: session lost, will auto-rejoin next poll\n')
      }
      return
    }

    if (!resp.messages || resp.messages.length === 0) return
    if (resp.next_index !== undefined) lastIndex = resp.next_index

    for (const msg of resp.messages) {
      // Skip our own messages
      if (msg.nick === currentNick) continue

      mcp.notification({
        method: 'notifications/claude/channel',
        params: {
          content: msg.message,
          meta: {
            from: msg.nick,
            message_id: String(msg.index),
            ts: new Date(msg.timestamp * 1000).toISOString(),
          },
        },
      }).catch(err => {
        process.stderr.write(`claude-chat: notification failed for msg #${msg.index}: ${err}\n`)
      })
    }
  } catch (err) {
    process.stderr.write(`claude-chat: poll error: ${err}\n`)
  }
}, POLL_INTERVAL_MS)

// --- Shutdown ---

let shuttingDown = false
function shutdown(): void {
  if (shuttingDown) return
  shuttingDown = true
  polling = false
  clearInterval(pollInterval)
  // Clean up lockfile
  try {
    const { unlinkSync } = require('fs')
    unlinkSync(`/tmp/friendly-claude-message-alerts/${sessionId}.lock`)
    unlinkSync(`/tmp/friendly-claude-message-alerts/${sessionId}`)
  } catch {}
  process.stderr.write('claude-chat: shutting down\n')
  process.exit(0)
}

process.stdin.on('end', shutdown)
process.stdin.on('close', shutdown)
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
process.on('unhandledRejection', err => {
  process.stderr.write(`claude-chat: unhandled rejection: ${err}\n`)
})
process.on('uncaughtException', err => {
  process.stderr.write(`claude-chat: uncaught exception: ${err}\n`)
})

// --- Startup ---

sessionId = deriveSessionId()
process.stderr.write(`claude-chat: starting for ${nickHint} (session ${sessionId.slice(0, 8)})\n`)

// Auto-join IRC
const joinErr = await ensureJoined()
if (joinErr) {
  process.stderr.write(`claude-chat: auto-join failed (will retry on tool call): ${joinErr}\n`)
}

// Connect MCP transport
await mcp.connect(new StdioServerTransport())
