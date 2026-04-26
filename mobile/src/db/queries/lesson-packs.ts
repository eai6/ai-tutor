import { eq } from 'drizzle-orm';

import { getDb, isSqliteAvailable } from '../client';
import { lesson_packs, lessons } from '../schema';
import type { OfflinePackResponse } from '@/api/offline-pack';

export interface StoredPack {
  lesson_id: number;
  version: number;
  policy: OfflinePackResponse['policy'];
  steps: OfflinePackResponse['content']['steps'];
  exit_ticket: OfflinePackResponse['content']['exit_ticket'];
  media_manifest: string[];
  downloaded_at: Date;
}

const WEB_STORAGE_PREFIX = 'aitutor.pack.';

function webStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function packKey(lessonId: number) {
  return `${WEB_STORAGE_PREFIX}${lessonId}`;
}

interface SerializedPack extends Omit<StoredPack, 'downloaded_at'> {
  downloaded_at: string;
}

export async function savePack(pack: OfflinePackResponse): Promise<void> {
  const now = new Date();

  if (!isSqliteAvailable()) {
    const storage = webStorage();
    if (!storage) return;
    const value: SerializedPack = {
      lesson_id: pack.lesson_id,
      version: pack.pack_version,
      policy: pack.policy,
      steps: pack.content.steps,
      exit_ticket: pack.content.exit_ticket,
      media_manifest: pack.media_manifest ?? [],
      downloaded_at: now.toISOString(),
    };
    storage.setItem(packKey(pack.lesson_id), JSON.stringify(value));
    return;
  }

  const db = getDb();
  await db
    .insert(lesson_packs)
    .values({
      lesson_id: pack.lesson_id,
      version: pack.pack_version,
      policy_json: pack.policy,
      steps_json: pack.content.steps,
      exit_ticket_json: pack.content.exit_ticket,
      media_manifest_json: pack.media_manifest,
      downloaded_at: now,
    })
    .onConflictDoUpdate({
      target: lesson_packs.lesson_id,
      set: {
        version: pack.pack_version,
        policy_json: pack.policy,
        steps_json: pack.content.steps,
        exit_ticket_json: pack.content.exit_ticket,
        media_manifest_json: pack.media_manifest,
        downloaded_at: now,
      },
    });

  const lesson = pack.content.lesson;
  await db
    .insert(lessons)
    .values({
      id: lesson.id,
      unit_id: lesson.unit_id,
      course_id: lesson.course_id,
      title: lesson.title,
      objective: lesson.objective ?? null,
      order_index: lesson.order_index,
      pack_downloaded: true,
      pack_version: pack.pack_version,
      pack_downloaded_at: now,
    })
    .onConflictDoUpdate({
      target: lessons.id,
      set: {
        unit_id: lesson.unit_id,
        course_id: lesson.course_id,
        title: lesson.title,
        objective: lesson.objective ?? null,
        order_index: lesson.order_index,
        pack_downloaded: true,
        pack_version: pack.pack_version,
        pack_downloaded_at: now,
      },
    });
}

export async function loadPack(lessonId: number): Promise<StoredPack | null> {
  if (!isSqliteAvailable()) {
    const storage = webStorage();
    if (!storage) return null;
    const raw = storage.getItem(packKey(lessonId));
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw) as SerializedPack;
      return {
        ...parsed,
        downloaded_at: new Date(parsed.downloaded_at),
      };
    } catch {
      return null;
    }
  }

  const db = getDb();
  const rows = await db
    .select()
    .from(lesson_packs)
    .where(eq(lesson_packs.lesson_id, lessonId))
    .limit(1);
  const row = rows[0];
  if (!row) return null;
  return {
    lesson_id: row.lesson_id,
    version: row.version,
    policy: row.policy_json as OfflinePackResponse['policy'],
    steps: row.steps_json as OfflinePackResponse['content']['steps'],
    exit_ticket: row.exit_ticket_json as OfflinePackResponse['content']['exit_ticket'],
    media_manifest: (row.media_manifest_json as string[] | null) ?? [],
    downloaded_at: row.downloaded_at ?? new Date(),
  };
}

export async function deletePack(lessonId: number): Promise<void> {
  if (!isSqliteAvailable()) {
    const storage = webStorage();
    if (!storage) return;
    storage.removeItem(packKey(lessonId));
    return;
  }

  const db = getDb();
  await db.delete(lesson_packs).where(eq(lesson_packs.lesson_id, lessonId));
  await db
    .update(lessons)
    .set({ pack_downloaded: false, pack_version: null, pack_downloaded_at: null })
    .where(eq(lessons.id, lessonId));
}
