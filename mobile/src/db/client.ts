import { Platform } from 'react-native';
import * as SQLite from 'expo-sqlite';
import { drizzle, type ExpoSQLiteDatabase } from 'drizzle-orm/expo-sqlite';

import * as schema from './schema';

const DB_NAME = 'aitutor.db';

let _db: ExpoSQLiteDatabase<typeof schema> | null = null;
let _sqlite: SQLite.SQLiteDatabase | null = null;
let _schemaReady = false;

export function isSqliteAvailable(): boolean {
  // expo-sqlite's web build requires SharedArrayBuffer (cross-origin
  // isolation), which Chrome only exposes when the page is served with
  // COOP/COEP headers. The Expo dev server doesn't always satisfy that
  // out of the box, so we fall back to a localStorage-backed shim on
  // web for now (see src/db/queries/lesson-packs.ts).
  return Platform.OS !== 'web';
}

const SCHEMA_SQL = [
  `CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    subject TEXT,
    grade_level TEXT NOT NULL,
    institution_id INTEGER,
    synced_at INTEGER
  );`,
  `CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    objective TEXT,
    order_index INTEGER,
    pack_downloaded INTEGER DEFAULT 0,
    pack_version INTEGER,
    pack_downloaded_at INTEGER,
    synced_at INTEGER
  );`,
  `CREATE TABLE IF NOT EXISTS lesson_packs (
    lesson_id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    exit_ticket_json TEXT,
    media_manifest_json TEXT,
    downloaded_at INTEGER NOT NULL
  );`,
  `CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    server_id INTEGER,
    lesson_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    pack_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    engine_state_json TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    last_synced_at INTEGER
  );`,
  `CREATE TABLE IF NOT EXISTS session_turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    server_id INTEGER,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    step_id INTEGER,
    metadata_json TEXT,
    generated_offline INTEGER DEFAULT 0,
    offline_model_id TEXT,
    client_generated_at INTEGER NOT NULL,
    synced INTEGER DEFAULT 0
  );`,
  `CREATE TABLE IF NOT EXISTS sync_queue (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    session_id TEXT,
    created_at INTEGER NOT NULL,
    attempt_count INTEGER DEFAULT 0,
    last_attempt_at INTEGER,
    last_error TEXT
  );`,
  `CREATE TABLE IF NOT EXISTS progress (
    lesson_id INTEGER PRIMARY KEY,
    mastery_level TEXT NOT NULL,
    best_score REAL,
    attempts_count INTEGER DEFAULT 0,
    last_session_at INTEGER
  );`,
  `CREATE TABLE IF NOT EXISTS active_model (
    id INTEGER PRIMARY KEY,
    model_id TEXT NOT NULL,
    set_at INTEGER NOT NULL
  );`,
];

function openSqlite(): SQLite.SQLiteDatabase {
  if (_sqlite) return _sqlite;
  _sqlite = SQLite.openDatabaseSync(DB_NAME);
  return _sqlite;
}

function bootstrapSchema(sqlite: SQLite.SQLiteDatabase) {
  if (_schemaReady) return;
  // Run statements one at a time so a syntax issue in one doesn't take
  // down the rest. Each is CREATE TABLE IF NOT EXISTS — idempotent.
  for (const sql of SCHEMA_SQL) {
    try {
      sqlite.execSync(sql);
    } catch (err) {
      // Surface in dev console — expo-sqlite Android wraps native
      // failures as NullPointerException, which is opaque without this.
      // eslint-disable-next-line no-console
      console.warn('[db] schema bootstrap failed for statement:', err);
    }
  }
  _schemaReady = true;
}

/**
 * Returns the Drizzle handle. Lazy-bootstraps the schema on first
 * call so callers don't have to wait for a separate ensureSchema().
 */
export function getDb(): ExpoSQLiteDatabase<typeof schema> {
  if (!isSqliteAvailable()) {
    throw new Error('SQLite is not available on this platform — use the web shim.');
  }
  const sqlite = openSqlite();
  bootstrapSchema(sqlite);
  if (_db) return _db;
  _db = drizzle(sqlite, { schema });
  return _db;
}

/**
 * Eagerly run schema bootstrap. Safe to call multiple times — it
 * no-ops after the first success. Kept for backwards-compat with
 * the root layout's effect.
 */
export function ensureSchema() {
  if (!isSqliteAvailable()) return;
  bootstrapSchema(openSqlite());
}
