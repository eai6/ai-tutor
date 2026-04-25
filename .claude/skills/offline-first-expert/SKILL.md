---
name: offline-first-expert
description: Expert on offline-first / low-connectivity software engineering patterns. Loads when working on sync engines, service workers, local databases, conflict resolution, or low-bandwidth UX. Covers local-first architecture, sync queues, UUIDs vs server IDs, append-only logs, last-write-wins, tombstones, optimistic UI, background sync, token refresh under degraded networks, and the specific offline strategy for this project's Seychelles/Tanzania pilots.
paths:
  - "mobile/src/sync/**"
  - "mobile/src/db/**"
  - "static/js/**sync**"
  - "apps/api/views/sync.py"
---

# Offline-First Expert

Expert on offline / intermittent-connectivity software development. The AI Tutor's pilot markets (Seychelles, Tanzania) have intermittent connectivity; the mobile app must feel fast and reliable regardless.

## Core principles

### 1. Local-first, not online-first

Every user action writes to LOCAL storage FIRST, returns to UI immediately, then syncs to server in the background. UI never blocks on network for primary actions.

**Wrong** (online-first):
```
User taps → HTTP POST → wait for response → update UI
```

**Right** (local-first):
```
User taps → write to SQLite → update UI (optimistic) → enqueue sync → worker pushes to server
                                                                        → server response reconciles SQLite
```

The mental shift: **the local DB is the source of truth for the user's experience**. Server sync is an eventual reconciliation, not a dependency.

### 2. Data model for offline

Two hard requirements for any offline-first entity:

**Client-generated IDs (UUIDs)**, not server-generated integers. The client must be able to reference an entity before the server has seen it.

```ts
const turnId = randomUUID();
await db.insert(session_turns).values({
  id: turnId,                // client UUID
  server_id: null,           // populated after sync
  ...
});
// Can immediately reference turnId elsewhere
```

**Per-row `synced` flag** (or a separate sync queue table). Worker finds unsynced rows and pushes them.

```sql
CREATE TABLE session_turns (
  id TEXT PRIMARY KEY,
  server_id INTEGER,              -- null until sync
  synced BOOLEAN DEFAULT false,
  content TEXT,
  client_generated_at INTEGER,   -- the time the client recorded this
  ...
);
```

### 3. Append-only where possible

Mutations are hard to sync. Additions are easy.

- `session_turns`: append-only. Never update, never delete. Sync = push to server in order.
- `engine_state`: mutable but **single writer per session** (the client that owns the session), so last-write-wins is safe.
- `progress`: mutable, merged with server — see conflict resolution below.

### 4. Optimistic UI with reconciliation

Show the write as if it succeeded. If the server rejects (rare), show the error and offer retry.

```ts
async function submitTurn(input: string) {
  const optimisticTurn = { id: uuid(), content: input, synced: false, ... };
  await db.insert(session_turns).values(optimisticTurn);
  // UI re-renders with the optimistic turn

  try {
    const serverTurn = await apiClient.post(`/sessions/${sid}/turns/`, optimisticTurn);
    await db.update(session_turns)
      .set({ server_id: serverTurn.id, synced: true })
      .where(eq(session_turns.id, optimisticTurn.id));
  } catch (e) {
    // Silent — sync worker will retry later. UI still shows the turn.
    await enqueueSync({ kind: 'turn', id: optimisticTurn.id });
  }
}
```

## Sync queue

The canonical pattern. A `sync_queue` table holds pending server writes. A worker drains it when online.

```sql
CREATE TABLE sync_queue (
  id TEXT PRIMARY KEY,          -- UUID
  kind TEXT NOT NULL,           -- 'turn' | 'engine_state' | 'exit_ticket' | ...
  payload_json TEXT NOT NULL,   -- serialized body
  session_id TEXT,              -- for ordering within a session
  created_at INTEGER NOT NULL,
  attempt_count INTEGER DEFAULT 0,
  last_attempt_at INTEGER,
  last_error TEXT
);
```

### Worker loop

