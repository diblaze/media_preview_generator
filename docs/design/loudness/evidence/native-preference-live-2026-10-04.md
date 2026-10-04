# Native Plex loudness preference: isolated live proof

Date: 2026-10-04. Tested the reviewed PR #362 follow-up source in retained
`mlab-loudness-preference-candidate` on storage, localhost port 18088, against the
real isolated `mlab-plex` server. Production was not used for these preference writes.

The source snapshot includes the final optional-button accessibility and text-contrast corrections.
Its SHA-256 manifest hash is
`6bfe62a7041534f53e17428e3c62ac8cef6a4658413d2b70ccd6971a2d09eca5`.
The base image is official dev `9142e7d`; this proof uses a source overlay and does
not claim to validate a future packaged image.

- Read the original native `LoudnessAnalysisBehavior=never`, temporarily set the
  isolated server to `scheduled`, and verified the real preference API after each step.
- Setup Health displayed the schedule as informational, without recommending Never.
  The optional action was outside bulk fixes.
- An unauthenticated POST was rejected by the actual CSRF middleware (HTTP 400).
- Empty selected libraries and an unavailable local writer each caused HTTP 409;
  neither changed the native preference. The writer refusal used an existing
  directory without a Plex database, exercising the real current capability check.
- Browser confirmation named music, unselected libraries, exclusions, preserved
  measurements, and how to restore the native schedule. Cancel sent no POST.
- Confirm sent exactly one authenticated POST with `{}`. Native Plex GET then
  returned `never`; the refreshed row displayed Never and removed the action.
- The exact original `never` preference and candidate settings were restored in
  `finally`. The app remained paused with no jobs, and the browser reported no errors.

Sanitized runtime evidence and screenshots are retained on storage under
`/tmp/loudness-preference-20261004/`: `proof.json`, `source-sha256.json`,
`readiness-scheduled.png`, `confirm-global-impact.png`, and `readiness-never.png`.
Private copied app configuration is separate and is not included in this document.
