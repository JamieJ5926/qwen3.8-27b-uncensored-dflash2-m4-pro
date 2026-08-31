# Phase 0. Freeze contract and ownership

## Goal

Record the shared contract before any code moves. Everything downstream cites this document.

## Files (3)

- `plan/01-local-model-coordinator/overview.md`
- `plan/01-local-model-coordinator/phase-0-freeze-contract.md`
- `plan/01-local-model-coordinator/phase-1-benchloop-repair.md`

## Work

Write the data shapes, module boundaries, ownership map, route-trace schema, hardware-lane lease, and acceptance gates from overview.md. Confirm each existing owner stays owner. State that the DFlash server is inference-only and the router never imports from it. Record the MacBook Pro M4 Pro 48GB resource envelope, the 19.49GB Qwen peak evidence, model and cache ownership under `/Volumes/BBALL_GM_DATA`, disk floors, and the rule that only the hardware-lane owner may start, stop, call, benchmark, download, mutate, or delete local model resources.

## Static checks

- Every shape in overview.md has a named producer and consumer.
- Every route target has a named owner module.
- The host-wide lock contract names one hardware-lane owner, process id, active model, port, model path, run id, action, start time, and cleanup procedure. Stale-lock recovery requires process verification.
- Storage inventory lists model caches under `/Volumes/BBALL_GM_DATA`, Docker disk use, benchmark artifacts, temporary download overhead, required free-space floors, and removable artifacts by owner.
- Markdown links resolve.

## Runtime checks

- The hardware-lane owner performs the read-only preflight. Inventory listeners, model processes, the lock, unified-memory pressure, swap, `/Volumes/BBALL_GM_DATA` availability and free space, and Docker disk use. No mutation of the server. The gate fails if another actor holds the lock, more than one local model process exists, or a resource floor is not met.

## Exit gate

Contract document merged. Ownership confirmed. One exclusive hardware lane and host-wide lock defined. Memory and external-volume storage floors recorded before any live benchmark or download.
