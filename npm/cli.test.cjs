'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')
const crypto = require('node:crypto')
const { spawnSync } = require('node:child_process')
const { parse, run, validateOrigin } = require('./cli.cjs')

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsdock-npm-cli-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const packageRoot = path.join(root, 'package')
  const home = path.join(root, 'home')
  fs.mkdirSync(path.join(packageRoot, 'server'), { recursive: true })
  fs.mkdirSync(home)
  fs.writeFileSync(path.join(packageRoot, 'package.json'), JSON.stringify({ version: '1.2.3-beta.4' }))
  fs.writeFileSync(path.join(packageRoot, 'server/VERSION'), '1.2.3-beta.4\n')
  fs.writeFileSync(path.join(packageRoot, 'server/install.sh'), '#!/bin/bash\nexit 73\n')
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519')
  fs.writeFileSync(path.join(packageRoot, 'server/release-public-key.pem'), publicKey.export({ type: 'spki', format: 'pem' }))
  const manifest = path.join(root, 'manifest.json')
  const signature = path.join(root, 'manifest.sig')
  const tokenFile = path.join(root, 'token')
  fs.writeFileSync(manifest, JSON.stringify({ schema: 2, distribution: 'npm', npm: { name: '@agentsdock/server' } }))
  fs.writeFileSync(signature, crypto.sign(null, fs.readFileSync(manifest), privateKey))
  fs.writeFileSync(tokenFile, 'fixture-token\n', { mode: 0o600 })
  const output = []
  const calls = []
  const context = { packageRoot, home, env: {}, platform: 'linux', uid: process.getuid(), print: text => output.push(text), spawn: (...args) => { calls.push(args); return { status: 0, stdout: 'not-found\n' } } }
  return { root, packageRoot, home, manifest, signature, tokenFile, context, output, calls }
}

function updateArgs(f, origin) {
  return ['update', '--server-url', origin, '--server-identity', 'server-one', '--server-instance-id', 'process-one', '--token-file', f.tokenFile, '--manifest', f.manifest, '--signature', f.signature]
}

function pendingRecovery(f) {
  const install = path.join(f.home, '.local/share/agents-server')
  const journalDirectory = path.join(install, '.activation-transaction')
  const state = path.join(f.home, '.agentsdock')
  fs.mkdirSync(journalDirectory, { recursive: true, mode: 0o700 })
  fs.chmodSync(install, 0o755) // Existing installations triggering the incident.
  fs.mkdirSync(state, { mode: 0o700 })
  fs.writeFileSync(path.join(state, 'server-identity'), 'server-one\n', { mode: 0o600 })
  const journal = { format: 3, transaction_id: `activation-${'a'.repeat(24)}`, release_version: '1.0.4',
    release_dir: path.join(install, 'releases/1.0.4'), phase: 'rolling-back', rollback_from: 'prepared', intent: 'server-update',
    env_path: path.join(f.home, '.config/agents-server/env'), service_path: path.join(f.home, '.config/systemd/user/agents-server.service'),
    service_state: 'running', execution: { api_contract: 28, gateway_state: 'absent', runtime_dir: path.join(state, 'execution') },
    hub: { kind: 'server-update', host_identity: 'server-one', data_dir: path.join(state, 'team-hub') } }
  const manifest = path.join(journalDirectory, 'manifest.json')
  const write = () => fs.writeFileSync(manifest, JSON.stringify(journal), { mode: 0o600 })
  write()
  return { install, journalDirectory, manifest, state, journal, write }
}

test('recover accepts no selectors, force flags or installer arguments', () => {
  assert.deepEqual(parse(['recover']), { command: 'recover', options: {} })
  for (const args of [['--force'], ['--release-version', '1.0.4'], ['--port', '7850'], ['--server-url', 'https://remote.example'], ['--dry-run']]) assert.throws(() => parse(['recover', ...args]), /Unsupported/)
})

