# Passive Claude discovery

Opening the runtime/model picker or rechecking installation must not initialize
a disposable authenticated Claude process. Such a process can begin native
OAuth renewal and exit before the replacement credential is saved. Do not
replace force-kill with an arbitrary sleep or assume graceful signals alone
establish renewal safety.

Claude readiness remains passive: installation/version is inspected, while a
real Claude request establishes authentication success or failure. Unknown
authentication is not a signed-out result.

Model discovery uses a bounded, five-minute, in-memory cache populated from
the initialization metadata of an existing real SDK connection. The pinned
SDK's `get_server_info()` returns metadata already obtained during `connect()`;
it sends no extra request. Only sanitized model values/labels survive the
projection. Catalog reads never create a client or extend a client's lifetime.

The cache is partitioned by executable, relevant provider environment and
native configuration revision. Workspace-specific settings are not promoted
into the global model picker. Native authentication failure clears the cache.
Cached metadata is not authentication evidence or permission to execute a model.

On a cache miss the existing API-key Models API, CLI help and labelled fallback
choices remain available. OAuth credentials are not read or exchanged by this
path. An authoritative cached empty picker stays empty. There is no second
process to expand the picker with historical model candidates; cached choices
follow the actual native connection. Unknown availability remains subject to
native validation when a real request runs.

Validation must cover refresh/recheck without authenticated probe processes,
real native requests, model selection and repeated refresh, reconnect, and
actual token renewal. A successful request immediately after login is not
proof that repeated-login incidents are fixed. Other older server installations
sharing the native credential store must be addressed before making that claim.
