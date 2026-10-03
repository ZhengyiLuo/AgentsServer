#!/usr/bin/env node
'use strict'

const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const { spawnSync } = require('node:child_process')

const HELP = `Usage: agentsdock setup [--port PORT] [--bind IP] [--non-interactive] [--dry-run]
       agentsdock install [same options as setup]
       agentsdock list
       agentsdock info NAME
       agentsdock new [NAME] [--port PORT] [--bind IP]
       agentsdock start|stop|restart|remove NAME
       agentsdock token [NAME]
       agentsdock status [NAME]
       agentsdock update --server-url URL --server-identity ID --server-instance-id ID
         --token-file PATH --manifest PATH --signature PATH
       agentsdock recover
       agentsdock --version

setup/install creates a fresh default server. Use new for another instance.
remove (also uninstall) asks for confirmation and preserves history by default.
token lists existing servers; use Up/Down and Enter to choose, or token NAME for direct access. Keep tokens private.
status shows each instance's status, addresses, installed version and port; add NAME for one instance.
update uses the signed managed updater; npm installation alone never upgrades a running server.
start/stop/restart/remove also accept --all [--exclude NAME]; an omitted target never selects all.
new --name NAME, token/status --instance NAME, and servers/instances ACTION remain supported.
Install this command globally with npm install -g agentsdock, or use npx agentsdock.
The first global installation automatically sets up the server; existing installations are left unchanged.
`
const ROOT_SELECTORS = ['AGENTS_SERVER_INSTALL_DIR', 'AGENTS_SERVER_CONFIG_DIR',
  'AGENTS_SERVER_STATE_DIR', 'AGENTSDOCK_STATE_DIR', 'ZENITHBOT_AGENT_DIR',
  'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'AGENTS_SERVER_INSTANCE']
const ENV_KEYS = ['PATH', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'LANG', 'LC_ALL',
  'TERM', 'TERMINFO', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS']

function selector(args, fallback) {
  if (!args.length) return fallback
  const name = args.length === 1 ? args[0] : args.length === 2 && args[0] === '--instance' ? args[1] : ''
  if (!/^[a-z][a-z0-9-]{0,31}$/.test(name)) throw new Error('Use NAME or --instance NAME to select one server.')
  return name
}

const INSTANCE_ACTIONS = ['list', 'info', 'new', 'start', 'stop', 'restart', 'remove']
function instanceRequest(action, args) {
  if (!INSTANCE_ACTIONS.includes(action)) {
    throw new Error('Use list, info, new, start, stop, restart, or remove. Use agentsdock update for signed updates.')
  }
  if (action === 'new' && args[0] && !args[0].startsWith('-')) {
    const name = selector([args[0]])
    if (args.slice(1).some(value => value === '--name' || value.startsWith('--name='))) {
      throw new Error('Select the new instance name once: NAME or --name NAME.')
    }
    args = ['--name', name, ...args.slice(1)]
  }
  return { kind: 'local', script: 'instances.sh', args: [action, ...args] }
}

function parse(argv) {
  const [command = '--help', ...args] = argv
  if (argv.some(value => /[\x00-\x1f\x7f]/.test(value))) throw new Error('Invalid control character in arguments.')
  if (['help', '--help', '-h', '--version', 'version'].includes(command)) {
    if (args.length) throw new Error('Unexpected arguments.')
    return { kind: ['--version', 'version'].includes(command) ? 'version' : 'help' }
  }
  if (['setup', 'install', 'update', 'recover'].includes(command)) {
    return { kind: 'core', args: [command === 'setup' ? 'install' : command, ...args] }
  }
  if (command === 'token') {
    const name = selector(args)
    return name
      ? { kind: 'local', script: 'install.sh', args: ['--instance', name, '--show-token'] }
      : { kind: 'local', script: 'instances.sh', args: ['token'] }
  }
  if (command === 'status') {
    const name = selector(args)
    return { kind: 'local', script: 'instances.sh', args: name ? ['status', name] : ['status'] }
  }
  if (INSTANCE_ACTIONS.includes(command) || command === 'uninstall') {
    return instanceRequest(command === 'uninstall' ? 'remove' : command, args)
  }
  if (['servers', 'instances'].includes(command)) {
    const [action = 'list', ...rest] = args
    // Internal binding helpers, manifests and the checkout-only update bypass
    // are not public npm commands. Python retains all instance/path validation.
    return instanceRequest(action, rest)
  }
  throw new Error('Unknown command. Run agentsdock --help.')
}

function readJson(filename) {
  const info = fs.lstatSync(filename)
  if (!info.isFile() || info.size > 64000) throw new Error('Invalid CLI package metadata.')
  return JSON.parse(fs.readFileSync(filename, 'utf8'))
}

function loadRuntime(packageRoot = __dirname, resolve = require.resolve) {
  const own = readJson(path.join(packageRoot, 'package.json'))
  const coreRoot = path.dirname(resolve('@agentsdock/server/package.json'))
  const core = readJson(path.join(coreRoot, 'package.json'))
  if (own.name !== 'agentsdock' || core.name !== '@agentsdock/server' ||
      own.dependencies?.['@agentsdock/server'] !== own.version || core.version !== own.version) {
    throw new Error('CLI and server package versions differ. Reinstall the matching agentsdock package.')
  }
  const payload = path.join(coreRoot, 'server')
  const versionFile = path.join(payload, 'VERSION')
  const info = fs.lstatSync(versionFile)
  if (!info.isFile() || info.size > 200 || fs.readFileSync(versionFile, 'utf8').trim() !== own.version) {
    throw new Error('CLI and server payload versions differ. Reinstall the matching agentsdock package.')
  }
  const coreCli = require(path.join(coreRoot, 'npm/cli.cjs'))
  return { version: own.version, payload, coreRoot, run: coreCli.run, preflight: coreCli.ensureFreshInstall }
}

function localEnvironment(context) {
  if (context.uid === 0) throw new Error('Run as the server owner, without sudo.')
  if (!['darwin', 'linux'].includes(context.platform)) throw new Error('Server management requires macOS or Linux.')
  for (const name of ROOT_SELECTORS) {
    if (context.env[name]) throw new Error(`Custom selector ${name} is unsupported; use --instance to select a managed server.`)
  }
  const env = { HOME: context.home }
  for (const key of ENV_KEYS) if (context.env[key]) env[key] = context.env[key]
  return env
}

async function run(argv, overrides = {}) {
  const context = { print: text => process.stdout.write(`${text}\n`), env: process.env,
    home: os.homedir(), uid: process.getuid?.(), platform: process.platform,
    spawn: spawnSync, load: loadRuntime, ...overrides }
  const request = parse(argv)
  if (request.kind === 'help') { context.print(HELP); return 0 }
  const runtime = context.load()
  if (request.kind === 'version') { context.print(runtime.version); return 0 }
  if (request.kind === 'core') {
    try {
      return await runtime.run(request.args, { print: text => context.print(text.replaceAll('agentsdock-server', 'agentsdock')) })
    } catch (error) {
      if (request.args[0] === 'install' && error.code === 'AGENTSDOCK_EXISTING_INSTALLATION') {
        error.message += '\nIf you want to add a new server instance, run: agentsdock new' +
          '\nOr choose its name and port: agentsdock new work --port 7854'
      }
      throw error
    }
  }
  const env = localEnvironment(context)
  const script = path.join(runtime.payload, request.script)
  if (!fs.lstatSync(script).isFile()) throw new Error('The installed server helper is not a regular file.')
  // Preserve the terminal for confirmation/clipboard prompts. Never interpolate
  // a command string, inherit startup hooks, or start a detached helper.
  const result = context.spawn('/bin/bash', [script, ...request.args], { stdio: 'inherit', env })
  if (result.error) throw new Error('Could not start the installed server helper.')
  return Number.isInteger(result.status) ? result.status : 1
}

if (require.main === module) run(process.argv.slice(2)).then(code => { process.exitCode = code }).catch(error => {
  process.stderr.write(`agentsdock: ${error.message.replaceAll('agentsdock-server', 'agentsdock')}\n`)
  process.exitCode = 1
})
module.exports = { parse, loadRuntime, localEnvironment, run }