test('recover with no installation or no journal never creates files or starts an installer', async t => {
  const f = fixture(t)
  assert.equal(await run(['recover'], f.context), 0)
  assert.deepEqual(fs.readdirSync(f.home), [])
  fs.mkdirSync(path.join(f.home, '.local/share/agents-server'), { recursive: true })
  const root = path.join(f.home, '.local/share/agents-server'), before = fs.statSync(root)
  assert.equal(await run(['recover'], f.context), 0)
  assert.equal(fs.statSync(root).ino, before.ino)
  assert.deepEqual(fs.readdirSync(root), [])
  assert.equal(f.calls.length, 0)
  assert.match(f.output.join('\n'), /No changes were made/)
})

test('recover delegates exact old journal pins to newer bundled installer and treats verified retirement as success', async t => {
  const f = fixture(t), r = pendingRecovery(f)
  const env = { HOME: '/wrong/home', PATH: '/usr/bin:/bin', BASH_ENV: '/malicious', PYTHONPATH: '/unrelated', AGENTSDOCK_AGENT_TOKEN: 'must-not-leak' }
  let invocation
  const result = await run(['recover'], { ...f.context, env, spawn: (...args) => {
    invocation = args
    fs.rmSync(r.journalDirectory, { recursive: true })
    return { status: 75 }
  } })
  assert.equal(result, 0)
  assert.deepEqual(invocation[1], [path.join(f.packageRoot, 'server/install.sh'), '--recover-only', '--recover-unarmed-only', '--non-interactive', '--execution-mode', 'split', '--expected-activation-id', r.journal.transaction_id, '--release-version', '1.0.4', '--expected-api-contract', '28', '--expected-server-identity', 'server-one'])
  assert.deepEqual(invocation[2].env, { HOME: f.home, PATH: '/usr/bin:/bin' })
  assert.match(f.output.at(-1), /retry the update; the server has not been upgraded/)
})

test('recovery rejects unsafe directories and control files without spawning or changing them', async t => {
  const changes = [
    (f, r) => { fs.renameSync(r.install, r.install + '-other'); fs.symlinkSync(r.install + '-other', r.install) },
    (f, r) => { fs.renameSync(path.join(f.home, '.local'), path.join(f.home, 'other')); fs.symlinkSync(path.join(f.home, 'other'), path.join(f.home, '.local')) },
    (f, r) => fs.chmodSync(r.install, 0o777),
    (f, r) => fs.chmodSync(r.journalDirectory, 0o755),
    (f, r) => { fs.renameSync(r.journalDirectory, r.journalDirectory + '-other'); fs.symlinkSync(r.journalDirectory + '-other', r.journalDirectory) },
    (f, r) => { fs.renameSync(r.manifest, r.manifest + '-other'); fs.symlinkSync(r.manifest + '-other', r.manifest) },
    (f, r) => fs.linkSync(r.manifest, r.manifest + '-other'),
    (f, r) => fs.chmodSync(r.manifest, 0o644),
    (f, r) => fs.writeFileSync(r.manifest, Buffer.alloc(256 * 1024 + 1)),
    (f, r) => fs.writeFileSync(r.manifest, '{bad JSON'),
    (f, r) => { fs.renameSync(r.manifest, r.manifest + '-other'); fs.mkdirSync(r.manifest) },
    (f, r) => { const identity = path.join(r.state, 'server-identity'); fs.renameSync(identity, identity + '-other'); fs.symlinkSync(identity + '-other', identity) },
    (f, r) => fs.chmodSync(path.join(r.state, 'server-identity'), 0o644),
    (f) => { f.context.uid++ },
  ]
  for (const change of changes) {
    const f = fixture(t), r = pendingRecovery(f)
    change(f, r)
    const before = fs.lstatSync(r.install)
    await assert.rejects(run(['recover'], f.context))
    assert.equal(f.calls.length, 0)
    assert.equal(fs.lstatSync(r.install).ino, before.ino)
  }
})

