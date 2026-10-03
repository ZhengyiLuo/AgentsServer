'use strict'

const path = require('node:path')
const fs = require('node:fs')
const os = require('node:os')
const { isIP } = require('node:net')
const { spawn, spawnSync } = require('node:child_process')
const { loadRuntime, localEnvironment } = require('./cli.cjs')

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

async function postinstall(overrides = {}) {
  const context = { env: process.env, home: os.homedir(), uid: process.getuid?.(),
    platform: process.platform, packageRoot: __dirname, spawn: spawnSync,
    launch: spawn, load: loadRuntime, print: text => process.stdout.write(`agentsdock: ${text}\n`), ...overrides }
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
  context.print('Setting up your first AgentsDock server. This may take a few minutes.')
  let receipt, missingUv = false, racedInstallation = false
  // Do not forward installer output into npm's captured logs: the setup JSON
  // contains an access token. Retain only a validated non-secret summary and
  // fixed diagnostics, never raw output (even when the installer fails).
  const stdout = lines(line => {
    if (!line.startsWith('AGENTSDOCK_SETUP_RESULT=')) return
    try {
      const result = JSON.parse(line.slice('AGENTSDOCK_SETUP_RESULT='.length))
      const url = new URL(result.server_url)
      const host = url.hostname.replace(/^\[|\]$/g, '')
      if (result.server_version === runtime.version && url.protocol === 'http:' &&
          (host === 'localhost' || isIP(host)) &&
          !url.username && !url.password && !url.search && !url.hash && url.pathname === '/') {
        receipt = { url: url.origin, version: runtime.version }
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
    const detail = missingUv ? ' A trusted uv installation is required on PATH.' : ''
    throw new Error('Automatic setup did not complete successfully.' + detail +
      ' For interactive diagnostics, install the CLI with AGENTSDOCK_SKIP_SETUP=1 npm install -g agentsdock, then run agentsdock setup. Existing state is never deleted automatically.')
  }
  context.print(`Server ${receipt.version} is ready at ${receipt.url}. Open AgentsDock to connect. Use agentsdock token to view/copy its private token.`)
  return 0
}

if (require.main === module) postinstall().then(code => { process.exitCode = code }).catch(error => {
  process.stderr.write(`agentsdock: ${error.message}\n`)
  process.exitCode = 1
})
module.exports = { skipReason, lines, postinstall }
