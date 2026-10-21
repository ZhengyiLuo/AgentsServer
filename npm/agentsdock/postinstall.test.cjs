'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')
const { PassThrough } = require('node:stream')
const { postinstall, skipReason, terminalPrint } = require('./postinstall.cjs')

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsdock-postinstall-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const prefix = path.join(root, 'prefix')
  const packageRoot = path.join(prefix, 'lib/node_modules/agentsdock')
  fs.mkdirSync(packageRoot, { recursive: true })
  const output = [], calls = []
  const runtime = { version: '1.2.3-beta.4', coreRoot: path.join(root, 'core'),
    preflight: context => calls.push({ preflight: context.env }) }
  const context = { env: { npm_config_global: 'true', npm_config_prefix: prefix,
    npm_command: 'install', npm_lifecycle_event: 'postinstall' }, packageRoot,
    home: root, uid: 1000, platform: 'linux', load: () => runtime,
    terminalPrint: () => false,
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

test('fresh setup shows URL and token only on the controlling terminal, never npm output', async t => {
  const f = fixture(t), terminal = []
  f.context.terminalPrint = value => { terminal.push(value); return true }
  assert.equal(await postinstall(f.context), 0)
  assert.match(terminal[0], /Setting up your first/)
  assert.match(terminal[1], /Your new service is up!/)
  assert.match(terminal[1], /URL:     http:\/\/127.0.0.1:7850/)
  assert.match(terminal[1], /Access token \(default\):\nprivate-token\n/)
  assert.match(terminal[1], /Keep this token private/)
  assert.doesNotMatch(terminal.join('\n'), /secret-stdout|secret-stderr/)
  assert.equal(f.output.length, 0)
})

test('terminal output failure falls back to a public summary, never the token', async t => {
  const f = fixture(t)
  assert.equal(await postinstall(f.context), 0)
  assert.match(f.output.at(-1), /ready at http/)
  assert.doesNotMatch(f.output.join('\n'), /private-token/)
})

test('automatic QR goes only to the controlling terminal, never lifecycle logs', async t => {
  const f = fixture(t), terminal = []
  let generated = 0
  f.context.showSetupQr = async (name, runtime, context, env) => {
    generated++
    assert.equal(name, 'default')
    assert.equal(runtime, f.runtime)
    assert.deepEqual(env, { HOME: f.root })
    context.print('private-qr-content')
  }
  await postinstall(f.context)
  assert.equal(generated, 0, 'headless installation must not even generate a QR')
  f.context.terminalPrint = text => { terminal.push(text); return true }
  await postinstall(f.context)
  assert.equal(generated, 1)
  assert.match(terminal.join('\n'), /private-qr-content/)
  assert.doesNotMatch(f.output.join('\n'), /private-qr-content|private-token/)
})

test('controlling terminal writer requires a tty, handles partial writes and closes on failure', () => {
  for (const mode of ['ok', 'not-tty', 'no-terminal', 'write-error', 'zero-write']) {
    let closed = false, written = '', opened = false
    const io = {
      openSync: (name, flags) => {
        assert.equal(name, '/dev/tty')
        assert.ok(flags & fs.constants.O_NOCTTY)
        assert.equal(flags & fs.constants.O_NONBLOCK, 0)
        if (mode === 'no-terminal') throw new Error('no controlling terminal')
        opened = true
        return 17
      },
      isatty: fd => { assert.equal(fd, 17); return mode !== 'not-tty' },
      writeSync: (fd, bytes, offset, length) => {
        assert.equal(fd, 17)
        if (mode === 'write-error') throw new Error('closed terminal')
        if (mode === 'zero-write') return 0
        const count = Math.min(3, length)
        written += bytes.subarray(offset, offset + count).toString()
        return count
      },
      closeSync: fd => { assert.equal(fd, 17); closed = true },
    }
    assert.equal(terminalPrint('private-token', io), mode === 'ok')
    assert.equal(written, mode === 'ok' ? 'private-token' : '')
    assert.equal(closed, opened)
  }
})

test('terminal QR resumes exact bytes after temporary backpressure and interrupted writes', () => {
  const expected = Buffer.from('QR ' + String.fromCodePoint(0x2588, 0x2580).repeat(20) + '\n')
  const chunks = []
  let calls = 0, pauses = 0, closed = false
  const io = {
    openSync: () => 17,
    isatty: () => true,
    writeSync: (_fd, bytes, offset, length) => {
      calls++
      const code = { 2: 'EAGAIN', 3: 'EWOULDBLOCK', 5: 'EINTR' }[calls]
      if (code) throw Object.assign(new Error('temporary terminal pressure'), { code })
      const count = Math.min(5, length)
      chunks.push(Buffer.from(bytes.subarray(offset, offset + count)))
      return count
    },
    closeSync: () => { closed = true },
  }
  assert.equal(terminalPrint(expected.toString(), io, () => { pauses++ }), true)
  assert.deepEqual(Buffer.concat(chunks), expected)
  assert.equal(pauses, 3)
  assert.equal(closed, true)
})

test('repeated transient terminal write errors have a bounded retry budget and still close', () => {
  let pauses = 0, closed = false
  const io = {
    openSync: () => 17,
    isatty: () => true,
    writeSync: () => { throw Object.assign(new Error('busy'), { code: 'EAGAIN' }) },
    closeSync: () => { closed = true },
  }
  assert.equal(terminalPrint('private QR', io, () => { pauses++ }), false)
  assert.equal(pauses, 100)
  assert.equal(closed, true)
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
  const terminal = []
  f.context.terminalPrint = value => { terminal.push(value); return true }
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
    assert.doesNotMatch(terminal.join('\n'), /ready|private-token|Your new service is up/)
  }
})

