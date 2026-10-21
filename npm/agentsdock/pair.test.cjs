const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { parsePair, selectAddresses, connectionLink, pair, showSetupQr } = require('./pair.cjs')
const { parse } = require('./cli.cjs')

test('pair defaults, alias, network selection and rejected ambiguous arguments', () => {
  assert.equal(parse(['pair']).name, 'default')
  assert.equal(parse(['qrcode', 'work']).name, 'work')
  assert.equal(parse(['pair', 'work', '--network', 'lan']).network, 'lan')
  for (const args of [['../bad'], ['--network', 'public'], ['work', 'other'], ['--output'],
    ['--link', '--link'], ['--link', '--output', 'file.png']]) assert.throws(() => parsePair(args))
  const choices = { local: ['http://127.0.0.1:7851'], lan: ['http://192.168.1.10:7851'], tailscale: 'http://100.64.0.10:7851' }
  assert.deepEqual(selectAddresses(choices, 'auto'), [choices.tailscale, ...choices.lan])
  assert.deepEqual(selectAddresses(choices, 'lan'), choices.lan)
  assert.throws(() => selectAddresses({ local: choices.local }, 'auto'), /localhost-only/)
  assert.deepEqual(selectAddresses({ local: choices.local }, 'local'), choices.local)
  assert.equal(selectAddresses({ lan: ['http://192.168.1.10:7851', 'http://172.17.0.1:7851'] }, 'auto').length, 2)
  assert.deepEqual(selectAddresses(choices, 'auto', choices.lan[0]), [choices.lan[0]])
  assert.throws(() => selectAddresses(choices, 'auto', 'https://other.example'), /not a verified address/)
  assert.throws(() => selectAddresses(choices, 'auto', choices.local[0]), /not a verified address/)
})

test('QR contract keeps credentials in fragment and rejects unsafe payloads', () => {
  const link = connectionLink({ serverUrl: 'http://192.168.1.1:7851', accessToken: 'a+&?#=', name: '工作', serverIdentity: 'id' })
  const url = new URL(link)
  assert.equal(url.search, '')
  assert.equal(new URLSearchParams(url.hash.slice(1)).get('token'), 'a+&?#=')
  for (const serverUrl of ['file:///x', 'https://user:pw@host', 'http://host?token=x'])
    assert.throws(() => connectionLink({ serverUrl, accessToken: 'x', name: 'work' }))
})

test('real QR output needs authenticated identity; private PNG never overwrites; failures hide child credentials', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pair-test-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.writeFileSync(path.join(root, 'instances.sh'), '# fixture')
  const output = [], requests = []
  const token = 'synthetic-test-token'
  const context = {
    print: message => output.push(message),
    spawn: (...args) => { requests.push(args); return { status: 0, stdout: JSON.stringify({ name: 'work', accessToken: token, choices: { lan: ['http://192.168.1.1:7851'] } }) } },
    fetch: async (url, options) => { assert.equal(options.headers['X-AgentsDock-Token'], token); assert.equal(options.redirect, 'error'); return { ok: true, json: async () => ({ ok: true, server_identity: 'fixture-id' }) } },
  }
  const request = parsePair(['work', '--output', path.join(root, 'pair.png')])
  await pair(request, { payload: root }, context, { HOME: root })
  assert.equal(fs.readFileSync(request.output).subarray(1, 4).toString(), 'PNG')
  assert.equal(fs.statSync(request.output).mode & 0o777, 0o600)
  assert.ok(!output.join('\n').includes(token))
  assert.deepEqual(requests[0][1].slice(1), ['_pair-info', 'work'])
  assert.equal(requests[0][2].stdio[2], 'pipe')
  await assert.rejects(pair(request, { payload: root }, context, {}), /EEXIST/)
  context.spawn = () => ({ status: 1, stderr: token, stdout: token })
  await assert.rejects(pair(request, { payload: root }, context, {}), error => !error.message.includes(token))
  context.spawn = () => ({ status: 0, stdout: JSON.stringify({ name: 'work', accessToken: token, choices: { lan: ['http://192.168.1.1:7851'] } }) })
  context.fetch = async () => ({ ok: false })
  await assert.rejects(pair(request, { payload: root }, context, {}), /authenticated health/)
})

test('setup QR uses authenticated reachable addresses and its failure is nonfatal and credential-free', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'setup-qr-test-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.writeFileSync(path.join(root, 'instances.sh'), '# fixture')
  const output = [], payloads = []
  const context = {
    print: text => output.push(text),
    spawn: () => ({ status: 0, stdout: JSON.stringify({ name: 'work', accessToken: 'private-token', choices: { lan: ['http://192.168.1.1:7852'] } }) }),
    fetch: async () => ({ ok: true, json: async () => ({ ok: true, server_identity: 'server-id' }) }),
    qr: { toString: async link => { payloads.push(link); return 'fixture-qr' } },
  }
  assert.equal(await showSetupQr('work', { payload: root }, context, {}), true)
  assert.equal(new URLSearchParams(new URL(payloads[0]).hash.slice(1)).get('identity'), 'server-id')
  assert.match(output.join('\n'), /Add server > Scan QR code/)
  assert.doesNotMatch(output.join('\n'), /private-token/)
  output.length = 0
  context.fetch = async () => { throw new Error('private-token') }
  assert.equal(await showSetupQr('work', { payload: root }, context, {}), false)
  assert.equal(payloads.length, 1)
  assert.match(output.join('\n'), /agentsdock pair work/)
  assert.doesNotMatch(output.join('\n'), /private-token/)
})

test('automatic QR includes authenticated LAN and Tailscale routes, omits failures and rejects mixed identities', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'multi-address-qr-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.writeFileSync(path.join(root, 'instances.sh'), '# fixture')
  const urls = ['http://100.64.0.2:7852', 'http://192.168.1.2:7852']
  let mismatch = false, fail = false
  const output = []
  const context = {
    print: text => output.push(text),
    spawn: () => ({ status: 0, stdout: JSON.stringify({ name: 'work', accessToken: 'fixture-token', choices: { tailscale: urls[0], lan: [urls[1]] } }) }),
    fetch: async url => ({ ok: !(fail && url.startsWith(urls[0])), json: async () => ({ ok: true, server_identity: mismatch && url.startsWith(urls[1]) ? 'foreign' : 'same' }) }),
  }
  const request = parsePair(['work', '--link'])
  await pair(request, { payload: root }, context, {})
  let fields = new URLSearchParams(new URL(output.pop()).hash.slice(1))
  assert.equal(fields.get('v'), '2')
  assert.deepEqual(JSON.parse(fields.get('urls')), urls)
  fail = true
  await pair(request, { payload: root }, context, {})
  fields = new URLSearchParams(new URL(output.pop()).hash.slice(1))
  assert.equal(fields.get('v'), '1')
  assert.equal(fields.get('url'), urls[1])
  fail = false
  mismatch = true
  await assert.rejects(pair(request, { payload: root }, context, {}), /different identities/)
})
