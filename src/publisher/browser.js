'use strict';

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const SESSION_DIR = process.env.SESSION_DIR || path.join(__dirname, '../../sessions');
const HEADLESS = process.env.HEADLESS !== 'false';

/**
 * Launch a Chromium browser and context for a given userId.
 * Loads their saved TikTok session if one exists.
 */
async function getBrowser(userId) {
  const sessionPath = _sessionPath(userId);
  const storageState = fs.existsSync(sessionPath) ? sessionPath : undefined;

  const browser = await chromium.launch({
    headless: HEADLESS,
    args: [
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--disable-blink-features=AutomationControlled',
    ],
  });

  const context = await browser.newContext({
    storageState,
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      + '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    viewport: { width: 1280, height: 800 },
    locale: 'en-US',
    timezoneId: 'America/New_York',
    // Avoid WebDriver detection
    extraHTTPHeaders: { 'Accept-Language': 'en-US,en;q=0.9' },
  });

  // Mask webdriver property
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });

  return { browser, context };
}

/**
 * Save the current browser context session to disk.
 * Returns the path the session was saved to.
 */
async function saveSession(context, userId) {
  const dir = path.join(SESSION_DIR, userId);
  fs.mkdirSync(dir, { recursive: true });
  const dest = _sessionPath(userId);
  await context.storageState({ path: dest });
  return dest;
}

/**
 * Returns true if a session file exists for userId.
 */
function sessionExists(userId) {
  return fs.existsSync(_sessionPath(userId));
}

function _sessionPath(userId) {
  return path.join(SESSION_DIR, userId, 'tiktok.json');
}

module.exports = { getBrowser, saveSession, sessionExists };
