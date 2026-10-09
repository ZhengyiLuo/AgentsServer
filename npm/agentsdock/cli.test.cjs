'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { parse, loadRuntime, run } = require('./cli.cjs')
const { persistentPath } = require('../cli.cjs')

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsdock-entry-test-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const payload = path.join(root, 'payload')
  fs.mkdirSync(payload)
  for (const name of ['instances.sh', 'install.sh']) fs.writeFileSync(path.join(payload, name), '# fixture\n')
  const calls = [], output = []
  const context = { env: {}, uid: 1000, platform: 'darwin', home: root,
    print: value => output.push(value), load: () => ({ version: '1.2.3-beta.4', payload, persistentPath,
      run: async (args, options) => { calls.push({ core: args }); options.print('agentsdock-server install'); return 7 } }),
    spawn: (...args) => { calls.push(args); return { status: 0 } } }
  return { root, payload, calls, output, context }
}

test('help works before dependency resolution; unknown and internal commands cannot spawn', async t => {
  const f = fixture(t)
  f.context.load = () => { throw new Error('must not load') }
  assert.equal(await run([], f.context), 0)
  assert.match(f.output[0], /npm install -g agentsdock/)
  for (const args of [['--help', 'extra'], ['anything'], ['servers', '_bindings', 'default'],
    ['servers', '_setup-network'], ['servers', 'install', '--manifest', '/anything'],
    ['servers', 'update', 'default'], ['token', '--instance', '../other'], ['token', '--all'],
    ['status', '--instance', 'test\nname']]) await assert.rejects(run(args, f.context))
  assert.equal(f.calls.length, 0)
})

test('setup, install, signed update and recovery retain the core CLI without new service paths', async t => {
  const f = fixture(t)
  for (const command of ['setup', 'install', 'update', 'recover']) {
    assert.equal(await run([command, '--dry-run'], f.context), 7)
    assert.deepEqual(f.calls.at(-1), { core: [command === 'setup' ? 'install' : command, '--dry-run'] })
    assert.equal(f.output.at(-1), 'agentsdock install')
  }
})

test('explicit setup/recovery and named or legacy helpers cannot persist npm execution paths', async t => {
  const f = fixture(t)
  const stable = ['/custom/provider bin', '/home/user/.local/bin', '/usr/bin', '/bin'].join(':')
  const env = { PATH: '/cache/_npx/0123/node_modules/.bin::./bin:' + stable,
    npm_config_prefix: '/custom/provider bin', npm_config_cache: '/home/user/.local/bin',
    npm_config_original_path: '/untrusted/baseline', ORIGINAL_PATH: '/other/baseline',
    BASH_ENV: '/startup', NODE_OPTIONS: '--bad', AGENTSDOCK_AGENT_TOKEN: 'private-token' }
  let delegated
  f.context.load = () => ({ payload: f.payload, persistentPath,
    run: async (args, options) => { delegated = { args, env: options.env }; return 0 } })
  for (const command of ['setup', 'install', 'recover']) {
    assert.equal(await run([command], { ...f.context, env }), 0)
    assert.deepEqual(delegated.env, { HOME: f.root, PATH: stable })
    assert.equal(delegated.args[0], command === 'setup' ? 'install' : command)
  }
  for (const args of [['new', 'work'], ['restart', 'default'], ['remove', 'work'], ['token', 'work']]) {
    assert.equal(await run(args, { ...f.context, env }), 0)
    assert.deepEqual(f.calls.at(-1)[2].env, { HOME: f.root, PATH: stable })
  }
  assert.equal(env.BASH_ENV, '/startup')
  assert.ok(env.PATH.startsWith('/cache/_npx')) // Never mutate caller/process env.
  assert.doesNotMatch(f.output.join('\n'), /private-token|\/cache|baseline/)
})

test('bare token opens the chooser; explicit token selects exactly one instance without a prompt', async t => {
  const f = fixture(t)
  assert.equal(await run(['token'], f.context), 0)
  assert.deepEqual(f.calls.at(-1).slice(0, 2), ['/bin/bash', [path.join(f.payload, 'instances.sh'), 'token']])
  for (const [args, name] of [[['default'], 'default'], [['work'], 'work'], [['--instance', 'work'], 'work']]) {
    assert.equal(await run(['token', ...args], f.context), 0)
    assert.deepEqual(f.calls.at(-1).slice(0, 2), ['/bin/bash', [path.join(f.payload, 'install.sh'),
      '--instance', name, '--show-token']])
  }
  for (const args of [['--default'], ['--instance'], ['--instance', 'work', '--bind', '0.0.0.0']]) {
    await assert.rejects(run(['token', ...args], f.context))
  }
})

