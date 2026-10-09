'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')
const { PassThrough } = require('node:stream')
const { postinstall, skipReason } = require('./postinstall.cjs')
const { persistentPath } = require('../cli.cjs')

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsdock-postinstall-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const prefix = path.join(root, 'prefix')
  const packageRoot = path.join(prefix, 'lib/node_modules/agentsdock')
  fs.mkdirSync(packageRoot, { recursive: true })
  const output = [], calls = []
  const runtime = { version: '1.2.3-beta.4', coreRoot: path.join(root, 'core'), persistentPath,
    preflight: context => calls.push({ preflight: context.env }) }
  const context = { env: { npm_config_global: 'true', npm_config_prefix: prefix,
    npm_command: 'install', npm_lifecycle_event: 'postinstall' }, packageRoot,
    home: root, uid: 1000, platform: 'linux', load: () => runtime,
    print: value => output.push(value), launch: (...args) => {
      calls.push({ launch: args })
      const child = new EventEmitter()
      child.stdout = new PassThrough(); child.stderr = new PassThrough()
      queueMicrotask(() => {
        child.stdout.write('installer private stdout: secret-stdout\n')
        child.stderr.write('installer private stderr: secret-stderr\n')
        child.stdout.write('AGENTSDOCK_SETUP_RESULT={"access_token":"private-token",')
        child.stdout.write('"server_version":"1.2.3-beta.4","server_url":"http://127.0.0.1:7850"}\n')
        child.emit('close', 0)
      })
      return child
    } }
  return { root, prefix, packageRoot, runtime, context, output, calls }
}

test('direct global fresh installation invokes the exact runtime once and never forwards token/log output', async t => {
  const f = fixture(t)
  f.context.env.BASH_ENV = '/startup-hook'; f.context.env.AGENTSDOCK_AGENT_TOKEN = 'inherited-token'
  assert.equal(await postinstall(f.context), 0)
  assert.equal(f.calls.length, 2)
  assert.deepEqual(f.calls[0], { preflight: { HOME: f.root } })
  assert.deepEqual(f.calls[1].launch.slice(0, 2), [process.execPath,
    [path.join(f.runtime.coreRoot, 'npm/cli.cjs'), 'install', '--non-interactive']])
  assert.deepEqual(f.calls[1].launch[2], { env: { HOME: f.root }, stdio: ['ignore', 'pipe', 'pipe'] })
  assert.match(f.output.at(-1), /ready at http:\/\/127.0.0.1:7850/)
  assert.doesNotMatch(f.output.join('\n'), /private-token|secret-stdout|secret-stderr|inherited-token/)
})

test('automatic setup strips npm lifecycle/cache paths without losing stable custom provider paths', async t => {
  const f = fixture(t)
  const stable = ['/custom/provider bin', '/home/user/.local/bin', '/usr/bin', '/bin'].join(':')
  const transient = path.join(f.packageRoot, 'node_modules/.bin')
  f.context.env.PATH = `:${transient}:/cache/_npx/0123/node_modules/provider/bin::${stable}:/usr/bin`
  f.context.env.npm_config_original_path = '/untrusted/baseline'
  f.context.env.BASH_ENV = '/startup'
  f.context.env.AGENTSDOCK_AGENT_TOKEN = 'private-inherited-token'
  assert.equal(await postinstall(f.context), 0)
  assert.deepEqual(f.calls[0].preflight, { HOME: f.root, PATH: stable })
  assert.deepEqual(f.calls[1].launch[2].env, { HOME: f.root, PATH: stable })
  assert.ok(f.context.env.PATH.includes(transient))
  assert.doesNotMatch(f.output.join('\n'), /private-token|private-inherited-token|secret-|baseline|\/cache|node_modules/)
})

test('existing installation/state is a successful no-op, not an automatic upgrade', async t => {
  const f = fixture(t)
  f.runtime.preflight = () => { throw Object.assign(new Error('existing'), { code: 'AGENTSDOCK_EXISTING_INSTALLATION' }) }
  assert.equal(await postinstall(f.context), 0)
  assert.match(f.output.join('\n'), /left unchanged/)
  assert.equal(f.calls.length, 0)
})

test('setup receipts accept the native installer Tailscale/LAN preference without forwarding credentials', async t => {
  const f = fixture(t)
  for (const url of ['http://100.64.0.8:7850', 'http://192.168.1.8:7850', 'http://[::1]:7850']) {
    const launch = () => {
      const child = new EventEmitter()
      child.stdout = new PassThrough(); child.stderr = new PassThrough()
      queueMicrotask(() => {
        child.stdout.write('AGENTSDOCK_SETUP_RESULT=' + JSON.stringify({server_url: url,
          server_version: f.runtime.version, access_token: 'private-token'}) + '\n')
        child.emit('close', 0)
      })
      return child
    }
    assert.equal(await postinstall({ ...f.context, launch }), 0)
    assert.ok(f.output.at(-1).includes(url))
    assert.doesNotMatch(f.output.join('\n'), /private-token/)
  }
})

test('an existing named-only installation does not cause a second default server to appear', async t => {
  for (const relative of ['.local/share/agents-server-instances', '.config/agents-server-instances', '.agentsdock-instances']) {
    const f = fixture(t)
    const marker = path.join(f.root, relative, 'work', 'retained-state')
    fs.mkdirSync(path.dirname(marker), { recursive: true })
    fs.writeFileSync(marker, 'fixture named history')
    f.context.load = () => { throw new Error('must not load runtime for named-only state') }
    assert.equal(await postinstall(f.context), 0)
    assert.match(f.output.join('\n'), /named-server.*left unchanged/)
    assert.equal(fs.readFileSync(marker, 'utf8'), 'fixture named history')
    assert.equal(f.calls.length, 0)
  }
})

