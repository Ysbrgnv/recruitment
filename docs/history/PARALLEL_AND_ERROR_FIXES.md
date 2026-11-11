# Minimal change release

The original project structure and behavior are preserved. Only these changes were introduced:

- 16 concurrent PostgreSQL queue consumers for the `general` (HH) lane.
- 8 concurrent PostgreSQL queue consumers for the `browser` (X-Ray) lane.
- X-Ray hard cap remains 8 active browser jobs.
- PostgreSQL row locking remains `FOR UPDATE SKIP LOCKED`, with one running job per task.
- HH vacancy refresh is single-flight, so concurrent startup/manual refresh calls do not duplicate work.
- Expected HH authorization failures are logged once as warnings without repeated stack traces.
- The existing `.hh-refresh-state.json` guard remains active across restarts, blocking repeated refresh attempts with the same rejected token pair.
- A failed vacancy refresh preserves the previous cached vacancy list and exposes the safe error in the UI.

This release uses the isolated Compose project `recruitment-clean-v3-parallel`; it does not touch prior v3/v4 volumes.

