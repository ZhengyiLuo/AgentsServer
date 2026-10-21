'use strict'

const path = require('node:path')
const fs = require('node:fs')
const os = require('node:os')
const { isIP } = require('node:net')
const { isatty } = require('node:tty')
const { spawn, spawnSync } = require('node:child_process')
const { loadRuntime, localEnvironment } = require('./cli.cjs')
const { showSetupQr } = require('./pair.cjs')

function skipReason(context) {
  const env = context.env
  if (env.AGENTSDOCK_SKIP_SETUP === '1') return 'Automatic setup was explicitly skipped.'
  if ((env.CI && !['0', 'false'].includes(env.CI.toLowerCase())) || env.GITHUB_ACTIONS === 'true') {
    return 'CI installation: automatic service setup skipped.'
  }
  if (env.npm_lifecycle_event !== 'postinstall' || env.npm_command !== 'install') {
    return 'Automatic setup only runs during npm install.'
  }
  if (env.npm_config_global !== 'true') return 'Local installation: run npx agentsdock setup when you want to create a server.'
  // Never start a service merely because another global tool depends on us,
  // or because a source checkout was linked. npm's direct global package must
  // occupy its canonical prefix, not a nested dependency or npx cache.
  const prefix = env.npm_config_prefix
  if (!prefix || !path.isAbsolute(prefix)) return 'Could not establish the direct global installation; setup skipped.'
  const expected = path.join(path.resolve(prefix), 'lib/node_modules/agentsdock')
  let direct = false
  try {
    direct = fs.lstatSync(expected).isDirectory() &&
      fs.realpathSync(context.packageRoot) === fs.realpathSync(expected)
  } catch { /* A different npm layout must never create an unexpected service. */ }
  if (!direct) {
    return 'Dependency or linked installation: automatic service setup skipped.'
  }
  return null
}

function lines(accept) {
  let pending = '', overflow = false
  return chunk => {
    for (const character of chunk.toString()) {
      if (character === '\n') {
        if (!overflow) accept(pending.replace(/\r$/, ''))
        pending = ''; overflow = false
      } else if (!overflow && pending.length < 16384) pending += character
      else { pending = ''; overflow = true }
    }
  }
}

function terminalPrint(text, io = { ...fs, isatty }, pause = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms)) {
  // npm captures lifecycle stdout/stderr, including --foreground-scripts in
  // its debug logs. Only the controlling terminal may receive credentials.
  // No terminal (CI/headless installs): fail closed, never fall back to a log.
  let fd
  try {
    // Use normal blocking tty writes. Nonblocking writes can split a large QR
    // mid-character and let npm's progress indicator overwrite its contents.
    fd = io.openSync('/dev/tty', fs.constants.O_WRONLY | fs.constants.O_NOCTTY)
    if (!io.isatty(fd)) return false
    const bytes = Buffer.from(text)
    let offset = 0, retries = 0
    while (offset < bytes.length) {
      try {
        const written = io.writeSync(fd, bytes, offset, bytes.length - offset)
        if (!written) return false
        offset += written
      } catch (error) {
        // Preserve the exact byte offset after interruptions; cap retries when
        // a terminal rejects a write instead of forwarding credentials to logs.
        if (!['EAGAIN', 'EWOULDBLOCK', 'EINTR'].includes(error.code) || retries++ >= 100) return false
        pause(10)
      }
    }
    return true
  } catch { return false }
  finally { if (fd !== undefined) { try { io.closeSync(fd) } catch { /* best effort */ } } }
}

