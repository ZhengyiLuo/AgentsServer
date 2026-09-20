'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')
const crypto = require('node:crypto')
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
  await assert.rejects(run(['install'], { ...f.context, uid: 501 }), /existing server installation or state/)
  assert.equal(f.calls.length, 0)
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
