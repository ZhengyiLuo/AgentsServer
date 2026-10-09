'use strict'

// Actual offline npm hooks/entrypoints with inert native and installer doubles.
// These are PATH propagation regressions, NEVER native-service acceptance.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

const supported = ['darwin', 'linux'].includes(process.platform) && process.getuid?.() !== 0

test('actual offline npm strips transient PATH before automatic setup, npm/npx helpers and scoped installation',
  { skip: !supported, timeout: 120000 }, t => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsdock-npm-path-'))
    t.after(() => fs.rmSync(root, { recursive: true, force: true }))
    const version = '0.0.0-path-fixture.1'
    const core = path.join(root, 'synthetic-core'), cli = path.join(root, 'cli')
    const account = path.join(root, 'home'), prefix = path.join(root, 'prefix'), local = path.join(root, 'local')
    for (const directory of [core, cli, account, local, path.join(core, 'npm'), path.join(core, 'server')]) {
      fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
    }
    const installer = fs.readFileSync(path.join(__dirname, '../../install.sh'), 'utf8')
    const start = installer.indexOf('SERVER_PATH=""\nappend_server_path()')
    const end = installer.indexOf('export PATH="$SERVER_PATH"', start)
    assert.ok(start > 0 && end > start, 'Reviewed installer PATH block must exist')
    const pathBlock = installer.slice(start, end + 'export PATH="$SERVER_PATH"'.length)
    // The maintained CLI and installer bytes are copied unchanged. A separate
    // wrapper supplies only explicit native absence and inert installer probes.
    fs.copyFileSync(path.join(__dirname, '../cli.cjs'), path.join(core, 'npm/actual-cli.cjs'))
    fs.copyFileSync(path.join(__dirname, '../../install.sh'), path.join(core, 'server/install.sh'))
    fs.writeFileSync(path.join(core, 'server/VERSION'), version + '\n')
    fs.writeFileSync(path.join(core, 'package.json'), JSON.stringify({ name: '@agentsdock/server', version,
      files: ['npm/', 'server/'], bin: { 'agentsdock-server': 'npm/cli.cjs' } }))
    fs.writeFileSync(path.join(core, 'npm/probe.cjs'), `
'use strict'
const fs = require('node:fs'), path = require('node:path')
const { execFileSync } = require('node:child_process')
const account = ${JSON.stringify(account)}, node = ${JSON.stringify(process.execPath)}
function observe(event, value) {
  if (process.env.HOME !== account) throw new Error('Fixture account changed')
  fs.appendFileSync(path.join(account, 'observations.jsonl'), JSON.stringify({event, ...value})+'\\n')
}
function persist(event, environment) {
  if (environment.HOME !== account) throw new Error('Unsafe fixture target')
  const metadataWriter = 'require("node:fs").appendFileSync(process.env.HOME+"/observations.jsonl",JSON.stringify({event:process.env.INERT_EVENT,persistedPath:process.env.PATH})+String.fromCharCode(10))'
  execFileSync('/bin/bash', ['-c', 'read_env_value() { return 0; }\\n' + ${JSON.stringify(pathBlock)} +
    '\\n"$INERT_NODE" -e "$INERT_WRITER"'], {env:{...environment, INERT_NODE:node,
      INERT_WRITER:metadataWriter, INERT_EVENT:event}, stdio:['ignore','pipe','pipe']})
}
module.exports={observe,persist}
if(require.main===module) {
  const args=process.argv.slice(2)
  if(JSON.stringify(args)!==JSON.stringify(['new','--name','sample','--port','7854','--bind','127.0.0.1'])) throw new Error('Unexpected helper arguments')
  persist('named-installer', process.env)
}
`)
    fs.writeFileSync(path.join(core, 'npm/cli.cjs'), `
#!/usr/bin/env node
'use strict'
const actual=require('./actual-cli.cjs'), probe=require('./probe.cjs')
probe.observe('incoming-core-load', {path:process.env.PATH})
function inert(command,args,options={}) {
  if(command==='/bin/launchctl' && args[0]==='print') return {status:113,stdout:'',stderr:'Could not find service'}
  if(command==='systemctl' && args[0]==='--user' && args[1]==='show') return {status:0,stdout:'not-found\\n',stderr:''}
  if(command!=='/bin/bash' || !args[0].endsWith('/server/install.sh')) throw new Error('Unexpected native call refused')
  probe.persist('default-installer',options.env)
  process.stdout.write('AGENTSDOCK_SETUP_RESULT='+JSON.stringify({server_url:'http://127.0.0.1:7850',server_version:${JSON.stringify(version)},access_token:'synthetic-never-real-secret'})+'\\n')
  return {status:0}
}
module.exports={persistentPath:actual.persistentPath, ensureFreshInstall:context=>actual.ensureFreshInstall({...context,spawn:inert}),
  run:(args,options)=>actual.run(args,{...options,spawn:inert})}
if(require.main===module) actual.run(process.argv.slice(2),{spawn:inert}).then(code=>{process.exitCode=code})
`.trimStart())
    // Only this inert local helper runs for `new`; production instances.sh and
    // install.sh are never executed. It uses the exact reviewed PATH block.
    const quote = value => "'" + value.replaceAll("'", "'\\''") + "'"
    fs.writeFileSync(path.join(core, 'server/instances.sh'), '#!/bin/bash\nexec ' + quote(process.execPath) +
      ' "$(dirname "$0")/../npm/probe.cjs" "$@"\n')
    for (const file of ['cli.cjs', 'postinstall.cjs']) fs.copyFileSync(path.join(__dirname, file), path.join(cli, file))
    const metadata = JSON.parse(fs.readFileSync(path.join(__dirname, 'package.json'), 'utf8'))
    fs.writeFileSync(path.join(cli, 'package.json'), JSON.stringify({...metadata, private: false, version,
      dependencies: {'@agentsdock/server':version}}))
    for (const file of ['user.npmrc', 'global.npmrc']) fs.writeFileSync(path.join(root, file), '', {mode:0o600})
    const stable = [...new Set([path.dirname(process.execPath), path.join(root, 'custom provider bin'),
      root + '/custom/link/../bin', path.join(account, '.local/bin'),
      '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'])]
    const env = {HOME:account, PATH:[...stable, process.env.PATH || ''].join(path.delimiter), LANG:'C.UTF-8',
      npm_config_cache:path.join(root,'cache'), npm_config_userconfig:path.join(root,'user.npmrc'),
      npm_config_globalconfig:path.join(root,'global.npmrc'), npm_config_update_notifier:'false'}
    for (const command of ['npm', 'npx']) {
      const result = spawnSync(command, ['--version'], { cwd:root, env, encoding:'utf8', timeout:10000 })
      if (result.error?.code === 'ENOENT') { t.skip(command + ' is unavailable'); return }
      assert.equal(result.status, 0, 'Isolated ' + command + ' version check failed: ' +
        (result.stderr || result.error || 'unknown'))
    }
    const invoke = (args, cwd=root, environment=env) => {
      const result=spawnSync(args[0],args.slice(1),{cwd,env:environment,encoding:'utf8',timeout:45000})
      assert.equal(result.status,0,'Inert npm fixture command failed: '+(result.stderr || result.error || 'unknown'))
      return result.stdout
    }
    const pack = directory => path.join(directory,JSON.parse(invoke(['npm','pack','--offline','--ignore-scripts','--json'],directory))[0].filename)
    const archives = [pack(core),pack(cli)]
    const observations = () => fs.readFileSync(path.join(account,'observations.jsonl'),'utf8').trim().split('\n').map(JSON.parse)
    const transient = entry => /\/node_modules\/\.bin\/?$/.test(entry) || /\/_npx\//.test(entry) || /\/node-gyp-bin\/?$/.test(entry)
    const assertPersisted = (entries, event) => {
      assert.ok(entries.some(item=>item.event==='incoming-core-load' && item.path.split(path.delimiter).some(transient)),
        'Real npm must first inject executable-search paths into the unchanged CLI')
      const persisted=entries.filter(item=>item.event===event)
      assert.equal(persisted.length,1)
      const values=persisted[0].persistedPath.split(path.delimiter)
      assert.ok(values.every(item=>path.isAbsolute(item) && !transient(item)))
      let previous = -1
      for (const entry of stable) {
        const index = values.indexOf(entry)
        assert.ok(index > previous, 'Stable Node/Homebrew/user/provider PATH spelling and order must survive')
        previous = index
      }
      assert.equal(fs.existsSync(path.join(account,'.agentsdock')),false)
      assert.equal(fs.existsSync(path.join(account,'Library/LaunchAgents')),false)
      assert.equal(fs.existsSync(path.join(account,'.config/systemd')),false)
    }
    const automatic=invoke(['npm','install','--global','--prefix',prefix,'--offline','--foreground-scripts','--no-audit','--no-fund',...archives])
    assert.match(automatic,/ is ready at /)
    assert.doesNotMatch(automatic,/synthetic-never-real-secret/)
    assertPersisted(observations(),'default-installer')
    let seen=observations().length
    invoke(['npm','install','--prefix',local,'--offline','--ignore-scripts','--no-audit','--no-fund',...archives])
    assert.equal(observations().length,seen,'Local install must not run setup')
    invoke(['npm','exec','--offline','--','agentsdock','setup','--non-interactive'],local)
    assertPersisted(observations().slice(seen),'default-installer')
    seen=observations().length
    invoke(['npx','--offline','agentsdock','new','sample','--port','7854','--bind','127.0.0.1'],local)
    assertPersisted(observations().slice(seen),'named-installer')
    seen=observations().length
    const fakeCache=path.join(root,'cache/_npx/fixture/node_modules/.bin')
    invoke([process.execPath,path.join(local,'node_modules/@agentsdock/server/npm/cli.cjs'),'install','--non-interactive'],local,
      {...env,PATH:fakeCache+path.delimiter+env.PATH})
    assertPersisted(observations().slice(seen),'default-installer')
  })