test('existing setup/install refusal explains how to add an instance without retrying installation', async t => {
  const f = fixture(t)
  for (const command of ['setup', 'install']) {
    const error = Object.assign(new Error('An existing server installation or state was found.'),
      { code: 'AGENTSDOCK_EXISTING_INSTALLATION' })
    let attempts = 0
    f.context.load = () => ({ run: async () => { attempts++; throw error } })
    await assert.rejects(run([command], f.context), actual => {
      assert.equal(actual, error)
      assert.match(actual.message, /If you want to add a new server instance, run: agentsdock new/)
      assert.match(actual.message, /agentsdock new work --port 7854/)
      assert.equal(actual.code, 'AGENTSDOCK_EXISTING_INSTALLATION')
      return true
    })
    assert.equal(attempts, 1)
  }
  for (const [command, code] of [['setup', undefined], ['update', 'AGENTSDOCK_EXISTING_INSTALLATION']]) {
    const error = Object.assign(new Error('Original failure'), { code })
    f.context.load = () => ({ run: async () => { throw error } })
    await assert.rejects(run([command], f.context), actual => actual === error && actual.message === 'Original failure')
  }
  assert.equal(f.calls.length, 0)
})

test('status and public server commands delegate exact argument arrays with terminal attached', async t => {
  const f = fixture(t)
  for (const [args, expected] of [
    [['status'], ['status']], [['status', '--instance', 'work'], ['status', 'work']],
    [['servers'], ['list']], [['instances', 'list'], ['list']],
    [['servers', 'new', '--name', 'work', '--port', '7854'], ['new', '--name', 'work', '--port', '7854']],
    ...['info', 'start', 'stop', 'restart', 'remove'].map(action => [['servers', action, 'work'], [action, 'work']]),
  ]) {
    await run(args, f.context)
    assert.deepEqual(f.calls.at(-1), ['/bin/bash', [path.join(f.payload, 'instances.sh'), ...expected],
      { stdio: 'inherit', env: { HOME: f.root } }])
  }
})

test('flat commands and positional selectors produce the same native request as existing forms', async t => {
  const f = fixture(t)
  for (const action of ['list', 'info', 'new', 'start', 'stop', 'restart', 'remove']) {
    const args = action === 'list' ? [] : action === 'new' ? ['--name', 'work'] : ['work']
    assert.deepEqual(parse([action, ...args]), parse(['servers', action, ...args]))
    await run([action, ...args], f.context)
    assert.deepEqual(f.calls.at(-1)[1], [path.join(f.payload, 'instances.sh'), action, ...args])
  }
  for (const action of ['token', 'status']) {
    assert.deepEqual(parse([action, 'work']), parse([action, '--instance', 'work']))
    assert.throws(() => parse([action, 'work', '--instance', 'other']), /select one server/)
  }
  assert.deepEqual(parse(['new', 'work', '--port', '7854']), parse(['new', '--name', 'work', '--port', '7854']))
  assert.deepEqual(parse(['servers', 'new', 'work']), parse(['new', '--name', 'work']))
  assert.deepEqual(parse(['uninstall', 'work']), parse(['remove', 'work']))
  assert.deepEqual(parse(['version']), parse(['--version']))
  for (const args of [['new', 'work', '--name', 'other'], ['new', 'work', '--name=other'],
    ['new', '../other'], ['token', '../other'], ['status', '--all']]) assert.throws(() => parse(args))
})

test('flat destructive controls preserve explicit selectors and never infer a bulk target', () => {
  for (const action of ['start', 'stop', 'restart', 'remove', 'uninstall']) {
    const native = action === 'uninstall' ? 'remove' : action
    assert.deepEqual(parse([action]).args, [native]) // native helper rejects the missing target
    assert.deepEqual(parse([action, '--all', '--exclude', 'default']).args, [native, '--all', '--exclude', 'default'])
  }
  assert.deepEqual(parse(['remove', 'work', '--purge-state']).args, ['remove', 'work', '--purge-state'])
  assert.throws(() => parse(['servers', 'update', 'work']), /signed updates/)
  assert.throws(() => parse(['servers', 'install', '--manifest', 'batch.json']), /signed updates/)
})

