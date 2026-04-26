import { sqliteTable, text, integer, real } from 'drizzle-orm/sqlite-core';

export const courses = sqliteTable('courses', {
  id: integer('id').primaryKey(),
  title: text('title').notNull(),
  description: text('description'),
  subject: text('subject'),
  grade_level: text('grade_level').notNull(),
  institution_id: integer('institution_id'),
  synced_at: integer('synced_at', { mode: 'timestamp' }),
});

export const lessons = sqliteTable('lessons', {
  id: integer('id').primaryKey(),
  unit_id: integer('unit_id').notNull(),
  course_id: integer('course_id').notNull(),
  title: text('title').notNull(),
  objective: text('objective'),
  order_index: integer('order_index'),
  pack_downloaded: integer('pack_downloaded', { mode: 'boolean' }).default(false),
  pack_version: integer('pack_version'),
  pack_downloaded_at: integer('pack_downloaded_at', { mode: 'timestamp' }),
  synced_at: integer('synced_at', { mode: 'timestamp' }),
});

export const lesson_packs = sqliteTable('lesson_packs', {
  lesson_id: integer('lesson_id').primaryKey(),
  version: integer('version').notNull(),
  policy_json: text('policy_json', { mode: 'json' }).notNull(),
  steps_json: text('steps_json', { mode: 'json' }).notNull(),
  exit_ticket_json: text('exit_ticket_json', { mode: 'json' }),
  media_manifest_json: text('media_manifest_json', { mode: 'json' }),
  downloaded_at: integer('downloaded_at', { mode: 'timestamp' }).notNull(),
});

export const sessions = sqliteTable('sessions', {
  id: text('id').primaryKey(),
  server_id: integer('server_id'),
  lesson_id: integer('lesson_id').notNull(),
  student_id: integer('student_id').notNull(),
  pack_version: integer('pack_version').notNull(),
  status: text('status').notNull(),
  engine_state_json: text('engine_state_json', { mode: 'json' }),
  started_at: integer('started_at', { mode: 'timestamp' }).notNull(),
  ended_at: integer('ended_at', { mode: 'timestamp' }),
  last_synced_at: integer('last_synced_at', { mode: 'timestamp' }),
});

export const session_turns = sqliteTable('session_turns', {
  id: text('id').primaryKey(),
  session_id: text('session_id').notNull(),
  server_id: integer('server_id'),
  role: text('role').notNull(),
  content: text('content').notNull(),
  step_id: integer('step_id'),
  metadata_json: text('metadata_json', { mode: 'json' }),
  generated_offline: integer('generated_offline', { mode: 'boolean' }).default(false),
  offline_model_id: text('offline_model_id'),
  client_generated_at: integer('client_generated_at', { mode: 'timestamp' }).notNull(),
  synced: integer('synced', { mode: 'boolean' }).default(false),
});

export const sync_queue = sqliteTable('sync_queue', {
  id: text('id').primaryKey(),
  kind: text('kind').notNull(),
  payload_json: text('payload_json', { mode: 'json' }).notNull(),
  session_id: text('session_id'),
  created_at: integer('created_at', { mode: 'timestamp' }).notNull(),
  attempt_count: integer('attempt_count').default(0),
  last_attempt_at: integer('last_attempt_at', { mode: 'timestamp' }),
  last_error: text('last_error'),
});

export const progress = sqliteTable('progress', {
  lesson_id: integer('lesson_id').primaryKey(),
  mastery_level: text('mastery_level').notNull(),
  best_score: real('best_score'),
  attempts_count: integer('attempts_count').default(0),
  last_session_at: integer('last_session_at', { mode: 'timestamp' }),
});

export const active_model = sqliteTable('active_model', {
  id: integer('id').primaryKey(),
  model_id: text('model_id').notNull(),
  set_at: integer('set_at', { mode: 'timestamp' }).notNull(),
});
