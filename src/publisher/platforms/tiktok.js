#!/usr/bin/env node
/**
 * tiktok.js  —  Upload a video to TikTok.
 *
 * Usage:
 *   node tiktok.js <userId> <videoPath> <caption> [queueItemId]
 *
 * Reads the session from sessions/<userId>/tiktok.json.
 * Outputs a single JSON line to stdout and exits.
 *
 * Exit codes:
 *   0  — posted successfully
 *   1  — posting failed (see JSON error field)
 *   3  — session expired / not logged in
 *   2  — usage / file-not-found error
 */

'use strict';

const path = require('path');
const fs = require('fs');
const { getBrowser, sessionExists } = require('../browser');

const UPLOAD_URL = 'https://www.tiktok.com/creator-center/upload';
const LOGIN_PATTERN = /tiktok\.com\/(login|signup)/i;
const UPLOAD_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes for upload + processing

(async () => {
  const [, , userId, videoPath, caption, queueItemId] = process.argv;

  if (!userId || !videoPath || !caption) {
    out({ ok: false, error: 'Usage: node tiktok.js <userId> <videoPath> <caption> [queueItemId]' });
    process.exit(2);
  }

  if (!fs.existsSync(videoPath)) {
    out({ ok: false, error: `Video file not found: ${videoPath}` });
    process.exit(2);
  }

  if (!sessionExists(userId)) {
    out({ ok: false, error: 'No session found. User must log in first.', sessionExpired: true });
    process.exit(3);
  }

  let browser;
  try {
    const { browser: b, context } = await getBrowser(userId);
    browser = b;
    const page = await context.newPage();

    // Navigate to creator upload page
    await page.goto(UPLOAD_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    // Detect session expiry
    if (LOGIN_PATTERN.test(page.url())) {
      out({ ok: false, error: 'TikTok session expired. User must re-login.', sessionExpired: true });
      process.exit(3);
    }

    // TikTok embeds the upload UI in an iframe
    await page.waitForSelector('iframe', { timeout: 20_000 });
    const iframeEl = await page.$('iframe');
    const frame = await iframeEl.contentFrame();

    // Upload the video file via the hidden file input
    const fileInput = await frame.waitForSelector('input[type="file"]', { timeout: 20_000 });
    await fileInput.setInputFiles(videoPath);

    // Wait for the video to finish processing (progress bar disappears)
    await frame.waitForSelector(
      '[class*="upload-progress"], [class*="processing"]',
      { state: 'detached', timeout: UPLOAD_TIMEOUT_MS }
    ).catch(() => {}); // not all UI versions show this, so ignore timeout

    // Fill in the caption / description field
    const captionSel = [
      '[data-text="true"]',
      '[contenteditable="true"]',
      'div[class*="caption"] [contenteditable]',
      'textarea[placeholder*="caption"]',
      'textarea[placeholder*="describe"]',
    ].join(', ');

    const captionEl = await frame.waitForSelector(captionSel, { timeout: 30_000 });
    await captionEl.click({ clickCount: 3 }); // select all
    await captionEl.fill('');                  // clear
    await captionEl.type(caption, { delay: 30 });

    // Set privacy to "Everyone" (Public) — find the radio/select
    const publicSel = [
      'label:has-text("Everyone")',
      'div[class*="privacy"] label:has-text("Everyone")',
      '[class*="radio"]:has-text("Everyone")',
      'input[value="public"] + label',
    ].join(', ');

    const publicOpt = await frame.$(publicSel);
    if (publicOpt) {
      await publicOpt.click().catch(() => {});
    }

    // Click the Post button
    const postBtnSel = [
      'button:has-text("Post")',
      'button[class*="post"]:not([disabled])',
      'div[class*="btn-post"]',
    ].join(', ');

    const postBtn = await frame.waitForSelector(postBtnSel, { timeout: 15_000 });
    await postBtn.click();

    // Confirm success — look for redirect or success banner
    const successDetected = await Promise.race([
      page.waitForNavigation({ timeout: 30_000 }).then(() => true).catch(() => false),
      frame.waitForSelector(
        '[class*="success"], [class*="uploaded"], [class*="publish-success"]',
        { timeout: 30_000 }
      ).then(() => true).catch(() => false),
    ]);

    if (!successDetected) {
      out({ ok: false, error: 'Post button clicked but success not detected — check account manually.' });
      process.exit(1);
    }

    out({ ok: true, userId, queueItemId: queueItemId || null, message: 'Video posted to TikTok' });
    process.exit(0);
  } catch (err) {
    const sessionExpired = /login|session|expired|unauthorized/i.test(err.message);
    out({ ok: false, error: err.message, sessionExpired });
    process.exit(sessionExpired ? 3 : 1);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
})();

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}