test('recovery rejects different identity, layout, later phase and existing owner without installer', async t => {
  const changes = [
    r => { r.journal.hub.host_identity = 'different-server' },
    r => { r.journal.phase = 'stopping'; r.journal.rollback_from = null },
    r => { r.journal.rollback_from = 'candidate-starting' },
    r => { r.journal.service_state = 'stopped' },
    r => { r.journal.env_path = '/other/env' },
    r => { r.journal.service_path = '/other/service' },
    r => { r.journal.execution.runtime_dir = '/other/execution' },
    r => { r.journal.execution.gateway_state = 'running' },
    r => { r.journal.execution.api_contract = 0 },
    r => { r.journal.hub.data_dir = '/other/team-hub' },
    r => { r.journal.transaction_id = '../../other' },
    r => { r.journal.release_version = '../other' },
    r => { r.journal.format = 2 },
    r => fs.mkdirSync(path.join(r.install, '.activation-recovery', r.journal.transaction_id), { recursive: true, mode: 0o700 }),
    r => { const parent = path.join(r.install, '.activation-recovery'); fs.mkdirSync(parent, { mode: 0o700 }); fs.symlinkSync('/nonexistent', path.join(parent, r.journal.transaction_id)) },
  ]
  for (const change of changes) {
    const f = fixture(t), r = pendingRecovery(f)
    change(r); r.write()
    const before = fs.readFileSync(r.manifest)
    await assert.rejects(run(['recover'], f.context))
    assert.equal(f.calls.length, 0)
    assert.deepEqual(fs.readFileSync(r.manifest), before)
  }
})

test('recovery does not mask failure, claim retirement with a journal, or relax payload version checks', async t => {
  const f = fixture(t), r = pendingRecovery(f)
  assert.equal(await run(['recover'], { ...f.context, spawn: () => ({ status: 9 }) }), 9)
  await assert.rejects(run(['recover'], { ...f.context, spawn: () => ({ status: 75 }) }), /journal remains/)
  await assert.rejects(run(['recover'], { ...f.context, env: { AGENTS_SERVER_INSTALL_DIR: r.install } }), /Custom installation selector/)
  await assert.rejects(run(['recover'], { ...f.context, uid: 0 }), /without sudo/)
  fs.writeFileSync(path.join(f.packageRoot, 'server/VERSION'), '1.0.4\n')
  await assert.rejects(run(['recover'], f.context), /payload versions do not match/)
  assert.equal(f.calls.length, 0)
})

test('rejects arbitrary installer flags and invalid values before delegation', () => {
  for (const args of [['install', '--release-version', '9.9.9'], ['install', '--instance', 'other'], ['install', '--port', '0'], ['install', '--port', '65536'], ['install', '--bind', 'anything;command'], ['install', '--port', '7850', '--port', '7851']]) assert.throws(() => parse(args))
  assert.throws(() => parse(['update']), /Required/)
})

test('fresh install only delegates validated arguments to the bundled installer', async t => {
  const f = fixture(t)
  await run(['install', '--port', '7851', '--bind', '127.0.0.1', '--non-interactive'], { ...f.context, uid: 501 })
  const install = f.calls.at(-1)
  assert.equal(install[0], '/bin/bash')
  assert.deepEqual(install[1], [path.join(f.packageRoot, 'server/install.sh'), '--fresh-install-only', '--release-version', '1.2.3-beta.4', '--port', '7851', '--bind', '127.0.0.1', '--non-interactive'])
  assert.equal(install[2].stdio, 'inherit')
})

test('fresh guard refuses an existing install or state without spawning anything', async t => {
  const f = fixture(t)
  fs.mkdirSync(path.join(f.home, '.agentsdock'))
  fs.writeFileSync(path.join(f.home, '.agentsdock/server-identity'), 'existing-server')
  await assert.rejects(run(['install'], f.context), /existing server installation or state/)
  assert.equal(f.calls.length, 0)
})

