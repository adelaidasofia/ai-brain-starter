/**
 * whatsapp-vault-sync (ai-brain-starter edition)
 *
 * Connects to personal WhatsApp via QR code (WhatsApp Web protocol),
 * pulls full chat history, and writes one markdown file per contact
 * to your Obsidian vault. Re-runs are incremental — only new messages.
 *
 * Usage:
 *   cd scripts/whatsapp && npm install
 *   VAULT_ROOT=/path/to/vault node sync.mjs
 *   VAULT_ROOT=/path/to/vault node sync.mjs --groups   # include group chats
 *
 * VAULT_ROOT is optional if this script lives inside your vault
 * (e.g. <vault>/scripts/whatsapp/sync.mjs) — it auto-detects 3 levels up.
 *
 * Session is saved in baileys_auth/ — no re-scan on subsequent runs.
 * Output: $VAULT_ROOT/🤖 AI Chats/WhatsApp/  (override with WA_OUTPUT env var)
 */

import makeWASocket, {
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
  isJidGroup,
} from '@whiskeysockets/baileys'
import qrcodeTerminal from 'qrcode-terminal'
import pino from 'pino'
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

// ── Config ────────────────────────────────────────────────────────────────────

const __dir = path.dirname(fileURLToPath(import.meta.url))
const INCLUDE_GROUPS = process.argv.includes('--groups')

// Auto-detect vault root: env override, or walk 3 levels up from this file
// (scripts/whatsapp/sync.mjs → scripts/whatsapp → scripts → vault root)
const VAULT_ROOT = process.env.VAULT_ROOT || path.resolve(__dir, '..', '..')

const OUTPUT_DIR = path.join(VAULT_ROOT, process.env.WA_OUTPUT || '🤖 AI Chats/WhatsApp')
const AUTH_DIR   = path.join(__dir, 'baileys_auth')
const STORE_FILE = path.join(__dir, 'baileys_store.json')

const silent = pino({ level: 'silent' })

// ── Store: load / save ────────────────────────────────────────────────────────

function loadStore() {
  if (fs.existsSync(STORE_FILE)) {
    try {
      const raw = JSON.parse(fs.readFileSync(STORE_FILE, 'utf8'))
      return {
        contacts: raw.contacts || {},
        messages: new Map(Object.entries(raw.messages || {})),
      }
    } catch { /* corrupt — start fresh */ }
  }
  return { contacts: {}, messages: new Map() }
}

function saveStore(store) {
  fs.writeFileSync(STORE_FILE, JSON.stringify({
    contacts: store.contacts,
    messages: Object.fromEntries(store.messages),
  }), 'utf8')
}

// ── Formatting ────────────────────────────────────────────────────────────────

function formatTime(unix) {
  return new Date(Number(unix) * 1000).toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit', hour12: true,
  })
}

function formatDate(unix) {
  return new Date(Number(unix) * 1000).toISOString().split('T')[0]
}

