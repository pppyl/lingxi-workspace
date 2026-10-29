import { chromium } from 'playwright-core';
import readline from 'node:readline';
import path from 'node:path';

// One owned browser profile. No access to the user's ordinary Chrome profile.
const origin = 'https://wx.10086.cn';
const entry = `${origin}/hlwyxhdhub/act-wedrecharge/1024101716`;
const endpoints = new Set(['_pub/rec_login', 'queryActivityInfo', 'queryDrawPrizeInfo', 'getPrizeList', 'drawPrize']);
let context = null;
let page = null;
let closing = false;
let browserVisible = false;
let ownedBrowser = null;
const browserOptions = process.env.CMCC_BROWSER_EXECUTABLE
  ? {executablePath:process.env.CMCC_BROWSER_EXECUTABLE}
  : {channel:process.env.CMCC_BROWSER_CHANNEL || 'chrome'};

async function closeContext() {
  if (context) await context.close();
  if (ownedBrowser) await ownedBrowser.close();
  ownedBrowser = null;
}

async function appSession({cookies, user_agent}) {
  if (!Array.isArray(cookies) || cookies.some(c => !['wx.10086.cn', '.wx.10086.cn', '.10086.cn', '10086.cn'].includes(c.domain))) {
    throw new Error('APP 活动 Cookie 域名无效');
  }
  await closeContext();
  ownedBrowser = await chromium.launch({...browserOptions, headless: true});
  context = await ownedBrowser.newContext({userAgent: user_agent, viewport: {width: 460, height: 900},
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai', acceptDownloads: false});
  browserVisible = false;
  context.on('close', () => { context = null; page = null; });
  // Ephemeral context: authenticated activity cookies never reach a profile file.
  await context.addCookies(cookies);
  page = await context.newPage();
  await page.goto(`${origin}/hlwyxhdhub/act-wedrecharge/index.html`, {waitUntil: 'domcontentloaded', timeout: 25000});
  return {open: true};
}

function validateEntry(value) {
  const url = new URL(value || entry);
  if (url.origin !== origin || url.username || url.password ||
      !['/hlwyxhdhub/act-wedrecharge/1024101716', '/hlwyxhdhub/act-wedrecharge/index.html'].includes(url.pathname)) {
    throw new Error('仅支持此活动的中国移动官方链接');
  }
  return url;
}

async function openBrowser({ url, visible = true, login = true }) {
  if (context && visible && !browserVisible) await context.close();
  if (!context) {
    context = await chromium.launchPersistentContext(path.resolve(process.env.CMCC_PROFILE_DIR || '.local/browser-profile'), {
      ...browserOptions,
      headless: !visible,
      viewport: { width: 460, height: 900 },
      locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
      acceptDownloads: false,
    });
    browserVisible = visible;
    context.on('close', () => { context = null; page = null; });
  }
  page = context.pages().find(p => !p.isClosed()) || await context.newPage();
  const dest = validateEntry(url);
  if (login) dest.searchParams.set('prelogin', 'false');
  try {
    await page.goto(dest.href, { waitUntil: 'domcontentloaded', timeout: 25000 });
  } catch (error) {
    // A navigation replaced by an SSO redirect is normal. Other failures are errors.
    if (!String(error.message).includes('ERR_ABORTED')) throw error;
  }
  if (visible) await page.bringToFront();
  return { open: true, page: new URL(page.url()).pathname };
}

function currentPage() {
  if (!context) throw new Error('官方登录窗口尚未打开，或已被关闭');
  page = context.pages().find(p => !p.isClosed() && p.url().startsWith(origin)) || page;
  if (!page || page.isClosed() || new URL(page.url()).origin !== origin) {
    throw new Error('请在官方窗口完成登录并返回活动页面');
  }
  return page;
}

async function callApi({ endpoint, body = {} }) {
  if (!endpoints.has(endpoint)) throw new Error('不支持的活动接口');
  const active = currentPage();
  return active.evaluate(async ({ endpoint, body }) => {
    const method = endpoint === '_pub/rec_login' ? 'GET' : 'POST';
    const response = await fetch(`/hlwyxhdhub/api/wedrecharge/${endpoint}`, {
      method, credentials: 'include', redirect: 'error',
      headers: { 'Content-Type': 'application/json;charset=UTF-8', 'login-check': '1' },
      ...(method === 'POST' ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error(`官方接口 HTTP ${response.status}`);
    const result = await response.json();
    if (!result || typeof result !== 'object') throw new Error('官方接口未返回 JSON 对象');
    return result;
  }, { endpoint, body });
}

async function command(data) {
  switch (data.command) {
    case 'open': return openBrowser(data);
    case 'info': return { open: !!context, page: page && !page.isClosed() ? new URL(page.url()).pathname : null };
    case 'app_session': return appSession(data);
    case 'risk': {
      const active = currentPage();
      await active.waitForFunction(() => typeof window.__constId === 'string' && window.__constId.length > 0, null, {timeout: 10000}).catch(() => {});
      return {const_id: await active.evaluate(() => typeof window.__constId === 'string' ? window.__constId : '')};
    }
    case 'api': return callApi(data);
    case 'logout':
      if (context) {
        await context.clearCookies();
        for (const p of context.pages()) {
          if (p.url().startsWith(origin)) await p.evaluate(() => { localStorage.clear(); sessionStorage.clear(); }).catch(() => {});
        }
        await closeContext();
      }
      return { open: false };
    case 'close':
      closing = true;
      await closeContext();
      return { open: false };
    default: throw new Error('不支持的浏览器指令');
  }
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  let message;
  try {
    message = JSON.parse(line);
    const result = await command(message);
    process.stdout.write(JSON.stringify({ id: message.id, ok: true, result }) + '\n');
  } catch (error) {
    // Never include command parameters, Cookies or token-bearing URLs in diagnostics.
    let safe = String(error.message).replace(/https?:\/\/[^\s"')]+/g, '[官方页面]');
    const code = /HTTP (403|429)/.test(safe) ? 'HTTP_ACCESS_LIMIT' : 'BROWSER_ERROR';
    process.stdout.write(JSON.stringify({ id: message?.id, ok: false, code, error: safe.slice(0, 400) }) + '\n');
  }
  if (closing) break;
}
await closeContext();