test('fresh install retries real bootstrap and preactivation subprocess failures without deleting empty scaffolding', async t => {
  const f = fixture(t)
  const attempt = path.join(f.root, 'attempt')
  const preactivationAttempt = path.join(f.root, 'preactivation-attempt')
  const completed = path.join(f.root, 'completed')
  fs.writeFileSync(path.join(f.packageRoot, 'server/install.sh'), `#!/bin/bash
set -eu
mkdir -p "$QA_FRESH_HOME/.local/share/agents-server/releases"
if [[ ! -e "$QA_INSTALL_ATTEMPT" ]]; then
  touch "$QA_INSTALL_ATTEMPT"
  exit 73
fi
mkdir -p "$QA_FRESH_HOME/.config/agents-server" "$QA_FRESH_HOME/.agentsdock/admin"
chmod 700 "$QA_FRESH_HOME/.config/agents-server" "$QA_FRESH_HOME/.agentsdock" "$QA_FRESH_HOME/.agentsdock/admin"
if [[ ! -e "$QA_PREACTIVATION_ATTEMPT" ]]; then
  touch "$QA_PREACTIVATION_ATTEMPT"
  exit 74
fi
touch "$QA_INSTALL_COMPLETED"
`)
  let installs = 0
  const context = {
    ...f.context,
    env: { ...process.env, QA_FRESH_HOME: f.home, QA_INSTALL_ATTEMPT: attempt, QA_PREACTIVATION_ATTEMPT: preactivationAttempt, QA_INSTALL_COMPLETED: completed },
    spawn: (command, args, options) => {
      if (command !== '/bin/bash') return { status: 0, stdout: 'not-found\n' }
      installs++
      return spawnSync(command, args, options)
    },
  }
  for (const key of ['AGENTS_SERVER_INSTALL_DIR', 'AGENTS_SERVER_CONFIG_DIR', 'AGENTS_SERVER_STATE_DIR', 'AGENTSDOCK_STATE_DIR', 'ZENITHBOT_AGENT_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME']) delete context.env[key]
  assert.equal(await run(['install', '--non-interactive'], context), 73)
  const installRoot = path.join(f.home, '.local/share/agents-server')
  assert.deepEqual(fs.readdirSync(installRoot), ['releases'])
  assert.deepEqual(fs.readdirSync(path.join(installRoot, 'releases')), [])
  assert.equal(fs.existsSync(path.join(f.home, '.agentsdock')), false)
  assert.equal(fs.existsSync(path.join(f.home, '.config/agents-server')), false)
  assert.equal(await run(['install', '--non-interactive'], context), 74)
  const config = path.join(f.home, '.config/agents-server'), state = path.join(f.home, '.agentsdock'), admin = path.join(state, 'admin')
  assert.deepEqual(fs.readdirSync(config), [])
  assert.deepEqual(fs.readdirSync(state), ['admin'])
  assert.deepEqual(fs.readdirSync(admin), [])
  const identities = [installRoot, config, state, admin].map(name => fs.statSync(name).ino)
  assert.equal(await run(['install', '--non-interactive'], context), 0)
  assert.equal(installs, 3)
  assert.deepEqual([installRoot, config, state, admin].map(name => fs.statSync(name).ino), identities)
  assert.equal(fs.existsSync(completed), true)
  fs.mkdirSync(path.join(installRoot, '.install-lock'))
  await assert.rejects(run(['install'], context), /existing server installation or state/)
  assert.equal(installs, 3)
})