```ts
async function syncWorker() {
  while (true) {
    if (!isOnline()) {
      await waitForOnline();  // block on connectivity event
      continue;
    }

    const items = await db.select()
      .from(sync_queue)
      .orderBy(sync_queue.created_at)
      .limit(10);

    if (items.length === 0) {
      await sleep(30_000);  // idle poll
      continue;
    }

    for (const item of items) {
      try {
        await pushItem(item);
        await db.delete(sync_queue).where(eq(sync_queue.id, item.id));
      } catch (e) {
        await recordFailure(item, e);
        if (item.attempt_count >= 5) {
          await moveToDeadLetter(item);
        }
      }
    }
  }
}
```

### Retry with backoff + jitter

```ts
const backoffMs = Math.min(
  30_000 * Math.pow(2, item.attempt_count),  // 30s, 1m, 2m, 4m, 8m
  300_000,                                    // cap at 5m
) + Math.random() * 10_000;                   // jitter to avoid thundering herd
```

Jitter matters — if 100 clients go offline then back online together, un-jittered retries stampede the server.

### Ordering within a session

For append-only data, preserve order:

- Include `client_generated_at` timestamp in the payload
- Or use a per-session sequence number
- Server rejects out-of-order writes or re-orders on ingest

## Conflict resolution

Three main strategies. Pick per data type.

### 1. Append-only (no conflict possible)

`session_turns`, `ExitTicketAttempt` rows. Client always wins because there's only one writer per row (the student on their device).

### 2. Last-write-wins (LWW)

`engine_state` within a single session. The client owns the session; server just stores the latest snapshot.

```ts
POST /api/v1/sessions/<id>/state/
Body: { engine_state: {...}, client_generated_at: <timestamp> }

// Server:
if (incoming.client_generated_at > existing.client_generated_at) {
  existing.engine_state = incoming.engine_state;
}
```

LWW works when:
- Single writer per resource (no split-brain across devices)
- Clock skew bounded (use client timestamps, not server `created_at`)

### 3. Server-authoritative merge

`StudentLessonProgress.best_score`. Both client and server might update (client via offline exit ticket, server via teacher override). Rule: `max(client_score, server_score)` wins. Server computes the merge on sync.

```python
# Server-side on sync:
def merge_progress(existing, incoming):
    if incoming.best_score and (existing.best_score is None or incoming.best_score > existing.best_score):
        existing.best_score = incoming.best_score
    if incoming.mastery_level == 'mastered':  # monotonic — never demote
        existing.mastery_level = 'mastered'
    existing.save()
```

### 4. CRDTs (for collaborative editing)

Overkill for this project. If you need real-time multi-device collaboration (not a current goal), consider automerge or yjs. Don't roll your own.

## Tombstones (for deletions)

Offline deletes are tricky — the server doesn't know the entity existed. Solution: soft-delete with a `deleted_at` tombstone + sync the tombstone, not the absence.

```ts
// Offline delete:
await db.update(items).set({ deleted_at: Date.now(), synced: false }).where(...);
await enqueueSync({ kind: 'delete_item', ... });

// Server eventually hard-deletes; client cleans up after confirmation.
```

For this project's current scope (no user-initiated deletes in offline mode), tombstones aren't needed.

## Network detection

React Native: `@react-native-community/netinfo` (more reliable than `expo-network` for change events).

```ts
import NetInfo from '@react-native-community/netinfo';

NetInfo.addEventListener((state) => {
  useNetworkStore.getState().setConnected(state.isConnected ?? false);
  if (state.isConnected) triggerSyncWorker();
});
```

**Don't rely on navigator.onLine alone** — it's lagging and sometimes wrong. Actively probe when needed (`HEAD /health/` with a short timeout).

### Degraded vs disconnected

Three states, not two:
- **Online**: full connectivity, normal flow
- **Degraded**: connected but slow/flaky (think 2G or edge-of-coverage)
- **Offline**: no connectivity

Degraded is the trickiest. Strategies:
- Short HTTP timeouts (5-10s, not 30s) — fail fast to offline mode
- Larger retry backoffs
- Don't block on background refreshes
- Show "syncing..." indicator but don't block UI