test('local helpers reject root/custom selectors and strip shell/Python startup and credentials', async t => {
  const f = fixture(t)
  for (const env of [{ AGENTSDOCK_STATE_DIR: '/other' }, { AGENTS_SERVER_INSTANCE: 'other' },
    { XDG_CONFIG_HOME: '/other' }, { AGENTS_SERVER_INSTALL_DIR: '/other' }]) {
    await assert.rejects(run(['servers'], { ...f.context, env }), /Custom selector/)
  }
  await assert.rejects(run(['servers'], { ...f.context, uid: 0 }), /without sudo/)
  await assert.rejects(run(['servers'], { ...f.context, platform: 'win32' }), /macOS or Linux/)
  assert.equal(f.calls.length, 0)
  await run(['token'], { ...f.context, env: { HOME: '/wrong', PATH: '/usr/bin', TERM: 'xterm',
    BASH_ENV: '/bad', ENV: '/bad', PYTHONPATH: '/bad', AGENTSDOCK_AGENT_TOKEN: 'private', NODE_OPTIONS: '--bad' } })
  assert.deepEqual(f.calls[0][2].env, { HOME: f.root, PATH: '/usr/bin', TERM: 'xterm' })
})

test('helper failures remain failures; symlinked helper cannot execute', async t => {
  const f = fixture(t)
  assert.equal(await run(['servers'], { ...f.context, spawn: () => ({ status: 23 }) }), 23)
  assert.equal(await run(['servers'], { ...f.context, spawn: () => ({ status: null, signal: 'SIGTERM' }) }), 1)
  await assert.rejects(run(['servers'], { ...f.context, spawn: () => ({ error: new Error('no spawn') }) }), /Could not start/)
  fs.renameSync(path.join(f.payload, 'instances.sh'), path.join(f.payload, 'other.sh'))
  fs.symlinkSync('other.sh', path.join(f.payload, 'instances.sh'))
  await assert.rejects(run(['servers'], f.context), /regular file/)
  assert.equal(f.calls.length, 0)
})

test('installed CLI refuses mismatched dependency or runtime payload before loading code', t => {
  const f = fixture(t)
  const own = { name: 'agentsdock', version: '1.2.3-beta.4', dependencies: { '@agentsdock/server': '1.2.3-beta.4' } }
  const core = { name: '@agentsdock/server', version: own.version }
  fs.mkdirSync(path.join(f.root, 'core/server'), { recursive: true })
  fs.mkdirSync(path.join(f.root, 'core/npm'))
  fs.writeFileSync(path.join(f.root, 'package.json'), JSON.stringify(own))
  fs.writeFileSync(path.join(f.root, 'core/package.json'), JSON.stringify(core))
  fs.writeFileSync(path.join(f.root, 'core/server/VERSION'), own.version)
  fs.writeFileSync(path.join(f.root, 'core/npm/cli.cjs'), 'module.exports = {run: () => 17}\n')
  const resolve = () => path.join(f.root, 'core/package.json')
  assert.equal(loadRuntime(f.root, resolve).version, own.version)
  for (const version of ['1.2.4', 'latest', '^1.2.3-beta.4']) {
    fs.writeFileSync(path.join(f.root, 'package.json'), JSON.stringify({ ...own, dependencies: { '@agentsdock/server': version } }))
    assert.throws(() => loadRuntime(f.root, resolve), /versions differ/)
  }
  fs.writeFileSync(path.join(f.root, 'package.json'), JSON.stringify(own))
  fs.writeFileSync(path.join(f.root, 'core/package.json'), JSON.stringify({ ...core, version: '1.2.4' }))
  assert.throws(() => loadRuntime(f.root, resolve), /versions differ/)
  fs.writeFileSync(path.join(f.root, 'core/package.json'), JSON.stringify(core))
  fs.writeFileSync(path.join(f.root, 'core/server/VERSION'), '1.2.4')
  assert.throws(() => loadRuntime(f.root, resolve), /payload versions differ/)
})
