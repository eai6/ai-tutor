import { eq } from 'drizzle-orm';

import { getDb, isSqliteAvailable } from '../client';
import { sessions, session_turns } from '../schema';
import type {
  ConversationTurn,
  EngineSnapshot,
} from '@/state-machine/types';

export interface StoredSession {
  id: string;
  lesson_id: number;
  status: 'active' | 'completed' | 'abandoned';
  snapshot: EngineSnapshot;
  started_at: Date;
  ended_at: Date | null;
}

const WEB_PREFIX = 'aitutor.offline-session.';

function webStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

interface WebPayload {
  session: {
    id: string;
    lesson_id: number;
    status: StoredSession['status'];
    snapshot: EngineSnapshot;
    started_at: string;
    ended_at: string | null;
  };
  turns: ConversationTurn[];
}

function key(lessonId: number) {
  return `${WEB_PREFIX}${lessonId}`;
}

/**
 * Identify the current local session for a lesson — there's at most
 * one active offline session per lesson per device. If none exists,
 * create one.
 */
export function offlineSessionId(lessonId: number, studentId: number): string {
  // Stable id so we always reuse the same row when resuming.
  return `local-${studentId}-${lessonId}`;
}

export async function saveSession(args: {
  id: string;
  lessonId: number;
  studentId: number;
  packVersion: number;
  status: StoredSession['status'];
  snapshot: EngineSnapshot;
  startedAt: Date;
  endedAt: Date | null;
}): Promise<void> {
  if (!isSqliteAvailable()) {
    const storage = webStorage();
    if (!storage) return;
    const existing = await loadSession(args.lessonId);
    const turns = existing ? await loadTurns(args.id) : [];
    const payload: WebPayload = {
      session: {
        id: args.id,
        lesson_id: args.lessonId,
        status: args.status,
        snapshot: args.snapshot,
        started_at: args.startedAt.toISOString(),
        ended_at: args.endedAt ? args.endedAt.toISOString() : null,
      },
      turns,
    };
    storage.setItem(key(args.lessonId), JSON.stringify(payload));
    return;
  }

  const db = getDb();
  const row = {
    id: args.id,
    lesson_id: args.lessonId,
    student_id: args.studentId,
    pack_version: args.packVersion,
    status: args.status,
    engine_state_json: args.snapshot,
    started_at: args.startedAt,
    ended_at: args.endedAt,
  };
  await db
    .insert(sessions)
    .values(row)
    .onConflictDoUpdate({ target: sessions.id, set: row });
}

export async function loadSession(lessonId: number): Promise<StoredSession | null> {
  if (!isSqliteAvailable()) {
    const storage = webStorage();
    if (!storage) return null;
    const raw = storage.getItem(key(lessonId));
    if (!raw) return null;
    try {
      const p = JSON.parse(raw) as WebPayload;
      return {
        id: p.session.id,
        lesson_id: p.session.lesson_id,
        status: p.session.status,
        snapshot: p.session.snapshot,
        started_at: new Date(p.session.started_at),
        ended_at: p.session.ended_at ? new Date(p.session.ended_at) : null,
      };
    } catch {
      return null;
    }
  }

  const db = getDb();
  const rows = await db
    .select()
    .from(sessions)
    .where(eq(sessions.lesson_id, lessonId))
    .limit(1);
  const row = rows[0];
  if (!row) return null;
  return {
    id: row.id,
    lesson_id: row.lesson_id,
    status: row.status as StoredSession['status'],
    snapshot: row.engine_state_json as EngineSnapshot,
    started_at: row.started_at ?? new Date(),
    ended_at: row.ended_at ?? null,
  };
}

export async function appendTurn(
  sessionId: string,
  turn: ConversationTurn,
): Promise<void> {
  if (!isSqliteAvailable()) {
    const storage = webStorage();
    if (!storage) return;
    // Find which lesson this session belongs to by scanning prefixes.
    for (let i = 0; i < storage.length; i += 1) {
      const k = storage.key(i);
      if (!k || !k.startsWith(WEB_PREFIX)) continue;
      const raw = storage.getItem(k);
      if (!raw) continue;
      try {
        const p = JSON.parse(raw) as WebPayload;
        if (p.session.id === sessionId) {
          p.turns.push(turn);
          storage.setItem(k, JSON.stringify(p));
          return;
        }
      } catch {
        // skip
      }
    }
    return;
  }

  const db = getDb();
  await db.insert(session_turns).values({
    id: turn.client_uuid,
    session_id: sessionId,
    role: turn.role,
    content: turn.content,
    step_id: turn.step_index,
    metadata_json: turn.media ? { media: turn.media } : null,
    generated_offline: turn.generated_offline,
    offline_model_id: turn.offline_model_id ?? null,
    client_generated_at: new Date(turn.client_generated_at),
    synced: false,
  });
}

export async function loadTurns(sessionId: string): Promise<ConversationTurn[]> {
  if (!isSqliteAvailable()) {
    const storage = webStorage();
    if (!storage) return [];
    for (let i = 0; i < storage.length; i += 1) {
      const k = storage.key(i);
      if (!k || !k.startsWith(WEB_PREFIX)) continue;
      const raw = storage.getItem(k);
      if (!raw) continue;
      try {
        const p = JSON.parse(raw) as WebPayload;
        if (p.session.id === sessionId) return p.turns;
      } catch {
        // skip
      }
    }
    return [];
  }

  const db = getDb();
  const rows = await db
    .select()
    .from(session_turns)
    .where(eq(session_turns.session_id, sessionId));
  return rows.map((r) => ({
    role: r.role as ConversationTurn['role'],
    content: r.content,
    client_uuid: r.id,
    client_generated_at: (r.client_generated_at ?? new Date()).toISOString(),
    step_index: r.step_id ?? 0,
    generated_offline: !!r.generated_offline,
    offline_model_id: r.offline_model_id ?? undefined,
    media: ((r.metadata_json as { media?: ConversationTurn['media'] } | null)?.media) ?? null,
  }));
}

export async function deleteSession(lessonId: number, sessionId: string): Promise<void> {
  if (!isSqliteAvailable()) {
    const storage = webStorage();
    if (!storage) return;
    storage.removeItem(key(lessonId));
    return;
  }
  const db = getDb();
  await db.delete(session_turns).where(eq(session_turns.session_id, sessionId));
  await db.delete(sessions).where(eq(sessions.id, sessionId));
}
