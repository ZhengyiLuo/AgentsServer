# AgentsServer 1.0.4-beta.5

- Accept native Codex forks when the requested workspace and Codex's resolved
  workspace point to the same folder, including symlinked paths.
- Preserve checks that the fork belongs to the original conversation and ends
  at the requested completed turn. The source chat continues running.
- Report clearer fork failure reasons and record failed live-fork attempts for
  diagnosis without logging provider credentials or conversation content.
