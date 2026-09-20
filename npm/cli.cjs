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
       agentsdock-server update --server-url URL --server-identity ID --server-instance-id ID
         --token-file PATH --manifest PATH --signature PATH
       agentsdock-server --version

install: fresh default user service only; existing installations must use managed updates.
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
  if (!['install', 'update'].includes(command)) throw new Error('Use install, update, or --help.')
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
  const targets = [
    '.local/share/agents-server', '.config/agents-server', '.agentsdock', '.zenithbot-agent',
    '.config/systemd/user/agents-server.service', '.config/systemd/user/zenithbot-agent.service',
    'Library/LaunchAgents/com.agentsdock.server.plist',
  ]
  for (const relative of targets) {
    try {
      fs.lstatSync(path.join(context.home, relative))
      throw new Error('An existing server installation or state was found. Use AgentsDock or agentsdock-server update; no installer was started.')
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
module.exports = { parse, readRegular, ensureFreshInstall, validateOrigin, run }