function sanitizeFilename(name) {
  return name.replace(/[/\\?%*:|"<>[\]]/g, '-').trim() || 'Unknown'
}

function getContactName(jid, contacts) {
  const bare = jid.split('@')[0]
  const c = contacts[jid]
           || contacts[bare + '@c.us']
           || contacts[bare + '@s.whatsapp.net']
  return c?.notify || c?.name || ('+' + bare)
}

// ── Message text extraction ───────────────────────────────────────────────────

function extractText(msg) {
  if (!msg?.message) return null
  const m = msg.message

  if (m.conversation)              return m.conversation
  if (m.extendedTextMessage?.text) return m.extendedTextMessage.text
  if (m.imageMessage)              return m.imageMessage.caption  ? `[Image: ${m.imageMessage.caption}]`  : '[Image]'
  if (m.videoMessage)              return m.videoMessage.caption  ? `[Video: ${m.videoMessage.caption}]`  : '[Video]'
  if (m.audioMessage)              return m.audioMessage.ptt      ? '[Voice note]'                        : '[Audio]'
  if (m.documentMessage)           return `[Document: ${m.documentMessage.fileName || 'file'}]`
  if (m.stickerMessage)            return '[Sticker]'
  if (m.contactMessage)            return `[Contact shared: ${m.contactMessage.displayName}]`
  if (m.locationMessage)           return '[Location]'
  if (m.pollCreationMessage)       return `[Poll: ${m.pollCreationMessage.name}]`
  if (m.ephemeralMessage)          return extractText({ message: m.ephemeralMessage.message })
  if (m.viewOnceMessage)           return '[View-once media]'
  if (m.reactionMessage)           return null   // skip emoji reactions
  if (m.protocolMessage)           return null   // skip system events
  if (m.pollUpdateMessage)         return null   // skip vote events
  return null
}

// ── Markdown builder ──────────────────────────────────────────────────────────

function buildMarkdown(displayName, phone, messages) {
  const byDate = {}
  let count = 0

  for (const msg of messages) {
    const ts = Number(msg.messageTimestamp)
    if (!ts || ts < 1000) continue
    const text = extractText(msg)
    if (!text) continue

    const date = formatDate(ts)
    if (!byDate[date]) byDate[date] = []
    byDate[date].push(`**${formatTime(ts)}** ${msg.key?.fromMe ? 'You' : displayName}: ${text}`)
    count++
  }

  const dates = Object.keys(byDate).sort()
  if (dates.length === 0) return null

  const today = new Date().toISOString().split('T')[0]

  const lines = [
    '---',
    `type: whatsapp-chat`,
    `contact: "${displayName}"`,
    `phone: "+${phone}"`,
    `message_count: ${count}`,
    `first_message: ${dates[0]}`,
    `last_message: ${dates[dates.length - 1]}`,
    `last_sync: ${today}`,
    '---',
    '',
    `# WhatsApp: ${displayName}`,
    '',
  ]

  for (const date of dates) {
    lines.push(`## ${date}`, '', ...byDate[date], '')
  }

  return lines.join('\n')
}

// ── Export ────────────────────────────────────────────────────────────────────

function exportToVault(store) {
  console.log('\nExporting to vault...')
  fs.mkdirSync(OUTPUT_DIR, { recursive: true })

  let synced = 0, skippedGroups = 0, skippedEmpty = 0

  for (const [jid, messages] of store.messages) {
    const isGroup = isJidGroup(jid) || jid.includes('@broadcast')
    if (isGroup && !INCLUDE_GROUPS) { skippedGroups++; continue }

    const phone       = jid.split('@')[0]
    const displayName = isGroup
      ? (store.contacts[jid]?.name || store.contacts[jid]?.notify || jid.split('@')[0])
      : getContactName(jid, store.contacts)

    if (!messages?.length) { skippedEmpty++; continue }

    const markdown = buildMarkdown(displayName, phone, messages)
    if (!markdown) { skippedEmpty++; continue }

    fs.writeFileSync(
      path.join(OUTPUT_DIR, sanitizeFilename(displayName) + '.md'),
      markdown, 'utf8'
    )
    synced++
    if (synced % 25 === 0) process.stdout.write(`  ${synced} written...\r`)
  }

  saveStore(store)
  fs.writeFileSync(path.join(__dir, '.last_sync'), new Date().toISOString())

  console.log(`\nDone.`)
  console.log(`  ${synced} conversations → ${OUTPUT_DIR}`)
  if (skippedGroups > 0) console.log(`  ${skippedGroups} groups skipped  (re-run with --groups to include)`)
  console.log(`\nSafe to close this window.`)
}

// ── Connection ────────────────────────────────────────────────────────────────

async function connect() {
  fs.mkdirSync(AUTH_DIR, { recursive: true })

  const isFirstRun = !fs.existsSync(STORE_FILE)
  const store      = loadStore()

  console.log(isFirstRun
    ? 'First run — requesting full history from WhatsApp...'
    : 'Previous session found — fetching new messages only...')
  console.log(`Output: ${OUTPUT_DIR}\n`)

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  const { version }          = await fetchLatestBaileysVersion()

  const sock = makeWASocket({
    version,
    auth:                state,
    logger:              silent,
    printQRInTerminal:   false,
    syncFullHistory:     true,
    markOnlineOnConnect: false,
    browser:             ['WhatsApp Vault Sync', 'Desktop', '1.0.0'],
  })

  sock.ev.on('creds.update', saveCreds)

  // ── Accumulate contacts from all sources
  const mergeContacts = (contacts) => {
    for (const c of contacts || []) {
      if (c.id) store.contacts[c.id] = { ...store.contacts[c.id], ...c }
    }
  }
  sock.ev.on('contacts.set',    ({ contacts }) => mergeContacts(contacts))
  sock.ev.on('contacts.upsert', (contacts)     => mergeContacts(contacts))

  // ── Accumulate messages, deduplicating by message ID
  const mergeMessages = (msgs) => {
    for (const msg of msgs || []) {
      const jid = msg.key?.remoteJid
      if (!jid) continue
      if (!store.messages.has(jid)) store.messages.set(jid, [])
      const arr = store.messages.get(jid)
      if (!arr.find(m => m.key?.id === msg.key?.id)) arr.push(msg)
    }
  }
  sock.ev.on('messages.set', ({ messages }) => mergeMessages(messages))

  // ── History sync — fires in chunks; export when stream goes quiet for 5 s
  let historyDone = false
  let idleTimer   = null
  // 0 = no history chunk has EVER arrived. Read by the connection-open fallback
  // below so it can tell "this account emits no history" apart from "history is
  // still streaming" (see the truncation note there).
  let lastChunkAt = 0

  const scheduleIdleCheck = () => {
    if (idleTimer) clearTimeout(idleTimer)
    idleTimer = setTimeout(() => {
      if (!historyDone) {
        historyDone = true
        exportToVault(store)
        process.exit(0)
      }
    }, 5_000)
  }

  sock.ev.on('messaging-history.set', ({ chats, contacts, messages, isLatest }) => {
    lastChunkAt = Date.now()
    mergeContacts(contacts || [])
    mergeMessages(messages || [])

    process.stdout.write(
      `  History: ${store.messages.size} chats | +${(messages || []).length} messages\r`
    )

    if (isLatest && !historyDone) {
      historyDone = true
      if (idleTimer) clearTimeout(idleTimer)
      setTimeout(() => { exportToVault(store); process.exit(0) }, 1_500)
      return
    }

    scheduleIdleCheck()
  })

  // ── Connection lifecycle
  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      process.stdout.write('\x1Bc')
      console.log('────────────────────────────────────────────────────────')
      console.log('  whatsapp-vault-sync — Scan to connect')
      console.log('────────────────────────────────────────────────────────')
      console.log('  On your phone:')
      console.log('  Settings › Linked Devices › Link a Device\n')
      qrcodeTerminal.generate(qr, { small: true })
      console.log('\n  (QR expires in ~20 s and refreshes automatically)\n')
    }

    if (connection === 'open') {
      console.log('\nConnected. Receiving history...\n')
      // Fallback for already-synced sessions that emit NO history events at all.
      // It must only fire in that case. It used to fire whenever history had not
      // finished by 20s, but on a large account the `messaging-history.set`
      // chunks are still streaming then, so it exported a PARTIAL history and
      // exited 0 with no warning -- silent data loss the user could not see.
      // When chunks are flowing, the 5s idle timer owns the exit instead: it
      // waits for the stream to actually go quiet.
      setTimeout(() => {
        if (historyDone) return
        if (lastChunkAt !== 0) return   // history is streaming; let the idle timer finish it
        historyDone = true
        if (idleTimer) clearTimeout(idleTimer)
        console.log('No history was sent for this session; exporting what is stored locally.')
        exportToVault(store)
        process.exit(0)
      }, 20_000)
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        console.log('\nLogged out. Delete baileys_auth/ and run again.')
        process.exit(1)
      }
      console.log('Connection dropped — reconnecting...')
      setTimeout(connect, 3_000)
    }
  })
}

connect().catch(err => {
  console.error('\nFatal error:', err.message)
  process.exit(1)
})
