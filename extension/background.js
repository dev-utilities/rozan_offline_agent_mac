// Rozan Bridge. Connects to the agent on this Mac through Chrome native messaging and runs
// a small, fixed set of commands. Nothing here talks to the internet.
'use strict';

const HOST = 'com.personal_agent.bridge';
let port = null;

function connect() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    port = null;
    return;
  }
  port.onMessage.addListener(onMessage);
  // Reading lastError marks it handled; otherwise Chrome lists "Native host has exited" under Errors every time
  // Rozan is closed or restarts. The alarm below simply tries again later.
  port.onDisconnect.addListener(() => { void chrome.runtime.lastError; port = null; });
  identity().then((who) => port && port.postMessage(who)).catch(() => {});
}

// Each browser profile running this extension has its own instance id, so the agent can tell
// "work Chrome" from "personal Chrome" or Brave. Kept in this profile's extension storage.
async function identity() {
  let { instanceId } = await chrome.storage.local.get('instanceId');
  if (!instanceId) {
    instanceId = crypto.randomUUID();
    await chrome.storage.local.set({ instanceId });
  }
  const brands = (navigator.userAgentData && navigator.userAgentData.brands) || [];
  const known = ['Microsoft Edge', 'Brave', 'Opera', 'Vivaldi', 'Arc', 'Google Chrome', 'Chromium'];
  const brand = known.find((k) => brands.some((b) => b.brand === k)) || 'Chromium browser';
  let incognito = false;
  try { incognito = await chrome.extension.isAllowedIncognitoAccess(); } catch (e) { /* older API */ }
  return { hello: chrome.runtime.getManifest().version, instance: instanceId, browser: brand, incognito };
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create('reconnect', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'reconnect') connect(); });
connect();

async function onMessage(msg) {
  if (!msg || typeof msg.id === 'undefined') return;
  const handler = HANDLERS[msg.cmd];
  try {
    if (!handler) throw new Error('unknown command ' + msg.cmd);
    const result = await handler(msg.args || {});
    port && port.postMessage({ id: msg.id, ok: true, result });
  } catch (e) {
    port && port.postMessage({ id: msg.id, ok: false, error: String((e && e.message) || e) });
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error('no active Chrome tab');
  return tab;
}

function waitLoaded(tabId, timeoutMs = 20000) {
  return new Promise((resolve) => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(t); resolve(); };
    const listener = (id, info) => { if (id === tabId && info.status === 'complete') done(); };
    const t = setTimeout(done, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function inPage(func, args = []) {
  const tab = await activeTab();
  const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
  if (res && res.result && res.result.__error) throw new Error(res.result.__error);
  return res ? res.result : null;
}

// ---- functions injected into the page (must be self-contained) ----------------------------------
function pageRead(maxChars) {
  return { title: document.title, url: location.href, text: (document.body ? document.body.innerText : '').slice(0, maxChars) };
}

// Find a visible control by its label: aria-label, tooltip, placeholder, title, or visible text.
// Marks the match with data-pa-target so a later click/type acts on exactly what was checked.
function pageResolve(target, kind, index) {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(target);
  const sel = kind === 'field'
    ? 'input:not([type=hidden]),textarea,[contenteditable=""],[contenteditable=true],[role=textbox],[role=combobox]'
    : 'a,button,input[type=submit],input[type=button],input[type=checkbox],[role=button],[role=link],[role=checkbox],[role=menuitem],[role=tab],[role=option],[aria-label],[data-tooltip]';
  const labelOf = (el) => {
    const lab = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    return el.getAttribute('aria-label') || el.getAttribute('data-tooltip') || el.getAttribute('placeholder') ||
      (lab && lab.innerText) || el.getAttribute('title') || el.getAttribute('name') ||
      (kind === 'field' ? '' : el.innerText) || el.value || '';
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const all = Array.from(document.querySelectorAll(sel)).filter(visible);
  let matches = all.filter((el) => norm(labelOf(el)) === want);
  if (!matches.length) matches = all.filter((el) => norm(labelOf(el)).startsWith(want));
  // keep the innermost element when a container and its child share a label
  matches = matches.filter((el) => !matches.some((o) => o !== el && el.contains(o)));
  document.querySelectorAll('[data-pa-target]').forEach((el) => el.removeAttribute('data-pa-target'));
  if (!matches.length) return { __error: 'no visible ' + (kind === 'field' ? 'field' : 'control') + ' labelled "' + target + '"' };
  if (matches.length > 1 && (index === null || index === undefined)) {
    return { __error: matches.length + ' controls are labelled "' + target + '"; the plan must say which one (index)' };
  }
  const el = matches[index || 0];
  if (!el) return { __error: 'index ' + index + ' is out of range (' + matches.length + ' matches)' };
  el.setAttribute('data-pa-target', '1');
  const r = el.getBoundingClientRect();
  return {
    tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', label: labelOf(el).slice(0, 120),
    href: el.href || el.closest('a')?.href || '', matches: matches.length,
    rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    page: location.href,
  };
}

// Lists visible controls by kind, using the same labels pageResolve matches on.
function pageControls(limit) {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const labelOf = (el, field) => {
    const lab = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    return norm(el.getAttribute('aria-label') || el.getAttribute('data-tooltip') || el.getAttribute('placeholder') ||
      (lab && lab.innerText) || el.getAttribute('title') || el.getAttribute('name') || (field ? '' : el.innerText) || el.value || '');
  };
  const kinds = {
    checkboxes: 'input[type=checkbox],[role=checkbox]',
    fields: 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]),textarea,[contenteditable=""],[contenteditable=true],[role=textbox],[role=combobox]',
    buttons: 'button,input[type=submit],input[type=button],[role=button],[role=menuitem],[role=tab]',
    links: 'a[href],[role=link]',
  };
  const out = {};
  const seen = new Set();
  for (const [kind, sel] of Object.entries(kinds)) {
    const labels = [];
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el) || !visible(el)) continue;
      seen.add(el);
      const l = labelOf(el, kind === 'fields').slice(0, 120);
      if (l && !labels.includes(l)) labels.push(l);
      if (labels.length >= limit) break;
    }
    out[kind] = labels;
  }
  out.url = location.href;
  return out;
}

