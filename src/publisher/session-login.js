#!/usr/bin/env node
/**
 * session-login.js  —  One-time TikTok login per user.
 *
 * Usage:
 *   HEADLESS=false node session-login.js <userId>
 *
 * Opens a Chromium browser window. The user logs in manually.
 * Once the TikTok home feed is detected (login confirmed), the
 * session is saved to sessions/<userId>/tiktok.json and the
 * script exits with code 0 and outputs JSON to stdout.
 *
 * Exit codes:
 *   0  — session saved successfully
 *   1  — timeout waiting for login (TIMEOUT_SEC)
 *   2  — usage error
 */

'use strict';

const { getBrowser, saveSession } = require('./browser');

const TIMEOUT_SEC = parseInt(process.env.LOGIN_TIMEOUT_SEC || '120', 10);
const LOGIN_URL = 'https://www.tiktok.com/login';
const SUCCESS_PATTERNS = [
  /tiktok\.com\/?$/,
  /tiktok\.com\/(foryou|following|explore|upload)/,
  /tiktok\.com\/@/,
];

(async () => {
  const userId = process.argv[2];
  if (!userId) {
    console.error(JSON.stringify({ ok: false, error: 'Usage: node session-login.js <userId>' }));
    process.exit(2);
  }

  let browser, context;
  try {
    ({ browser, context } = await getBrowser(userId));
    const page = await context.newPage();

    await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    console.error(`[login] Browser open for user "${userId}". Please log in within ${TIMEOUT_SEC}s.`);

    // Poll until a success URL pattern is detected or timeout
    const deadline = Date.now() + TIMEOUT_SEC * 1000;
    let loggedIn = false;

    while (Date.now() < deadline) {
      const url = page.url();
      if (SUCCESS_PATTERNS.some((p) => p.test(url))) {
        loggedIn = true;
        break;
      }
      await page.waitForTimeout(1500);
    }

    if (!loggedIn) {
      console.log(JSON.stringify({ ok: false, error: `Timed out after ${TIMEOUT_SEC}s waiting for login` }));
      process.exit(1);
    }

    // Give the page a moment to settle cookies
    await page.waitForTimeout(2000);

    const sessionPath = await saveSession(context, userId);
    console.log(JSON.stringify({ ok: true, userId, sessionPath }));
    process.exit(0);
  } catch (err) {
    console.log(JSON.stringify({ ok: false, error: err.message }));
    process.exit(1);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
})();
