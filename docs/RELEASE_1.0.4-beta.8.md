# AgentsServer 1.0.4-beta.8

- Stream Codex thinking summaries as they arrive, preserving section order
  and their position among tool activity.
- Save the authoritative completed summary once, without advancing transcript
  cursors for partial updates or duplicating text after reconnecting.
- Retain already visible summary text when a turn stops before the native item
  completes, with an explicit partial marker.
- Check custom-model thinking summary support separately from basic tool
  compatibility, and enable supported summaries for that model.
- Retain verified summary support per model and saved endpoint revision
  across server restarts. Failed optional summary checks leave basic
  compatibility results intact.

Use with AgentsDock 1.0.4-beta.8 for live summary display. Only reasoning
summaries supplied by the provider can be displayed; model support varies.
