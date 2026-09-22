# AgentsServer 1.0.5

Paired with AgentsDock 1.0.5, this release fixes migration from existing managed
servers and recovery after the failed 1.0.4 upgrade.

- Normalize safely owned legacy installation-root permissions under the
  installation lock. Preserve the live incumbent and its Hub database when
  activation fails before service takeover.
- Retire only the exact failed update fence, verify the original server before
  releasing recovery state, and preserve staged runtime files while an
  activation transaction remains unfinished.
- Do not replay durably accepted native-goal follow-ups during queue recovery.
- Restore native retries for custom endpoints and supported model effort
  choices, preserve Claude parent settings for side questions, avoid redundant
  fork-history scans, and report failed scheduled turns accurately.

Existing users should use the app's coordinated update and recovery controls.
The signed legacy server bridge remains available for older managed servers.
Updates preserve existing server identity, credentials, chats and Team Hub data,
and wait for idle before replacing execution. Multiple named services remain
outside this npm migration release line.
