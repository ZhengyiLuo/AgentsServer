#!/usr/bin/env node
'use strict'

const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const net = require('node:net')
const crypto = require('node:crypto')
const http = require('node:http')
const https = require('node:https')
const { spawnSync } = require('node:child_process')

const HELP = `Usage: agentsdock-server install [--port PORT] [--bind IP] [--non-interactive] [--dry-run]
       agentsdock-server recover
       agentsdock-server update --server-url URL --server-identity ID --server-instance-id ID
         --token-file PATH --manifest PATH --signature PATH
       agentsdock-server --version

install: fresh default user service only; existing installations must use managed updates.
recover: on the server computer, recover an interrupted pre-activation migration; then retry the app update.
update: verify the signed descriptor and request an update when the exact server is idle.
No installation hooks run when this npm package is installed.
`
const ROOT_SELECTORS = ['AGENTS_SERVER_INSTALL_DIR', 'AGENTS_SERVER_CONFIG_DIR', 'AGENTS_SERVER_STATE_DIR', 'AGENTSDOCK_STATE_DIR', 'ZENITHBOT_AGENT_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME']

function parse(argv) {
  const [command = 'help', ...args] = argv
  if (['help', '--help', '-h', '--version'].includes(command)) {
    if (args.length) throw new Error('Unexpected arguments.')
    return { command }
  }
  const values = command === 'install' ? ['--port', '--bind'] : command === 'update'
    ? ['--server-url', '--server-identity', '--server-instance-id', '--token-file', '--manifest', '--signature'] : []
  if (!['install', 'update', 'recover'].includes(command)) throw new Error('Use install, update, recover, or --help.')
  const flags = command === 'install' ? ['--non-interactive', '--dry-run'] : []
  const options = {}
  for (let index = 0; index < args.length; index++) {
    const name = args[index]
    if (Object.hasOwn(options, name)) throw new Error(`Duplicate option: ${name}`)
    if (flags.includes(name)) options[name] = true
    else if (values.includes(name)) {
      const value = args[++index]
      if (!value || value.startsWith('--') || /[\x00-\x1f\x7f]/.test(value)) throw new Error(`Missing or invalid value for ${name}`)
      options[name] = value
    } else throw new Error(`Unsupported option: ${name}`)
  }
  if (command === 'update') for (const name of values) if (!options[name]) throw new Error(`Required option: ${name}`)
  if (options['--port'] && (!/^[1-9][0-9]{0,4}$/.test(options['--port']) || Number(options['--port']) > 65535)) throw new Error('Port must be an integer from 1 to 65535.')
  if (options['--bind'] && !net.isIP(options['--bind'])) throw new Error('Bind address must be a literal IP address.')
  return { command, options }
}

function readRegular(filename, maxBytes, { privateFile = false, uid = process.getuid?.() } = {}) {
  const descriptor = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0))
  try {
    const info = fs.fstatSync(descriptor)
    if (!info.isFile() || info.size > maxBytes || info.nlink !== 1) throw new Error('Expected a bounded regular file.')
    if (privateFile && (info.uid !== uid || (info.mode & 0o077) !== 0)) throw new Error('Token file must be owned by the current user and readable only by that user.')
    const bytes = fs.readFileSync(descriptor)
    if (bytes.length > maxBytes) throw new Error('File exceeded its size limit.')
    return bytes
  } finally { fs.closeSync(descriptor) }
}

