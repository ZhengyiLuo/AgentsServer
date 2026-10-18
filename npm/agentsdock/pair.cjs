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

function connectionLink({ serverUrl, accessToken, name, serverIdentity }) {
  const url = new URL(serverUrl)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash ||
      !accessToken || /[\x00-\x1f\x7f]/.test(accessToken)) throw new Error('Invalid pairing details.')
  const fields = new URLSearchParams({ v: '1', url: url.toString().replace(/\/$/, ''), token: accessToken, name })
  if (serverIdentity) fields.set('identity', serverIdentity)
  const link = `agentsdock://connect#${fields}`
  if (Buffer.byteLength(link) > 1800) throw new Error('Connection details are too long for a QR code.')
  return link
}

function selectAddress(choices, network, selectedUrl = null) {
  const eligible = network === 'auto' ? [choices.tailscale, ...(choices.lan ?? [])].filter(Boolean)
    : network === 'tailscale' ? [choices.tailscale].filter(Boolean) : choices[network] ?? []
  if (selectedUrl) {
    if (!eligible.includes(selectedUrl)) throw new Error('The requested URL is not a verified address for this instance/network. Run agentsdock status to check its addresses.')
    return selectedUrl
  }
  const candidates = network === 'auto' && choices.tailscale ? [choices.tailscale] : eligible
  if (candidates.length > 1) throw new Error(`Multiple addresses are available. Select the one your phone can reach with --url URL:\n${candidates.join('\n')}`)
  const url = candidates[0]
  if (!url) throw new Error('No reachable address for this network. Run agentsdock status to check the binding. A localhost-only server cannot pair with a phone; use a reachable LAN or Tailscale binding. --network local is for this computer or an iOS simulator only.')
  return url
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
  const serverUrl = selectAddress(details.choices, request.network, request.url)
  let health
  try {
    const response = await (context.fetch ?? fetch)(`${serverUrl}/api/health`, {
      headers: { 'X-AgentsDock-Token': details.accessToken }, redirect: 'error', signal: AbortSignal.timeout(8000),
    })
    if (!response.ok) throw new Error('health failed')
    health = await response.json()
    if (health.ok !== true || typeof health.server_identity !== 'string' || !health.server_identity.trim()) throw new Error('identity missing')
  } catch { throw new Error('The selected address did not pass authenticated health. Check that this server is running and reachable, then try again.') }
  const link = connectionLink({ ...details, serverUrl, serverIdentity: health.server_identity })
  if (request.link) { context.print(link); return 0 }
  const qr = context.qr ?? require('./qr-renderer.cjs')
  if (request.output) {
    const png = await qr.toBuffer(link, { type: 'png', errorCorrectionLevel: 'M', margin: 4, scale: 6 })
    fs.writeFileSync(request.output, png, { flag: 'wx', mode: 0o600 })
  }
  context.print(`Pair ${request.name}\nServer: ${serverUrl}\nOpen AgentsDock mobile → Servers → Scan connection QR.\nThe QR contains your access token. Share it only with someone who should have access.`)
  if (request.network === 'local') context.print('Local-only: works on this computer/iOS simulator, not a physical phone.')
  if (request.output) context.print('QR image saved. Keep this file private.')
  else context.print(await qr.toString(link, { type: 'terminal', small: true, errorCorrectionLevel: 'M', margin: 4 }))
  return 0
}

module.exports = { parsePair, connectionLink, selectAddress, pair }