function pageClick() {
  const el = document.querySelector('[data-pa-target]');
  if (!el) return { __error: 'the checked control is gone; nothing clicked' };
  el.removeAttribute('data-pa-target');
  el.scrollIntoView({ block: 'center' });
  el.click();
  return { clicked: true };
}

function pageType(text, submit) {
  const el = document.querySelector('[data-pa-target]');
  if (!el) return { __error: 'the checked field is gone; nothing typed' };
  el.removeAttribute('data-pa-target');
  el.focus();
  if (el.isContentEditable) {
    document.execCommand('selectAll', false);
    document.execCommand('insertText', false, text);
  } else {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  if (submit) {
    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true };
    el.dispatchEvent(new KeyboardEvent('keydown', opts));
    el.dispatchEvent(new KeyboardEvent('keyup', opts));
    if (el.form && el.form.requestSubmit) el.form.requestSubmit();
  }
  return { typed: text.length, submitted: !!submit };
}

// ---- commands ---------------------------------------------------------------------------------
const HANDLERS = {
  ping: async () => ({ pong: true, ...(await identity()) }),
  open: async ({ url }) => {
    if (!/^https?:\/\//i.test(url || '')) throw new Error('only http(s) URLs');
    const tab = await chrome.tabs.create({ url, active: true });
    await waitLoaded(tab.id);
    const t = await chrome.tabs.get(tab.id);
    return { tabId: t.id, url: t.url, title: t.title };
  },
  tabs: async () => (await chrome.tabs.query({})).map((t) => ({ id: t.id, url: t.url, title: t.title, active: t.active, incognito: t.incognito })),
  read: async ({ maxChars }) => inPage(pageRead, [maxChars || 30000]),
  resolve: async ({ target, kind, index }) => inPage(pageResolve, [String(target || ''), kind === 'field' ? 'field' : 'control', index ?? null]),
  controls: async ({ limit }) => inPage(pageControls, [Math.min(limit || 80, 150)]),
  click: async () => { const r = await inPage(pageClick); await new Promise((ok) => setTimeout(ok, 800)); return r; },
  type: async ({ text, submit }) => inPage(pageType, [String(text || ''), !!submit]),
  screenshot: async () => {
    const tab = await activeTab();
    return { dataUrl: await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' }), url: tab.url };
  },
};