test('empty scaffolding admission still refuses files, links, releases, unsafe ownership and state', async t => {
  const cases = [
    (root) => fs.writeFileSync(root, 'existing'),
    (root, f) => fs.symlinkSync(f.home, root),
    (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, 'releases'), 'existing') },
    (root, f) => { fs.mkdirSync(root); fs.symlinkSync(f.home, path.join(root, 'releases')) },
    (root) => fs.mkdirSync(path.join(root, 'releases/older-version'), { recursive: true }),
    (root) => fs.mkdirSync(path.join(root, 'releases/.stage-other'), { recursive: true }),
    (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, '.hidden'), 'existing') },
    (root) => { fs.mkdirSync(root); fs.chmodSync(root, 0o777) },
    (root) => { fs.mkdirSync(path.join(root, 'releases'), { recursive: true }); fs.chmodSync(path.join(root, 'releases'), 0o777) },
    (root, f) => { fs.mkdirSync(root); f.context.uid++ },
    (root, f) => { fs.mkdirSync(root); fs.mkdirSync(path.join(f.home, '.agentsdock')); fs.writeFileSync(path.join(f.home, '.agentsdock/server-identity'), 'existing') },
    (root, f) => { fs.mkdirSync(root); fs.mkdirSync(path.join(f.home, '.config/agents-server'), { recursive: true }); fs.writeFileSync(path.join(f.home, '.config/agents-server/env'), 'existing') },
  ]
  for (const setup of cases) {
    const f = fixture(t)
    const root = path.join(f.home, '.local/share/agents-server')
    fs.mkdirSync(path.dirname(root), { recursive: true })
    setup(root, f)
    await assert.rejects(run(['install'], f.context), /existing server installation or state/)
    assert.equal(f.calls.length, 0)
    assert.equal(fs.lstatSync(root).isSymbolicLink() || fs.existsSync(root), true)
  }
})

test('empty config and state admission rejects credentials, histories, unknown directories, locks and unsafe entries', async t => {
  const cases = [
    ['.config/agents-server', (root) => fs.writeFileSync(root, 'existing')],
    ['.config/agents-server', (root, f) => fs.symlinkSync(f.home, root)],
    ['.config/agents-server', (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, 'env'), 'existing-token') }],
    ['.config/agents-server', (root) => fs.mkdirSync(path.join(root, 'admin'), { recursive: true })],
    ['.agentsdock', (root) => fs.writeFileSync(root, 'existing')],
    ['.agentsdock', (root, f) => fs.symlinkSync(f.home, root)],
    ['.agentsdock', (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, 'server-identity'), 'existing') }],
    ['.agentsdock', (root) => fs.mkdirSync(path.join(root, 'chats'), { recursive: true })],
    ['.agentsdock', (root) => fs.mkdirSync(path.join(root, '.lock'), { recursive: true })],
    ['.agentsdock', (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, 'admin'), 'existing') }],
    ['.agentsdock', (root, f) => { fs.mkdirSync(root); fs.symlinkSync(f.home, path.join(root, 'admin')) }],
    ['.agentsdock', (root) => { fs.mkdirSync(path.join(root, 'admin'), { recursive: true }); fs.writeFileSync(path.join(root, 'admin', 'pending-update'), 'existing') }],
    ['.agentsdock', (root) => fs.mkdirSync(path.join(root, 'admin', '.lock'), { recursive: true })],
    ['.zenithbot-agent', (root) => fs.mkdirSync(root)],
  ]
  for (const relative of ['.config/agents-server', '.agentsdock', '.agentsdock/admin']) {
    for (const mode of [0o770, 0o707, 0o300]) cases.push([relative, root => { fs.mkdirSync(root, { recursive: true }); fs.chmodSync(root, mode) }])
    cases.push([relative, (root, f) => { fs.mkdirSync(root, { recursive: true }); f.context.uid++ }])
  }
  for (const [relative, setup] of cases) {
    const f = fixture(t), root = path.join(f.home, relative)
    fs.mkdirSync(path.dirname(root), { recursive: true })
    setup(root, f)
    const before = fs.lstatSync(root)
    await assert.rejects(run(['install'], f.context), /existing server installation or state/)
    assert.equal(f.calls.length, 0)
    assert.equal(fs.lstatSync(root).ino, before.ino)
    if (before.isDirectory()) fs.chmodSync(root, 0o700)
  }
})

test('fresh guard refuses custom roots, root user, loaded service and inspection failure', async t => {
  const f = fixture(t)
  await assert.rejects(run(['install'], { ...f.context, uid: 501, env: { AGENTS_SERVER_INSTALL_DIR: '/custom' } }), /Custom installation/)
  await assert.rejects(run(['install'], { ...f.context, uid: 0 }), /without sudo/)
  for (const stdout of ['loaded', '']) await assert.rejects(run(['install'], { ...f.context, uid: 501, spawn: () => ({ status: 0, stdout }) }), /existing user service|absence could not/)
})

