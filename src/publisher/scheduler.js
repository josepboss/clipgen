#!/usr/bin/env node
/**
 * scheduler.js  —  Standalone Node.js queue processor.
 *
 * This is an OPTIONAL alternative to the Python scheduler.
 * The primary scheduler is publisher_scheduler.py (Python).
 * Run this only if you prefer a pure-Node.js queue processor.
 *
 * Usage:
 *   node scheduler.js
 *
 * Reads from the SQLite DB at PUBLISHER_DB_PATH, picks up
 * "pending" items whose scheduled_time <= now, and posts them.
 */

'use strict';

// Only runs if sqlite3 is available; otherwise logs a warning.
let Database;
try {
  Database = require('better-sqlite3');
} catch (_) {
  console.error('[scheduler] better-sqlite3 not installed. Use the Python scheduler instead.');
  process.exit(0);
}

const { execFileSync } = require('child_process');
const path = require('path');

const DB_PATH = process.env.PUBLISHER_DB_PATH || path.join(__dirname, '../../publisher.db');
const INTERVAL_MS = parseInt(process.env.PUBLISH_INTERVAL_MINUTES || '30', 10) * 60 * 1000;
const TIKTOK_SCRIPT = path.join(__dirname, 'platforms/tiktok.js');

function processQueue() {
  const db = new Database(DB_PATH);
  const now = new Date().toISOString();

  const items = db
    .prepare(
      `SELECT * FROM publish_queue
       WHERE platform = 'tiktok'
         AND status = 'pending'
         AND (scheduled_time IS NULL OR scheduled_time <= ?)
       ORDER BY created_at ASC`
    )
    .all(now);

  for (const item of items) {
    console.log(`[scheduler] Processing queue item ${item.id} for user ${item.user_id}`);
    db.prepare(`UPDATE publish_queue SET status='running', updated_at=? WHERE id=?`)
      .run(new Date().toISOString(), item.id);

    try {
      const result = execFileSync(
        'node',
        [TIKTOK_SCRIPT, item.user_id, item.video_path, item.caption, String(item.id)],
        { encoding: 'utf8', timeout: 6 * 60 * 1000 }
      );
      const parsed = JSON.parse(result.trim().split('\n').pop());

      if (parsed.ok) {
        db.prepare(`UPDATE publish_queue SET status='done', updated_at=? WHERE id=?`)
          .run(new Date().toISOString(), item.id);
        console.log(`[scheduler] Item ${item.id} posted successfully`);
      } else if (parsed.sessionExpired) {
        db.prepare(`UPDATE publish_queue SET status='session_expired', error_msg=?, updated_at=? WHERE id=?`)
          .run(parsed.error, new Date().toISOString(), item.id);
        console.warn(`[scheduler] Item ${item.id}: session expired for user ${item.user_id}`);
      } else {
        db.prepare(`UPDATE publish_queue SET status='failed', error_msg=?, updated_at=? WHERE id=?`)
          .run(parsed.error, new Date().toISOString(), item.id);
        console.error(`[scheduler] Item ${item.id} failed: ${parsed.error}`);
      }
    } catch (err) {
      db.prepare(`UPDATE publish_queue SET status='failed', error_msg=?, updated_at=? WHERE id=?`)
        .run(err.message, new Date().toISOString(), item.id);
      console.error(`[scheduler] Item ${item.id} exception: ${err.message}`);
    }
  }

  db.close();
}

console.log(`[scheduler] Starting — checking every ${INTERVAL_MS / 60000} minutes`);
processQueue();
setInterval(processQueue, INTERVAL_MS);