function ensureFreshInstall(context) {
  if (context.uid === 0) throw new Error('Run as the user who will own the server, without sudo.')
  if (!['darwin', 'linux'].includes(context.platform)) throw new Error('Server installation requires Linux or Apple silicon macOS.')
  for (const name of ROOT_SELECTORS) if (context.env[name]) throw new Error(`Custom installation selector ${name} is unsupported by the fresh-install CLI. Use the existing server's managed updater.`)
  const existingInstall = () => new Error('An existing server installation or state was found. Use AgentsDock or agentsdock-server update; no installer was started.')
  const safeDirectory = info => info.isDirectory() && !info.isSymbolicLink() && info.uid === context.uid && (info.mode & 0o022) === 0 && (info.mode & 0o500) === 0o500
  const allowEmptyScaffold = (relative, child) => {
    const root = path.join(context.home, relative)
    let rootInfo
    try { rootInfo = fs.lstatSync(root) } catch (error) { if (error.code !== 'ENOENT') throw error }
    if (!rootInfo) return
    // Failed bootstrap or preactivation can leave these exact empty directories.
    // Never delete them or admit files, credentials, histories, links or locks.
    // install.sh repeats this check under exclusive installation ownership.
    if (!safeDirectory(rootInfo)) throw existingInstall()
    const entries = fs.readdirSync(root)
    if (entries.length > 1 || (entries.length === 1 && entries[0] !== child)) throw existingInstall()
    if (entries.length === 1) {
      const nested = path.join(root, child)
      if (!safeDirectory(fs.lstatSync(nested)) || fs.readdirSync(nested).length !== 0) throw existingInstall()
    }
  }
  allowEmptyScaffold('.local/share/agents-server', 'releases')
  allowEmptyScaffold('.config/agents-server')
  allowEmptyScaffold('.agentsdock', 'admin')
  const targets = [
    '.zenithbot-agent',
    '.config/systemd/user/agents-server.service', '.config/systemd/user/zenithbot-agent.service',
    'Library/LaunchAgents/com.agentsdock.server.plist',
  ]
  for (const relative of targets) {
    try {
      fs.lstatSync(path.join(context.home, relative))
      throw existingInstall()
    } catch (error) { if (error.code !== 'ENOENT') throw error }
  }
  if (context.platform === 'darwin') {
    const check = context.spawn('/bin/launchctl', ['print', `gui/${context.uid}/com.agentsdock.server`], { encoding: 'utf8', timeout: 10000 })
    if (check.status === 0) throw new Error('An AgentsServer service already exists. Use its managed updater.')
    if (check.error || !/could not find service|service.*not found/i.test(`${check.stdout || ''} ${check.stderr || ''}`)) throw new Error('Could not verify the user service is absent. No installer was started.')
  } else {
    for (const unit of ['agents-server.service', 'zenithbot-agent.service']) {
      const check = context.spawn('systemctl', ['--user', 'show', unit, '--property=LoadState', '--value'], { encoding: 'utf8', timeout: 10000 })
      if (check.error || String(check.stdout).trim() !== 'not-found') throw new Error('An existing user service was found or its absence could not be verified. Use its managed updater.')
    }
  }
}

function recoveryContext(context) {
  if (context.uid === 0) throw new Error('Run as the user who owns the server, without sudo.')
  if (!['darwin', 'linux'].includes(context.platform)) throw new Error('Server recovery requires Linux or Apple silicon macOS.')
  for (const name of ROOT_SELECTORS) if (context.env[name]) throw new Error(`Custom installation selector ${name} is unsupported by recovery; no installer was started.`)
  const inspect = filename => {
    try { return fs.lstatSync(filename) } catch (error) { if (error.code !== 'ENOENT') throw error; return null }
  }
  const directory = (filename, { optional = false, privateDirectory = false } = {}) => {
    const info = inspect(filename)
    if (!info && optional) return null
    if (!info || !info.isDirectory() || info.isSymbolicLink() || info.uid !== context.uid ||
        (info.mode & (privateDirectory ? 0o077 : 0o022)) !== 0 || (info.mode & 0o500) !== 0o500) {
      throw new Error('Recovery requires owned, unlinked, safely permissioned default installation directories.')
    }
    return info
  }
  const readPrivate = (filename, limit) => {
    const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK)
    try {
      const info = fs.fstatSync(fd)
      if (!info.isFile() || info.uid !== context.uid || (info.mode & 0o077) !== 0 || info.nlink !== 1 || info.size > limit) {
        throw new Error('Recovery requires a bounded, private, current-user-owned regular control file.')
      }
      const bytes = fs.readFileSync(fd)
      if (bytes.length > limit) throw new Error('Recovery control file exceeded its size limit.')
      return bytes
    } finally { fs.closeSync(fd) }
  }
  directory(context.home)
  let root = context.home
  for (const component of ['.local', 'share', 'agents-server']) {
    root = path.join(root, component)
    if (!directory(root, { optional: true })) return null
  }
  const rootInfo = directory(root)
  const journalDirectory = path.join(root, '.activation-transaction')
  if (!directory(journalDirectory, { optional: true, privateDirectory: true })) return null
  const journal = JSON.parse(readPrivate(path.join(journalDirectory, 'manifest.json'), 256 * 1024))
  const origin = ['rolling-back', 'rolled-back', 'rollback-healthy'].includes(journal.phase) ? journal.rollback_from : journal.phase
  const config = path.join(context.home, '.config/agents-server')
  const state = path.join(context.home, '.agentsdock')
  const service = context.platform === 'darwin' ? path.join(context.home, 'Library/LaunchAgents/com.agentsdock.server.plist') : path.join(context.home, '.config/systemd/user/agents-server.service')
  if (journal.format !== 3 || !['prepared', 'guarded'].includes(origin) || journal.intent !== 'server-update' ||
      !/^activation-[0-9a-f]{24}$/.test(journal.transaction_id) ||
      !/^\d+\.\d+\.\d+(?:-beta\.[1-9]\d*)?$/.test(journal.release_version) ||
      journal.release_dir !== path.join(root, 'releases', journal.release_version) ||
      journal.env_path !== path.join(config, 'env') || journal.service_path !== service ||
      journal.service_state !== 'running' || journal.execution?.gateway_state !== 'absent' ||
      journal.execution?.runtime_dir !== path.join(state, 'execution') ||
      !Number.isSafeInteger(journal.execution?.api_contract) || journal.execution.api_contract < 1 ||
      journal.hub?.kind !== 'server-update' || journal.hub?.data_dir !== path.join(state, 'team-hub')) {
    throw new Error('This journal is not a supported interrupted pre-activation migration; no installer was started.')
  }
  directory(state, { privateDirectory: true })
  const identity = readPrivate(path.join(state, 'server-identity'), 1024).toString('utf8').trim()
  if (!/^[A-Za-z0-9_.:-]{8,240}$/.test(identity) || journal.hub.host_identity !== identity) {
    throw new Error('The pending migration belongs to a different server identity; no installer was started.')
  }
  // The installer validates complete native configuration, links, ownership and
  // authenticated incumbent health under its lock. These checks only select
  // the exact existing transaction; they never grant recovery authority.
  const ownerParent = path.join(root, '.activation-recovery')
  if (directory(ownerParent, { optional: true, privateDirectory: true }) && inspect(path.join(ownerParent, journal.transaction_id))) {
    throw new Error('This activation already has a recovery owner; no competing recovery was started.')
  }
  return { root, rootInfo, journalDirectory, journal, identity }
}

