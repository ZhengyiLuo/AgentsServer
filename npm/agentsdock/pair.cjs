'use strict'
const fs = require('node:fs')
const path = require('node:path')

function parsePair(args) {
  const result = { kind: 'pair', name: 'default', network: 'auto', url: null, output: null, link: false }
  if (args[0] && !args[0].startsWith('-')) result.name = args.shift()
  if (!/^[a-z][a-z0-9-]{0,31}$/.test(result.name)) throw new Error('Invalid instance name.')
  const seen = new Set()
  while (args.length) {
    const key = args.shift()
    if (seen.has(key)) throw new Error('Specify each pairing option once.')
    seen.add(key)
    if (key === '--link') result.link = true
    else if (key === '--network' && ['auto', 'tailscale', 'lan', 'local'].includes(args[0])) result.network = args.shift()
    else if (key === '--url' && args[0] && !args[0].startsWith('-')) result.url = args.shift().replace(/\/$/, '')
    else if (key === '--output' && args[0] && !args[0].startsWith('-')) result.output = args.shift()
    else throw new Error('Use pair [NAME] [--network auto|tailscale|lan|local] [--url URL] [--output FILE.png] [--link].')
  }
  if (result.link && result.output) throw new Error('Choose --link or --output, not both.')
  return result
}

function connectionLink({ serverUrl, serverUrls, accessToken, name, serverIdentity }) {
  const values = serverUrls ?? [serverUrl]
  if (!Array.isArray(values) || !values.length || values.length > 6) throw new Error('Invalid server addresses.')
  const urls = [...new Set(values.map(value => {
    if (typeof value !== 'string' || /[\x00-\x20\x7f]/.test(value)) throw new Error('Invalid server address.')
    let url
    try { url = new URL(value) } catch { throw new Error('Invalid server address.') }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Invalid server address.')
    return url.toString().replace(/\/$/, '')
  }))]
  if (!accessToken || (serverUrls && !serverIdentity?.trim()) ||
      [accessToken, name, serverIdentity ?? ''].some(value => /[\x00-\x1f\x7f]/.test(value))) throw new Error('Invalid pairing details.')
  const fields = new URLSearchParams(serverUrls
    ? { v: '2', urls: JSON.stringify(urls), token: accessToken, name }
    : { v: '1', url: urls[0], token: accessToken, name })
  if (serverIdentity) fields.set('identity', serverIdentity)
  const link = `agentsdock://connect#${fields}`
  if (Buffer.byteLength(link) > 1800) throw new Error('Connection details are too long for a QR code.')
  return link
}

function selectAddresses(choices, network, selectedUrl = null) {
  const eligible = network === 'auto' ? [choices.tailscale, ...(choices.lan ?? [])].filter(Boolean)
    : network === 'tailscale' ? [choices.tailscale].filter(Boolean) : choices[network] ?? []
  if (selectedUrl) {
    if (!eligible.includes(selectedUrl)) throw new Error('The requested URL is not a verified address for this instance/network. Run agentsdock status to check its addresses.')
    return [selectedUrl]
  }
  const candidates = [...new Set(eligible)]
  if (!candidates.length) throw new Error('No reachable address for this network. Run agentsdock status to check the binding. A localhost-only server cannot pair with a phone; use a reachable LAN or Tailscale binding. --network local is for this computer or an iOS simulator only.')
  if (candidates.length > 6) throw new Error('Too many server addresses. Select a network or use --url URL.')
  return candidates
}

async function pair(request, runtime, context, env) {
  const helper = path.join(runtime.payload, 'instances.sh')
  if (!fs.lstatSync(helper).isFile()) throw new Error('The instance helper is missing.')
  const result = context.spawn('/bin/bash', [helper, '_pair-info', request.name], {
    env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 20000, maxBuffer: 64000,
  })
  // Child output contains a credential. Never relay raw output/errors.
  if (result.error || result.status !== 0) throw new Error('Could not read that server. Run agentsdock list and select an installed instance owned by this user.')
  let details
  try { details = JSON.parse(result.stdout) } catch { throw new Error('Invalid instance pairing metadata.') }
  const candidates = selectAddresses(details.choices, request.network, request.url)
  const checks = await Promise.all(candidates.map(async serverUrl => {
    try {
      const response = await (context.fetch ?? fetch)(`${serverUrl}/api/health`, {
        headers: { 'X-AgentsDock-Token': details.accessToken }, redirect: 'error', signal: AbortSignal.timeout(8000),
      })
      if (!response.ok) return null
      const health = await response.json()
      if (health.ok !== true || typeof health.server_identity !== 'string' || !health.server_identity.trim()) return null
      return { serverUrl, identity: health.server_identity }
    } catch { return null }
  }))
  const first = checks.find(Boolean)
  if (!first) throw new Error('No address passed authenticated health. Check that this server is running and reachable, then try again.')
  if (checks.some(check => check && check.identity !== first.identity)) throw new Error('Server addresses reported different identities. Check the instance binding before sharing.')
  const serverUrls = checks.filter(Boolean).map(check => check.serverUrl)
  const serverUrl = serverUrls[0]
  const link = connectionLink({ ...details, serverUrl, ...(serverUrls.length > 1 ? { serverUrls } : {}), serverIdentity: first.identity })
  if (request.link) { context.print(link); return 0 }
  const qr = context.qr ?? require('./qr-renderer.cjs')
  if (request.output) {
    const png = await qr.toBuffer(link, { type: 'png', errorCorrectionLevel: 'M', margin: 4, scale: 6 })
    fs.writeFileSync(request.output, png, { flag: 'wx', mode: 0o600 })
  }
  context.print(`Pair ${request.name}\nServer addresses:\n${serverUrls.join('\n')}\nOpen AgentsDock mobile > Servers > Add server > Scan QR code.\nThe QR contains your access token. Keep it private. Mobile automatically tries the included addresses over Wi-Fi or Tailscale.`)
  if (request.network === 'local') context.print('Local-only: works on this computer/iOS simulator, not a physical phone.')
  if (request.output) context.print('QR image saved. Keep this file private.')
  else context.print(await qr.toString(link, { type: 'terminal', small: true, errorCorrectionLevel: 'M', margin: 4 }))
  return 0
}

async function showSetupQr(name, runtime, context, env) {
  try {
    await pair(parsePair([name]), runtime, context, env)
    return true
  } catch {
    // Setup/token retrieval has already succeeded. Never expose private child errors
    // or fail the installation merely because this network cannot pair yet.
    context.print(`Connection QR unavailable. Once the server is reachable from your phone, run: agentsdock pair ${name}\nFor a specific network, add --network lan or --network tailscale.`)
    return false
  }
}

if (require.main === module) {
  const name = process.argv[2]
  if (!process.stdin.isTTY || !process.stdout.isTTY || process.argv.length !== 3 || !/^[a-z][a-z0-9-]{0,31}$/.test(name ?? '')) {
    process.exitCode = 1
  } else {
    void showSetupQr(name, { payload: path.resolve(__dirname, '../..') }, {
      spawn: require('node:child_process').spawnSync,
      print: text => process.stdout.write(`${text}\n`),
    }, process.env)
  }
}

module.exports = { parsePair, connectionLink, selectAddresses, pair, showSetupQr }