test('dry run checks absence but never starts installer', async t => {
  const f = fixture(t)
  await run(['install', '--dry-run'], { ...f.context, uid: 501 })
  assert.equal(f.calls.some(call => call[0] === '/bin/bash'), false)
  assert.equal(JSON.parse(f.output[0]).executed, false)
})

test('remote token transport requires HTTPS and refuses URLs with ambient authority', () => {
  for (const origin of ['http://remote.example', 'https://user:secret@example.com', 'https://example.com/api', 'https://example.com?q=1', 'https://example.com/#fragment']) assert.throws(() => validateOrigin(origin))
  assert.equal(validateOrigin('http://127.0.0.1:7850'), 'http://127.0.0.1:7850')
  assert.equal(validateOrigin('https://example.com/'), 'https://example.com')
})

test('invalid signature or insecure token file never contacts a server', async t => {
  const f = fixture(t)
  let requests = 0
  const context = { ...f.context, request: () => { requests++; throw new Error('must not request') } }
  fs.chmodSync(f.tokenFile, 0o644)
  await assert.rejects(run(updateArgs(f, 'http://127.0.0.1'), context), /Token file/)
  fs.chmodSync(f.tokenFile, 0o600)
  fs.appendFileSync(f.manifest, '\n')
  await assert.rejects(run(updateArgs(f, 'http://127.0.0.1'), context), /signature is invalid/)
  assert.equal(requests, 0)
})

test('actual HTTP boundary carries native auth and signed exact-target ensure, without installer', async t => {
  const f = fixture(t)
  const received = []
  const server = http.createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    received.push({ path: request.url, method: request.method, headers: request.headers, body: Buffer.concat(chunks).toString() })
    response.setHeader('content-type', 'application/json')
    response.end(JSON.stringify(request.url === '/api/health' ? { server_identity: 'server-one', server_instance_id: 'process-one' } : { reconciliation: 'pending', phase: 'waiting_for_idle', schedule_id: 'owned-schedule' }))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => server.close(resolve)))
  await run(updateArgs(f, `http://127.0.0.1:${server.address().port}`), f.context)
  assert.deepEqual(received.map(r => [r.method, r.path]), [['GET', '/api/health'], ['POST', '/api/admin/update/ensure']])
  for (const request of received) {
    assert.equal(request.headers['x-agentsdock-token'], 'fixture-token')
    assert.equal(request.headers.authorization, undefined)
    for (const header of ['sec-fetch-mode', 'sec-fetch-site', 'origin', 'referer']) assert.equal(request.headers[header], undefined)
  }
  const body = JSON.parse(received[1].body)
  assert.equal(body.expected_server_identity, 'server-one')
  assert.equal(body.expected_server_instance_id, 'process-one')
  assert.deepEqual(Buffer.from(body.manifest_base64, 'base64'), fs.readFileSync(f.manifest))
  assert.deepEqual(Buffer.from(body.signature_base64, 'base64'), fs.readFileSync(f.signature))
  assert.equal(JSON.parse(f.output[0]).reconciliation, 'pending')
  assert.equal(f.output.join('').includes('fixture-token'), false)
  assert.equal(f.calls.length, 0)
})

test('changed process identity or unsupported endpoint never falls back to installer', async t => {
  const f = fixture(t)
  let requests = 0
  let different = true
  const server = http.createServer((request, response) => {
    requests++
    if (request.url === '/api/health') response.end(JSON.stringify({ server_identity: 'server-one', server_instance_id: different ? 'other-process' : 'process-one' }))
    else { response.statusCode = 404; response.end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => server.close(resolve)))
  const origin = `http://127.0.0.1:${server.address().port}`
  await assert.rejects(run(updateArgs(f, origin), f.context), /process instance changed/)
  assert.equal(requests, 1)
  different = false
  await assert.rejects(run(updateArgs(f, origin), f.context), /HTTP 404/)
  assert.equal(f.calls.length, 0)
})