## Token refresh under degraded networks

JWT access tokens expire (~15 min); refresh tokens last longer (~30 days). If the refresh endpoint is unreachable:

1. If access token hasn't expired — continue (cached)
2. If access token expired but refresh token valid — try refresh; if fails, fall back to:
   - Cached data (read-only)
   - Queue writes (they'll re-auth on sync)
3. If refresh token expired — force re-login

**Grace period**: treat tokens as valid for a few minutes past expiration if offline. Server will reject on sync; handle that as a re-auth flow, not a catastrophic failure.

## Low-bandwidth UX patterns

### Image handling

- Server-side: generate low-quality JPEG (300KB max) for mobile consumers
- Progressive loading: serve LQIP (low-quality image placeholder) then full res
- Offline caching: fetch once, store in `FileSystem` (RN) or Cache Storage (web PWA)

### Lesson pack pre-download

When a student starts a lesson or selects "offline", pre-fetch everything needed:
- Lesson JSON (small)
- All step images (medium)
- Exit ticket (small)
- Optionally: predicted-next-lesson pack (speculative)

Pattern: zip pack + manifest, verify SHA-256, unpack to local cache. WiFi-only by default; user can override.

### WiFi-only downloads

Check `NetInfo.getState().type === 'wifi'` before large downloads. Respect user setting.

## Service workers (PWA path)

If you add a PWA (even transitional, before the RN app):

- `workbox` library — don't hand-roll service worker logic
- Three caching strategies:
  - **Static assets** (JS, CSS, images): cache-first with background update
  - **API responses** (GET): network-first with stale-while-revalidate fallback
  - **API mutations** (POST): queue via Background Sync API, drain when online
- Install prompt: don't spam; show after 2-3 engaged sessions

```ts
// Example workbox config:
workbox.routing.registerRoute(
  /\/api\/v1\/lessons\/\d+$/,
  new workbox.strategies.StaleWhileRevalidate({ cacheName: 'lessons' }),
);
```

## Testing offline

### Development

- Chrome DevTools: Network tab → "Offline"
- React Native: airplane mode on device (simulators are unreliable)
- Custom: override `NetInfo.fetch()` in a debug toggle

### Automated

- Mock network conditions in tests
- Test: action while offline → queued → comes online → syncs correctly
- Test: retry with backoff + jitter bounds
- Test: conflict resolution (LWW, merge, tombstone)
- Test: token refresh grace period

## This project's offline strategy

### Mobile (planned, `memory/mobile_rn_plan.md`)

- SQLite + Drizzle as primary store
- Sync queue table with background worker
- Policy-as-data state machine (no server round-trip for tutor turns in offline mode)
- On-device LLM (llama.rn) for offline tutor reply generation
- Lesson pack download before offline session
- Conflict resolution:
  - `session_turns`: append-only, client wins
  - `engine_state`: LWW per session
  - `StudentLessonProgress`: server merge, `max(client_score, server_score)`
  - `ExitTicketAttempt`: append-only

### Web (current, no offline)

Django templates today, fully online. If we ever add PWA capabilities for the web version, apply workbox patterns.

## Patterns NOT to use

❌ **Don't** keep an in-memory offline queue. It's lost on app restart.
❌ **Don't** use last-write-wins for `progress.best_score` — monotonic max merge instead.
❌ **Don't** hard-delete entities that may need to sync.
❌ **Don't** assume `navigator.onLine === true` means reachable. Actively probe.
❌ **Don't** block UI on sync — ever. Sync is background.
❌ **Don't** re-authenticate silently in a loop if refresh fails. One retry, then force re-login.
❌ **Don't** sync on every change — batch.
❌ **Don't** design conflict resolution ad-hoc per field. Pick a strategy per data type and document it.

## Further reading

- Martin Kleppmann, "Designing Data-Intensive Applications" — chapter on replication + consistency
- RedwoodJS / Linear engineering blogs — excellent local-first case studies
- `memory/mobile_rn_plan.md` — this project's concrete offline plan
- `memory/offline_mobile_architecture.md` — architecture decisions (policy-as-data, pluggable inference)