test('unsafe or missing token is never rendered as terminal control content', async t => {
  const f = fixture(t)
  for (const token of [null, '', '\u001b[2J', 'token\nextra-line', 'x'.repeat(4097)]) {
    const terminal = []
    const launch = () => {
      const child = new EventEmitter()
      child.stdout = new PassThrough(); child.stderr = new PassThrough()
      queueMicrotask(() => {
        child.stdout.write('AGENTSDOCK_SETUP_RESULT=' + JSON.stringify({server_url: 'http://127.0.0.1:7850',
          server_version: f.runtime.version, access_token: token}) + '\n')
        child.emit('close', 0)
      })
      return child
    }
    assert.equal(await postinstall({ ...f.context, launch,
      terminalPrint: value => { terminal.push(value); return true } }), 0)
    assert.doesNotMatch(terminal.join('\n'), /Access token \(default\):|\u001b|extra-line|xxxx/)
    assert.match(terminal.at(-1), /agentsdock token default/)
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
      /did not complete.*uv[\s\S]*AGENTSDOCK_SKIP_SETUP/.test(error.message) && !error.message.includes('private-token'))
    assert.doesNotMatch(f.output.join('\n'), /ready|private-token|xxx/)
  }
})

test('uv failures provide platform-appropriate installation and exact-version retry commands', async t => {
  for (const platform of ['darwin', 'linux']) {
    for (const version of ['1.0.10', '1.0.11-beta.1']) {
      const f = fixture(t)
      f.runtime.version = version
      let launches = 0
      const launch = () => {
        launches++
        const child = new EventEmitter()
        child.stdout = new PassThrough(); child.stderr = new PassThrough()
        queueMicrotask(() => {
          child.stderr.write('private-token\nUnavailable prerequisite: uv\nInstall the trusted ')
          child.stderr.write('uv package manager before running this installer.\n')
          child.emit('close', 1)
        })
        return child
      }
      await assert.rejects(postinstall({ ...f.context, platform, launch }), error => {
        assert.match(error.message, /uv is missing from PATH or cannot run/)
        assert.match(error.message, /needs uv to prepare its Python environment/)
        assert.match(error.message, /https:\/\/docs\.astral\.sh\/uv\/getting-started\/installation\//)
        assert.match(error.message, /\n  uv --version\n/)
        assert.ok(error.message.includes(`npm install -g agentsdock@${version} --foreground-scripts`))
        assert.ok(error.message.includes(`AGENTSDOCK_SKIP_SETUP=1 npm install -g agentsdock@${version}`))
        assert.match(error.message, /\n  agentsdock setup\n/)
        assert.equal(error.message.includes('brew install uv'), platform === 'darwin')
        assert.match(error.message, /Existing state is never deleted automatically/)
        assert.doesNotMatch(error.message, /private-token|Unavailable prerequisite/)
        return true
      })
      assert.equal(launches, 1) // Guidance never executes brew, a download or a retry.
      assert.equal(f.calls.length, 1) // Existing read-only preflight only.
      assert.doesNotMatch(f.output.join('\n'), /ready|private-token/)
    }
  }
})

test('unrelated setup failures do not suggest that installing uv will fix them', async t => {
  const f = fixture(t)
  f.context.launch = () => {
    const child = new EventEmitter()
    child.stdout = new PassThrough(); child.stderr = new PassThrough()
    queueMicrotask(() => {
      child.stderr.write('private diagnostic: service manager failed\n')
      child.emit('close', 1)
    })
    return child
  }
  await assert.rejects(postinstall(f.context), error => {
    assert.match(error.message, /Automatic setup did not complete successfully/)
    assert.match(error.message, /AGENTSDOCK_SKIP_SETUP/)
    assert.doesNotMatch(error.message, /brew install uv|uv is missing|private diagnostic/)
    return true
  })
})
