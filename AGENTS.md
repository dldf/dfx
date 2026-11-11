# Working constraints

- Never make changes unless the user explicitly requests them or grants
  permission beforehand. Questions and requests for explanations do not
  authorize changes.
- Keep tasks narrowly scoped and minimize usage.
- Before implementing a feature, inspect relevant work on
  `archive/accessconverter-pilot` and reuse suitable parts without restoring
  unrelated infrastructure.
- Do only the specific change requested. Do not expand it into cleanup,
  modernization, documentation, tooling, or unrelated fixes.
- Inspect only files needed for the change. Avoid broad repository exploration,
  repeated checks, and exhaustive validation.
- Explain the intended change before editing. Preserve unrelated user changes.
- Before making coding changes, present an assessment table with columns:
  `#`, `Proposed change`, `Effort`, `Regression risk`, and `Main uncertainty`.
  Number the rows so the user can select which changes to implement. Use
  Low/Moderate/High ratings, and distinguish document-specific corrections
  from general converter fixes where relevant.
- Immediately below the table, add a short note with recommendations,
  caveats, dependencies, and any conflicts with existing behavior,
  instructions, or other proposed changes. State when none are identified.
- Wait for the user's row selections before implementing the assessed
  changes. Implement only the selected scope; do not request approval again
  for rows the user has already approved. If new work or materially different
  risks emerge, update the assessment before expanding the scope.
- For folder moves, update affected references without rewriting moved content.
- Use the smallest relevant verification. Run the entire test suite only when
  the change requires it.
- If substantial additional work is discovered, describe it briefly and stop
  before doing it.
- Finish with a short summary of what changed and what was verified.

## Git synchronization

- When the user says "sync", "sync all", or "sync to GitHub", first clarify
  which changes and destination branch to synchronize, including whether to
  merge. Then complete the agreed sync and branch cleanup.
- Push only the agreed branches. "Sync all" does not mean push every local branch.
- Clear finished task branches after synchronization, both locally and on
  GitHub where applicable. Before deleting a branch, verify that its work is
  preserved in the agreed destination and it has no unique work left to retain.
- Keep the default branch, intentional archive branches, and unfinished work.
  Do not create another task branch just to carry the same uncommitted changes
  forward; finish the current task's commit and branch lifecycle first.
- Report what was synchronized, which branches were removed, and anything
  that remains unresolved. Do not leave finished branch cleanup as an optional
  follow-up.