async function postinstall(overrides = {}) {
  const context = { env: process.env, home: os.homedir(), uid: process.getuid?.(),
    platform: process.platform, packageRoot: __dirname, spawn: spawnSync,
    launch: spawn, load: loadRuntime, terminalPrint, showSetupQr,
    print: text => process.stdout.write(`agentsdock: ${text}\n`), ...overrides }
  const skipped = skipReason(context)
  if (skipped) { context.print(skipped); return 0 }
  const environment = localEnvironment(context)
  for (const relative of ['.local/share/agents-server-instances', '.config/agents-server-instances', '.agentsdock-instances']) {
    try {
      fs.lstatSync(path.join(context.home, relative))
      context.print('Existing named-server installation or state found; left unchanged. Use agentsdock list to inspect it.')
      return 0
    } catch (error) { if (error.code !== 'ENOENT') throw error }
  }
  const runtime = context.load()
  try {
    runtime.preflight({ ...context, env: environment })
  } catch (error) {
    if (error.code !== 'AGENTSDOCK_EXISTING_INSTALLATION') throw error
    context.print('Existing server installation or state found; left unchanged. Use agentsdock list to inspect it.')
    return 0
  }
  const starting = 'Setting up your first AgentsDock server. This may take a few minutes.'
  if (!context.terminalPrint(`agentsdock: ${starting}\n`)) context.print(starting)
  let receipt, missingUv = false, racedInstallation = false
  // Do not forward installer output into npm's captured logs: the setup JSON
  // contains an access token. Only a validated success receipt may go directly
  // to the terminal; stdout/stderr receive a non-secret summary. Never forward
  // raw output (even when the installer fails).
  const stdout = lines(line => {
    if (!line.startsWith('AGENTSDOCK_SETUP_RESULT=')) return
    try {
      const result = JSON.parse(line.slice('AGENTSDOCK_SETUP_RESULT='.length))
      const url = new URL(result.server_url)
      const host = url.hostname.replace(/^\[|\]$/g, '')
      if (result.server_version === runtime.version && url.protocol === 'http:' &&
          (host === 'localhost' || isIP(host)) &&
          !url.username && !url.password && !url.search && !url.hash && url.pathname === '/') {
        const token = typeof result.access_token === 'string' && /^[\x21-\x7e]{1,4096}$/.test(result.access_token)
          ? result.access_token : ''
        receipt = { url: url.origin, version: runtime.version, token }
      }
    } catch { /* A malformed/missing success receipt must not become success. */ }
  })
  const stderr = lines(line => {
    if (/trusted uv|uv.*not.*(?:available|found)/i.test(line)) missingUv = true
    if (/An existing server installation or state was found|An AgentsServer service already exists|An existing user service was found/.test(line)) racedInstallation = true
  })
  const status = await new Promise((resolve, reject) => {
    const child = context.launch(process.execPath,
      [path.join(runtime.coreRoot, 'npm/cli.cjs'), 'install', '--non-interactive'],
      { env: environment, stdio: ['ignore', 'pipe', 'pipe'] })
    child.stdout.on('data', stdout)
    child.stderr.on('data', stderr)
    child.once('error', () => reject(new Error('Could not launch automatic server setup.')))
    child.once('close', code => resolve(code))
  })
  if (status !== 0 && racedInstallation) {
    context.print('Another installation or existing state was detected; no replacement was attempted. Run agentsdock list.')
    return 0
  }
  if (status !== 0 || !receipt) {
    if (missingUv) {
      // Fixed guidance only: never echo the installer's potentially private
      // diagnostics. Preserve the selected version, including a beta.
      const install = `npm install -g agentsdock@${runtime.version}`
      throw new Error([
        'Automatic setup did not complete: uv is missing from PATH or cannot run.',
        'AgentsDock Server needs uv to prepare its Python environment.',
        ...(context.platform === 'darwin'
          ? ['On macOS with Homebrew, install uv:', '  brew install uv'] : []),
        'Installation guide: https://docs.astral.sh/uv/getting-started/installation/',
        'Then verify and retry the same AgentsDock version:',
        '  uv --version',
        `  ${install} --foreground-scripts`,
        'If uv works but setup still fails, run these for interactive diagnostics:',
        `  AGENTSDOCK_SKIP_SETUP=1 ${install}`,
        '  agentsdock setup',
        'Existing state is never deleted automatically.',
      ].join('\n'))
    }
    throw new Error('Automatic setup did not complete successfully.' +
      ' For interactive diagnostics, install the CLI with AGENTSDOCK_SKIP_SETUP=1 npm install -g agentsdock, then run agentsdock setup. Existing state is never deleted automatically.')
  }
  const summary = `Server ${receipt.version} is ready at ${receipt.url}. Open AgentsDock to connect. Use agentsdock token to view/copy its private token.`
  const shown = context.terminalPrint([
    '', 'Your new service is up!', '', 'Server (default):',
    `  Version: ${receipt.version}`, `  URL:     ${receipt.url}`, '',
    ...(receipt.token ? ['Access token (default):', receipt.token, '', 'Keep this token private.'] : []),
    'Open AgentsDock and add this server using its URL and token.',
    'To view or copy the token again: agentsdock token default', '',
  ].join('\n'))
  if (!shown) context.print(summary)
  else if (receipt.token) await context.showSetupQr('default', runtime, {
    ...context, print: text => { context.terminalPrint(`${text}\n`) },
  }, environment)
  return 0
}

if (require.main === module) postinstall().then(code => { process.exitCode = code }).catch(error => {
  process.stderr.write(`agentsdock: ${error.message}\n`)
  process.exitCode = 1
})
module.exports = { skipReason, lines, terminalPrint, postinstall }