function validateOrigin(value) {
  const url = new URL(value)
  if (url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) throw new Error('Server URL must be an origin without credentials, path, query, or fragment.')
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) throw new Error('Use HTTPS for remote servers; HTTP is allowed only on loopback.')
  return url.origin
}

async function requestJSON(context, url, token, body) {
  // Native admin routes intentionally reject browser transport headers. Node's
  // fetch adds Sec-Fetch-Mode, so use the native HTTP client without redirects.
  const payload = body ? Buffer.from(JSON.stringify(body)) : null
  return new Promise((resolve, reject) => {
    let deadline
    const fail = error => { clearTimeout(deadline); reject(error) }
    const finish = value => { clearTimeout(deadline); resolve(value) }
    const send = context.request || (url.startsWith('https:') ? https.request : http.request)
    const request = send(url, {
      method: body ? 'POST' : 'GET',
      headers: { 'X-AgentsDock-Token': token, ...(payload ? { 'Content-Type': 'application/json', 'Content-Length': payload.length } : {}) },
    }, response => {
      if (response.statusCode < 200 || response.statusCode >= 300) {
        response.destroy()
        fail(new Error(`Server request failed (HTTP ${response.statusCode}); no installer fallback was attempted.`))
        return
      }
      const chunks = []
      let size = 0
      response.on('data', chunk => {
        size += chunk.length
        if (size > 1024 * 1024) { request.destroy(new Error('Server response exceeded its size limit.')); return }
        chunks.push(chunk)
      })
      response.on('error', fail)
      response.on('end', () => {
        try { finish(JSON.parse(Buffer.concat(chunks).toString('utf8'))) } catch { fail(new Error('Server returned invalid JSON.')) }
      })
    })
    request.on('error', fail)
    deadline = setTimeout(() => request.destroy(new Error('Server request timed out.')), 30000)
    deadline.unref()
    request.setTimeout(30000, () => request.destroy(new Error('Server request timed out.')))
    request.end(payload)
  })
}

