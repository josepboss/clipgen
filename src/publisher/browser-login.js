#!/usr/bin/env node
/**
 * browser-login.js  —  Interactive TikTok login via screenshot relay.
 *
 * Usage:
 *   node browser-login.js <userId> <screenshotPath> <commandsPath>
 *
 * Environment:
 *   SESSION_DIR          — path to sessions directory
 *   LOGIN_TIMEOUT_SEC    — max seconds to wait for login (default 180)
 *   DISPLAY              — X display (set by xvfb-run / Xvfb)
 *
 * Behaviour:
 *   1. Opens Chromium (headless:false if DISPLAY is set, else headless:true)
 *   2. Navigates to tiktok.com/login
 *   3. Every SCREENSHOT_MS: takes a JPEG screenshot → saves to screenshotPath
 *   4. Every COMMAND_POLL_MS: reads commandsPath (JSON array), executes & clears it
 *   5. Polls page URL; when login detected → saves session → exits 0
 *
 * stdout: newline-delimited JSON events { status, message[, sessionPath] }
 * Exit codes: 0 success, 1 error/timeout, 2 usage
 */

'use strict';

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [, , userId, screenshotPath, commandsPath] = process.argv;

if (!userId || !screenshotPath || !commandsPath) {
  out({ status: 'error', message: 'Usage: node browser-login.js <userId> <screenshotPath> <commandsPath>' });
  process.exit(2);
}

const SESSION_DIR = process.env.SESSION_DIR || path.join(__dirname, '../../sessions');
const TIMEOUT_SEC = parseInt(process.env.LOGIN_TIMEOUT_SEC || '180', 10);
const SCREENSHOT_MS = 2000;
const COMMAND_POLL_MS = 400;
const HEADLESS = !process.env.DISPLAY; // headless when no X display available

const SUCCESS_PATTERNS = [
  /tiktok\.com\/?($|\?)/,
  /tiktok\.com\/(foryou|following|explore|upload|creator-center)/,
  /tiktok\.com\/@/,
];

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

async function drainCommands(page, cmdPath) {
  if (!fs.existsSync(cmdPath)) return;
  let cmds;
  try {
    const raw = fs.readFileSync(cmdPath, 'utf8').trim();
    cmds = raw ? JSON.parse(raw) : [];
    if (!cmds.length) return;
    fs.writeFileSync(cmdPath, '[]');
  } catch (_) {
    return;
  }
  for (const cmd of cmds) {
    try {
      if (cmd.type === 'click') {
        await page.mouse.click(cmd.x, cmd.y);
      } else if (cmd.type === 'type') {
        await page.keyboard.type(cmd.text, { delay: 30 });
      } else if (cmd.type === 'key') {
        await page.keyboard.press(cmd.key);
      } else if (cmd.type === 'scroll') {
        await page.mouse.wheel(0, cmd.delta);
      }
    } catch (_) {}
  }
}

(async () => {
  // Ensure directories exist
  fs.mkdirSync(path.dirname(screenshotPath), { recursive: true });
  fs.mkdirSync(path.dirname(commandsPath), { recursive: true });
  fs.writeFileSync(commandsPath, '[]');

  out({ status: 'opening', message: 'Launching browser…' });

  let browser;
  let screenshotInterval;
  let commandInterval;

  try {
    browser = await chromium.launch({
      headless: HEADLESS,
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-blink-features=AutomationControlled',
        '--window-size=1280,800',
        '--disable-extensions',
      ],
    });

    const context = await browser.newContext({
      userAgent:
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        + '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
      viewport: { width: 1280, height: 800 },
      locale: 'en-US',
      timezoneId: 'America/New_York',
      extraHTTPHeaders: { 'Accept-Language': 'en-US,en;q=0.9' },
    });

    await context.addInitScript(() => {
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
      Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    });

    const page = await context.newPage();
    await page.goto('https://www.tiktok.com/login', {
      waitUntil: 'domcontentloaded',
      timeout: 30_000,
    });

    out({ status: 'waiting', message: 'TikTok login page is open. Log in to continue.' });

    // Take screenshot immediately
    try {
      await page.screenshot({ path: screenshotPath, type: 'jpeg', quality: 75 });
      out({ status: 'screenshot' });
    } catch (_) {}

    // Periodic screenshot capture
    screenshotInterval = setInterval(async () => {
      try {
        await page.screenshot({ path: screenshotPath, type: 'jpeg', quality: 75 });
        out({ status: 'screenshot' });
      } catch (_) {}
    }, SCREENSHOT_MS);

    // Periodic command drain
    commandInterval = setInterval(() => drainCommands(page, commandsPath), COMMAND_POLL_MS);

    // Poll for login success
    const deadline = Date.now() + TIMEOUT_SEC * 1000;
    let loggedIn = false;

    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 1500));
      const url = page.url();
      if (!url.includes('/login') && SUCCESS_PATTERNS.some(p => p.test(url))) {
        loggedIn = true;
        break;
      }
    }

    clearInterval(screenshotInterval);
    clearInterval(commandInterval);

    if (!loggedIn) {
      out({ status: 'error', message: `Login timed out after ${TIMEOUT_SEC}s` });
      process.exit(1);
    }

    // One final screenshot after login
    try {
      await page.screenshot({ path: screenshotPath, type: 'jpeg', quality: 80 });
    } catch (_) {}

    // Save session
    const sessionDir = path.join(SESSION_DIR, userId);
    fs.mkdirSync(sessionDir, { recursive: true });
    const sessionPath = path.join(sessionDir, 'tiktok.json');
    await context.storageState({ path: sessionPath });

    out({ status: 'success', message: 'Session saved! TikTok is now connected.', sessionPath });
    process.exit(0);

  } catch (err) {
    clearInterval(screenshotInterval);
    clearInterval(commandInterval);
    out({ status: 'error', message: err.message });
    process.exit(1);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
})();