test('a concurrently detected installation remains a no-op without claiming a ready server', async t => {
  const f = fixture(t)
  f.context.launch = () => {
    const child = new EventEmitter()
    child.stdout = new PassThrough(); child.stderr = new PassThrough()
    queueMicrotask(() => {
      child.stderr.write('An existing server installation or state was found. private-token\n')
      child.emit('close', 1)
    })
    return child
  }
  assert.equal(await postinstall(f.context), 0)
  assert.match(f.output.at(-1), /no replacement was attempted/)
  assert.doesNotMatch(f.output.join('\n'), /ready|private-token/)
  assert.equal(f.calls.length, 1) // Fresh preflight only; no retry is launched.
})

test('setup spawn failure, signal termination and wrong-version receipt cannot report success', async t => {
  const f = fixture(t)
  for (const result of ['spawn-error', 'signal', 'wrong-version']) {
    f.output.length = 0
    f.context.launch = () => {
      const child = new EventEmitter()
      child.stdout = new PassThrough(); child.stderr = new PassThrough()
      queueMicrotask(() => {
        if (result === 'spawn-error') return child.emit('error', new Error('private diagnostic'))
        child.stdout.write('AGENTSDOCK_SETUP_RESULT=' + JSON.stringify({
          server_version: result === 'wrong-version' ? '1.2.3-beta.3' : f.runtime.version,
          server_url: 'http://127.0.0.1:7850', access_token: 'private-token',
        }) + '\n')
        child.emit('close', result === 'signal' ? null : 0)
      })
      return child
    }
    await assert.rejects(postinstall(f.context), error =>
      /Could not launch|did not complete/.test(error.message) && !error.message.includes('private'))
    assert.doesNotMatch(f.output.join('\n'), /ready|private-token/)
  }
})

test('uncertain preflight, root and custom selectors do not get mistaken for installed/healthy', async t => {
  const f = fixture(t)
  f.runtime.preflight = () => { throw new Error('Cannot verify service absence') }
  await assert.rejects(postinstall(f.context), /Cannot verify/)
  await assert.rejects(postinstall({ ...f.context, uid: 0 }), /without sudo/)
  await assert.rejects(postinstall({ ...f.context, env: { ...f.context.env, AGENTS_SERVER_INSTALL_DIR: '/other' } }), /Custom selector/)
  assert.equal(f.calls.length, 0)
})

test('local, CI, explicit skip, npx/rebuild and dependency installs never load a runtime', async t => {
  const f = fixture(t)
  const noLoad = () => { throw new Error('must not load') }
  for (const env of [
    { npm_config_global: 'false' }, { CI: 'true' }, { GITHUB_ACTIONS: 'true' },
    { AGENTSDOCK_SKIP_SETUP: '1' }, { npm_command: 'exec' }, { npm_command: 'rebuild' },
    { npm_lifecycle_event: 'prepare' }, { npm_config_prefix: '/other-prefix' },
  ]) assert.equal(await postinstall({ ...f.context, env: { ...f.context.env, ...env }, load: noLoad }), 0)
  const nested = path.join(f.packageRoot, 'node_modules/agentsdock')
  fs.mkdirSync(nested, { recursive: true })
  assert.equal(await postinstall({ ...f.context, packageRoot: nested, load: noLoad }), 0)
  assert.equal(f.calls.length, 0)
})

test('prefix path aliases work; npm link source checkouts remain inert', t => {
  const f = fixture(t)
  const alias = path.join(f.root, 'alias')
  fs.symlinkSync(f.prefix, alias)
  assert.equal(skipReason({ ...f.context, env: { ...f.context.env, npm_config_prefix: alias } }), null)
  fs.renameSync(f.packageRoot, path.join(f.root, 'source'))
  fs.symlinkSync(path.join(f.root, 'source'), f.packageRoot)
  assert.match(skipReason({ ...f.context, packageRoot: path.join(f.root, 'source') }), /linked/)
})

test('failed setup and invalid receipts cannot report ready or disclose raw diagnostics', async t => {
  const f = fixture(t)
  const unsafeUrls = ['http://private-token@localhost:7850', 'https://localhost:7850',
    'http://example.invalid:7850', 'http://localhost:7850/?token=private-token',
    'http://localhost:7850/#private-token', 'http://localhost:7850/private-token']
  for (const [code, receipt] of [[9, '{}'], [0, '{}'], [0, '{bad JSON'],
    ...unsafeUrls.map(server_url => [0, JSON.stringify({ server_version: f.runtime.version, server_url })])]) {
    f.output.length = 0
    const launch = () => {
      const child = new EventEmitter()
      child.stdout = new PassThrough(); child.stderr = new PassThrough()
      queueMicrotask(() => {
        child.stdout.write('x'.repeat(20000) + '\nAGENTSDOCK_SETUP_RESULT=' + receipt + '\n')
        child.stderr.write('private-token\nA trusted uv installation is not available on PATH.\n')
        child.emit('close', code)
      })
      return child
    }
    await assert.rejects(postinstall({ ...f.context, launch }), error =>
      /did not complete.*uv.*AGENTSDOCK_SKIP_SETUP/.test(error.message) && !error.message.includes('private-token'))
    assert.doesNotMatch(f.output.join('\n'), /ready|private-token|xxx/)
  }
})