async function run(argv, overrides = {}) {
  const context = { env: process.env, home: os.homedir(), uid: process.getuid?.(), platform: process.platform, spawn: spawnSync, packageRoot: path.resolve(__dirname, '..'), print: text => process.stdout.write(`${text}\n`), ...overrides }
  const { command, options } = parse(argv)
  if (['help', '--help', '-h'].includes(command)) { context.print(HELP); return 0 }
  const metadata = JSON.parse(readRegular(path.join(context.packageRoot, 'package.json'), 64000))
  if (command === '--version') { context.print(metadata.version); return 0 }
  const payload = path.join(context.packageRoot, 'server')
  const payloadVersion = readRegular(path.join(payload, 'VERSION'), 200).toString('utf8').trim()
  if (payloadVersion !== metadata.version) throw new Error('Package and server payload versions do not match.')
  if (command === 'recover') {
    const recovery = recoveryContext(context)
    if (!recovery) { context.print('No unfinished default server migration was found. No changes were made.'); return 0 }
    const installer = path.join(payload, 'install.sh')
    readRegular(installer, 2 * 1024 * 1024)
    const args = [installer, '--recover-only', '--recover-unarmed-only', '--non-interactive', '--execution-mode', 'split',
      '--expected-activation-id', recovery.journal.transaction_id, '--release-version', recovery.journal.release_version,
      '--expected-api-contract', String(recovery.journal.execution.api_contract), '--expected-server-identity', recovery.identity]
    const env = { HOME: context.home }
    for (const key of ['PATH', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'LANG', 'LC_ALL', 'TERM', 'TERMINFO', 'SSL_CERT_FILE', 'SSL_CERT_DIR']) {
      if (context.env[key]) env[key] = context.env[key]
    }
    const result = context.spawn('/bin/bash', args, { stdio: 'inherit', env })
    if (result.error) throw new Error('Could not launch the bundled recovery installer.')
    if (![0, 75].includes(result.status)) return Number.isInteger(result.status) ? result.status : 1
    const rootAfter = fs.lstatSync(recovery.root)
    if (!rootAfter.isDirectory() || rootAfter.isSymbolicLink() || rootAfter.dev !== recovery.rootInfo.dev || rootAfter.ino !== recovery.rootInfo.ino) {
      throw new Error('Installation ownership changed during recovery; verify the server before retrying.')
    }
    try { fs.lstatSync(recovery.journalDirectory); throw new Error('The activation journal remains; recovery is not complete.') } catch (error) { if (error.code !== 'ENOENT') throw error }
    context.print('The interrupted migration was recovered. Reconnect to AgentsDock and retry the update; the server has not been upgraded by this command.')
    return 0
  }
  if (command === 'install') {
    ensureFreshInstall(context)
    const installer = path.join(payload, 'install.sh')
    readRegular(installer, 2 * 1024 * 1024)
    const args = [installer, '--fresh-install-only', '--release-version', metadata.version]
    for (const name of ['--port', '--bind']) if (options[name]) args.push(name, options[name])
    if (options['--non-interactive']) args.push('--non-interactive')
    if (options['--dry-run']) { context.print(JSON.stringify({ command: '/bin/bash', args, executed: false })); return 0 }
    const result = context.spawn('/bin/bash', args, { stdio: 'inherit', env: context.env })
    if (result.error) throw new Error('Could not launch the bundled installer.')
    return Number.isInteger(result.status) ? result.status : 1
  }
  const origin = validateOrigin(options['--server-url'])
  for (const name of ['--server-identity', '--server-instance-id']) if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(options[name])) throw new Error('Invalid expected server identity.')
  const manifest = readRegular(options['--manifest'], 8192)
  const signature = readRegular(options['--signature'], 64)
  const publicKey = readRegular(path.join(payload, 'release-public-key.pem'), 4096)
  if (signature.length !== 64 || !crypto.verify(null, manifest, publicKey, signature)) throw new Error('Release descriptor signature is invalid.')
  const descriptor = JSON.parse(manifest)
  if (descriptor.schema !== 2 || descriptor.distribution !== 'npm' || descriptor.npm?.name !== '@agentsdock/server') throw new Error('Expected a signed npm server release descriptor.')
  const token = readRegular(options['--token-file'], 4096, { privateFile: true, uid: context.uid }).toString('utf8').trim()
  if (!token || /[\x00-\x20\x7f]/.test(token)) throw new Error('Token file must contain one nonempty server token.')
  const identity = { expected_server_identity: options['--server-identity'], expected_server_instance_id: options['--server-instance-id'] }
  const health = await requestJSON(context, `${origin}/api/health`, token)
  if (health.server_identity !== identity.expected_server_identity || health.server_instance_id !== identity.expected_server_instance_id) throw new Error('The connected server or its process instance changed. No update was requested.')
  const result = await requestJSON(context, `${origin}/api/admin/update/ensure`, token, { ...identity, manifest_base64: manifest.toString('base64'), signature_base64: signature.toString('base64') })
  context.print(JSON.stringify(result, null, 2))
  return 0
}

if (require.main === module) run(process.argv.slice(2)).then(code => { process.exitCode = code }).catch(error => { process.stderr.write(`agentsdock-server: ${error.message}\n`); process.exitCode = 1 })
module.exports = { parse, readRegular, ensureFreshInstall, recoveryContext, validateOrigin, run }
