/* Rozan web UI. Plain JS, no build step.
 *
 * The same page serves the modal next to the bubble (narrow) and the full app window (wide, with a
 * sidebar). Every value that came from a tool, the reader, or the audit log is untrusted: it is only
 * ever placed in the DOM through textContent, never as markup. The page's CSP forbids inline script.
 *
 * Token: window.__AGENT_TOKEN__ (injected by the native shell), or #token=... in the URL once
 * (stored in sessionStorage, then stripped), or pasted by hand on the token screen.
 * Shell hook: when window.webkit.messageHandlers.shell exists the page shows "Open full app" and
 * posts {action: "openFullApp"} to it.
 */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const stream = $('#stream');
  const input = $('#input');
  const shell = (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.shell) || null;

  const state = {
    token: null,
    conversationId: null,
    ws: null,
    wsAttempt: 0,
    planning: false,
    paused: false,
    agentState: 'idle',
    view: 'chat',
    turnEls: new Map(),   // turn id -> element
    planTurn: new Map(),  // plan id -> turn id
    runEls: new Map(),    // run id -> {root, head, events, status, stop, planId}
    pendingShow: null,    // badge of the last show card, until its audit record names the sources
    audit: [],
    otherRuns: new Set(),
  };

  // ---------- small helpers ----------------------------------------------------------------
  function el(tag, attrs = {}, ...children) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('data-') || k === 'title' || k === 'type' || k === 'id' || k === 'role' || k === 'aria-label') n.setAttribute(k, v);
      else n[k] = v;
    }
    for (const c of children) if (c != null) n.append(c);
    return n;
  }
  const str = (v) => (typeof v === 'string' ? v : JSON.stringify(v, null, 2));
  function compact(v, max = 160) {
    let s = typeof v === 'string' ? v : JSON.stringify(v);
    if (s == null) s = '';
    return s.length > max ? s.slice(0, max) + '…' : s;
  }
  function when(ts) {
    const d = new Date(ts * 1000);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  }
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false });
  const wide = () => window.matchMedia('(min-width: 720px)').matches;
  function nearBottom() { return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 80; }
  function scrollDown(force) { if (force || nearBottom()) stream.scrollTop = stream.scrollHeight; }
  let toastTimer = null;
  function toast(msg, kind = 'err') {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast' + (kind === 'info' ? ' toast-info' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), 4000);
  }
  const show = (sel) => $(sel).classList.remove('hidden');
  const hide = (sel) => $(sel).classList.add('hidden');
  function store(key, val) { try { if (val == null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, val); } catch (e) { /* private mode */ } }
  function load(key) { try { return sessionStorage.getItem(key); } catch (e) { return null; } }

  // ---------- token ------------------------------------------------------------------------
  function discoverToken() {
    let t = window.__AGENT_TOKEN__ || null;
    const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
    if (hash.get('token')) {
      t = hash.get('token');
      history.replaceState(null, '', location.pathname);
    }
    if (t) store('agent_token', t); else t = load('agent_token');
    return t;
  }
  function showTokenScreen(msg) {
    if (msg) $('#token-msg').textContent = msg;
    show('#token-screen');
    $('#token-input').focus();
  }

  // ---------- api --------------------------------------------------------------------------
  async function api(method, path, body) {
    const headers = { Authorization: 'Bearer ' + state.token };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let r;
    try {
      r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      throw new Error('The core is not reachable.');
    }
    if (r.status === 401) {
      showTokenScreen('The core rejected this token. Paste the current one from ~/.personal_agent/session.token.');
      throw new Error('unauthorized');
    }
    if (r.status === 204) return null;
    let data = null;
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (!r.ok) {
      const d = data && (data.detail || data.error);
      throw new Error(d ? (typeof d === 'string' ? d : JSON.stringify(d)) : `${method} ${path} failed (${r.status})`);
    }
    return data;
  }

  // ---------- views and agent state --------------------------------------------------------
  function showView(name) {
    state.view = name;
    for (const v of $$('.view')) v.classList.toggle('hidden', v.id !== 'view-' + name);
    for (const b of $$('.side-link')) b.classList.toggle('active', b.dataset.view === name);
    // In the modal, a non-chat view keeps only the back button in the top bar.
    const subview = name !== 'chat' && !wide();
    $('#btn-back').classList.toggle('hidden', !subview);
    for (const b of [$('#btn-new'), $('#btn-more'), $('#btn-settings')]) b.classList.toggle('hidden', subview);
    hide('#more');
    if (name === 'audit') loadAudit();
    if (name === 'settings') loadSettings();
    if (name === 'chat') input.focus();
  }

  const STATE_LABEL = {
    idle: 'Idle', planning: 'Planning…', running: 'Running…', needs_approval: 'Needs approval',
    error: 'Error', paused: 'Standby',
  };
  function setAgentState(view) {
    state.agentState = view.state;
    state.paused = !!view.paused;
    state.watcher = !!view.watcher;
    const chip = $('#state-chip');
    chip.className = 'chip chip-' + view.state + (view.state === 'idle' ? ' hidden' : '');
    chip.textContent = STATE_LABEL[view.state] || view.state;
    chip.title = view.last_error ? view.last_error : '';
    document.title = (view.state === 'needs_approval' ? '● Approval needed · ' : '') + 'Rozan';
    $('#paused-banner').classList.toggle('hidden', !state.paused);
    $('#paused-text').textContent = (view.standby_reason || 'On standby.') + ' Nothing is planned or run until you resume.';
    $('#btn-estop').classList.toggle('hidden', !['planning', 'running', 'needs_approval'].includes(view.state));
    for (const b of [$('#side-pause'), $('#more-pause')]) {
      b.querySelector('.lbl').textContent = state.paused ? 'Resume agent' : 'Standby';
      const w = b.querySelector('.ic-wrap');
      if (w) w.innerHTML = window.icon(state.paused ? 'play' : 'pause');
    }
    setComposer(!state.planning && !state.paused);
  }
  async function togglePause() {
    hide('#more');
    try { setAgentState(await api('POST', state.paused ? '/resume' : '/pause')); }
    catch (e) { toast(e.message); }
  }

  // ---------- rendering: turns and plans ---------------------------------------------------
  function resetStream() {
    for (const n of Array.from(stream.children)) if (n.id !== 'empty') n.remove();
    state.turnEls.clear(); state.planTurn.clear(); state.runEls.clear(); state.pendingShow = null;
    $('#empty').classList.remove('hidden');
  }

  function countSteps(steps) {
    let n = 0;
    for (const s of steps || []) { n += 1; if (s.steps) n += countSteps(s.steps); }
    return n;
  }

  function renderTurn(turn) {
    $('#empty').classList.add('hidden');
    let root = state.turnEls.get(turn.id);
    if (!root) {
      root = el('section', { class: 'turn', 'data-turn-id': turn.id });
      stream.append(root);
      state.turnEls.set(turn.id, root);
    }
    root.replaceChildren();
    root.append(el('div', { class: 'msg-user' + (turn.kind === 'example' ? ' msg-example' : ''), text: turn.request }));
    if (turn.error) {
      root.append(el('div', { class: 'card card-error' },
        el('div', { class: 'card-title', text: 'Could not make a plan' }),
        el('pre', { class: 'pre', text: turn.error })));
    }
    if (turn.answer) {
      root.append(botMsg(md(turn.answer)));
    } else if (turn.plan) {
      state.planTurn.set(turn.plan_id, turn.id);
      root.append(turn.auto_run ? renderPlanMini(turn) : renderPlanCard(turn));
    }
    root.append(el('div', { class: 'runs' }));
    for (const run of turn.runs || []) {
      ensureRun({ id: run.id, plan_id: run.plan_id, status: run.status, error: run.error });
      for (const evt of run.events || []) renderEvent(evt, false);
    }
    return root;
  }

  // ---------- small helpers for the modern layout -------------------------------------------
  function iconEl(name, size = 16) { const s = el('span', { class: 'ic-wrap' }); s.innerHTML = window.icon(name, size); return s; }

  function botMsg(...children) {
    return el('div', { class: 'msg-bot' }, el('span', { class: 'avatar' }, iconEl('rozan', 15)), el('div', { class: 'msg-bot-body' }, ...children));
  }

  // Minimal Markdown for replies: escaped first, so no HTML or links from any source can get through.
  function md(text) {
    const esc = (t) => t.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const inline = (t) => esc(t)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
    const out = [];
    let list = null;
    const lines = String(text).replace(/\r/g, '').split('\n');
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i];
      if (/^```/.test(l)) {
        const code = [];
        for (i++; i < lines.length && !/^```/.test(lines[i]); i++) code.push(lines[i]);
        if (list) { out.push(`</${list}>`); list = null; }
        out.push(`<pre class="pre">${esc(code.join('\n'))}</pre>`);
        continue;
      }
      const ul = l.match(/^\s*[-*•]\s+(.*)$/), ol = l.match(/^\s*\d+[.)]\s+(.*)$/);
      const kind = ul ? 'ul' : ol ? 'ol' : null;
      if (kind) {
        if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
        out.push(`<li>${inline((ul || ol)[1])}</li>`);
        continue;
      }
      if (list) { out.push(`</${list}>`); list = null; }
      const h = l.match(/^#{1,6}\s+(.*)$/);
      if (h) out.push(`<p class="md-h">${inline(h[1])}</p>`);
      else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
    }
    if (list) out.push(`</${list}>`);
    const d = el('div', { class: 'md' });
    d.innerHTML = out.join('');
    return d;
  }

  // A readable list of what a plan will do, nested for loops.
  function describeArgs(args) {
    const parts = [];
    for (const [k, v] of Object.entries(args || {})) {
      const val = v && typeof v === 'object' && '$ref' in v ? `‹${v.$ref}›` : typeof v === 'string' ? v : JSON.stringify(v);
      parts.push(k === 'text' || k === 'command' || k === 'url' ? val : `${k}: ${val}`);
    }
    return parts.join(' · ');
  }
  const RISKY = new Set(['shell.run', 'gmail.send', 'gmail.trash', 'browser.click', 'browser.type', 'fs.write', 'fs.move_to_trash']);
  function stepList(steps) {
    const ul = el('ul', { class: 'steps' });
    for (const st of steps || []) {
      if (st.foreach) {
        const li = el('li', { class: 'step-loop' }, el('span', { class: 'step-tool', text: 'for each' }),
          el('span', { class: 'step-args', text: `‹${st.foreach.$ref || ''}›` }));
        li.append(stepList(st.steps));
        ul.append(li);
      } else if (st.quarantine) {
        ul.append(el('li', {}, el('span', { class: 'step-tool', text: 'reader' }),
          el('span', { class: 'step-args', text: st.quarantine.question || st.quarantine.schema })));
      } else {
        const li = el('li', { class: RISKY.has(st.tool) ? 'step-risky' : '' },
          el('span', { class: 'step-tool', text: st.tool }), el('span', { class: 'step-args', text: describeArgs(st.args) }));
        if (st.when) li.append(el('span', { class: 'step-when', text: `if ‹${st.when.$ref || ''}›` }));
        ul.append(li);
      }
    }
    return ul;
  }

  // A plan that only reads and shows runs on its own: one quiet line, details on demand.
  function renderPlanMini(turn) {
    const plan = turn.plan;
    const box = el('details', { class: 'plan-mini', 'data-plan-id': turn.plan_id });
    const n = countSteps(plan.steps);
    box.append(el('summary', {}, iconEl('rozan', 13), el('span', { text: plan.goal || 'Plan' }),
      el('span', { class: 'muted', text: ` · ${n} step${n === 1 ? '' : 's'}` })));
    box.append(stepList(plan.steps));
    if (turn.planned_by) box.append(el('div', { class: 'plan-meta muted', text: 'Planned by ' + turn.planned_by }));
    for (const note of turn.notes || []) box.append(el('div', { class: 'plan-note', text: note }));
    if (turn.unavailable) box.open = true, box.append(el('div', { class: 'plan-note', text: turn.unavailable }));
    return box;
  }

  function renderPlanCard(turn) {
    const plan = turn.plan;
    const n = countSteps(plan.steps);
    const card = el('div', { class: 'card card-plan', 'data-plan-id': turn.plan_id });
    card.append(el('div', { class: 'card-head' },
      el('span', { class: 'card-title' }, iconEl('shield', 14), el('span', { text: 'Review before running' })),
      el('span', { class: 'muted', text: `${n} step${n === 1 ? '' : 's'}` })));
    card.append(el('div', { class: 'plan-goal', text: plan.goal || '(no goal)' }));
    card.append(stepList(plan.steps));
    if (turn.scope_text) {
      card.append(el('div', { class: 'plan-meta' },
        el('span', { class: 'muted', text: 'Allowed to: ' }),
        el('span', { text: turn.scope_text.length ? turn.scope_text.join(', ') : 'only show results' })));
    }
    if (turn.planned_by) {
      const why = turn.escalated && turn.escalated.length ? ' · careful check because ' + turn.escalated.join('; ') : '';
      card.append(el('div', { class: 'plan-meta muted', text: 'Planned by ' + turn.planned_by + why }));
    }
    for (const n of turn.notes || []) card.append(el('div', { class: 'plan-note', text: n }));
    const pre = el('pre', { class: 'pre hidden', text: JSON.stringify(plan, null, 2) });
    const toggle = el('button', { class: 'btn btn-link', type: 'button', text: 'Show JSON' });
    toggle.classList.add('btn-small');
    toggle.addEventListener('click', () => {
      const hidden = pre.classList.toggle('hidden');
      toggle.textContent = hidden ? 'Show JSON' : 'Hide JSON';
    });
    if (turn.unavailable) {
      card.append(el('div', { class: 'plan-note', text: turn.unavailable }));
      const toggleOnly = el('button', { class: 'btn btn-link', type: 'button', text: 'Show JSON' });
      const preOnly = el('pre', { class: 'pre hidden', text: JSON.stringify(plan, null, 2) });
      toggleOnly.addEventListener('click', () => preOnly.classList.toggle('hidden'));
      card.append(el('div', { class: 'actions-row' }, toggleOnly), preOnly);
      return card;
    }
    const exec = el('button', { class: 'btn btn-primary btn-exec', type: 'button', text: 'Run' });
    const cancel = el('button', { class: 'btn btn-ghost btn-cancel-plan', type: 'button', text: 'Cancel' });
    const actions = el('div', { class: 'actions-row' }, exec, cancel, toggle);
    exec.addEventListener('click', async () => {
      exec.disabled = true;
      try { await api('POST', `/plan/${turn.plan_id}/run`); }
      catch (e) { toast(e.message); exec.disabled = false; }
    });
    cancel.addEventListener('click', () => {
      card.classList.add('card-discarded');
      actions.replaceChildren(el('span', { class: 'muted', text: 'Not executed' }), toggle);
    });
    card.append(actions, pre);
    return card;
  }

  // ---------- rendering: runs and events ---------------------------------------------------
  const STATUS_LABEL = {
    running: 'Running…', done: 'Done', refused: 'Stopped: policy refused a step',
    error: 'Error', cancelled: 'Stopped',
  };

  function ensureRun(info) {
    let r = state.runEls.get(info.id);
    if (r) return r;
    const turnId = state.planTurn.get(info.plan_id);
    const host = turnId ? state.turnEls.get(turnId) : null;
    const root = el('div', { class: 'run', 'data-run-id': info.id });
    const status = el('span', { class: 'run-status' });
    const stop = el('button', { class: 'btn btn-ghost btn-small btn-stop', type: 'button', text: 'Stop' });
    stop.addEventListener('click', async () => {
      stop.disabled = true;
      try { await api('POST', `/run/${info.id}/cancel`); }
      catch (e) { toast(e.message); stop.disabled = false; }
    });
    const head = el('div', { class: 'run-head' }, el('span', { class: 'spinner' }), status, stop);
    const events = el('div', { class: 'events' });
    root.append(head, events);
    (host ? host.querySelector('.runs') : stream).append(root);
    r = { root, head, events, status, stop, planId: info.plan_id };
    state.runEls.set(info.id, r);
    setRunStatus(r, info.status || 'running', info.error);
    return r;
  }

  function setRunStatus(r, status, error) {
    r.root.dataset.status = status;
    r.status.textContent = STATUS_LABEL[status] || status;
    r.head.querySelector('.spinner').classList.toggle('hidden', status !== 'running');
    r.stop.classList.toggle('hidden', status !== 'running');
    if (status !== 'running') {
      const card = r.planId ? stream.querySelector(`.card-plan[data-plan-id="${r.planId}"]`) : null;
      const exec = card ? card.querySelector('.btn-exec') : null;
      if (exec) { exec.disabled = false; exec.textContent = 'Run again'; }
    }
  }

  function line(r, cls, text, step, detail) {
    const n = el('div', { class: 'line ' + cls });
    if (step) n.append(el('span', { class: 'step', text: step }));
    n.append(el('span', { class: 'line-text', text }));
    if (detail !== undefined) {
      const pre = el('pre', { class: 'pre hidden', text: str(detail) });
      n.append(pre);
      n.classList.add('expandable');
      n.title = 'Click to show the audit record';
      n.addEventListener('click', () => pre.classList.toggle('hidden'));
    }
    r.events.append(n);
    return n;
  }

  function runFor(runId) { return state.runEls.get(runId) || null; }

  function renderEvent(evt, live = true) {
    if (evt.run_id && state.otherRuns.has(evt.run_id)) {
      if (evt.type === 'run.end') { state.otherRuns.delete(evt.run_id); if (live) refreshChats(); }
      return;                        // progress of a task in another chat
    }
    switch (evt.type) {
      case 'state':
        if (live) setAgentState(evt);
        return;
      case 'run.start':
        if (live && evt.conversation_id && evt.conversation_id !== state.conversationId) { state.otherRuns.add(evt.run_id); return; }
        ensureRun({ id: evt.run_id, plan_id: evt.plan_id, status: 'running' });
        break;
      case 'run.end': {
        const r = ensureRun({ id: evt.run_id, plan_id: evt.plan_id });
        setRunStatus(r, evt.status, evt.error);
        if (evt.status === 'error' && evt.error) line(r, 'line-error', evt.error);
        if (live) refreshChats();
        break;
      }
      case 'approval.request': renderApproval(evt); break;
      case 'choice.request': renderChoice(evt); break;
      case 'choice.resolved': resolveChoiceCard(evt); break;
      case 'approval.resolved': resolveApprovalCard(evt); break;
      case 'show': renderShow(evt); break;
      case 'audit': renderAudit(evt); break;
      case 'log': {
        // The executor also prints what it audits; keep only the lines that carry extra information.
        const dup = /^\[(policy|approval|tool|quarantine)\]|^\[executor\] skipping/.test(evt.text || '');
        const r = runFor(evt.run_id);
        if (r && !dup) line(r, 'line-log', evt.text);
        break;
      }
      default: break;
    }
    if (live) scrollDown(evt.type === 'approval.request' || evt.type === 'choice.request');
  }

  function renderAudit(evt) {
    const r = runFor(evt.run_id);
    if (!r) return;
    const rec = evt.record || {};
    switch (rec.event) {
      case 'plan.start': case 'plan.end': case 'approval':
        return;
      case 'watcher':
        line(r, rec.verdict === 'ok' ? 'line-ok' : 'line-warn', `watcher: ${rec.verdict}${rec.reason ? ' — ' + rec.reason : ''}`, rec.step, rec);
        return;
      case 'choice':
        line(r, 'line-ok', `you picked ${rec.selected && rec.selected.length ? compact(rec.selected) : 'nothing'}`, rec.step, rec);
        return;
      case 'tool.call':
        if (rec.tool === 'show') {
          if (state.pendingShow) { setSources(state.pendingShow, rec.arg_sources); state.pendingShow = null; }
          return;
        }
        line(r, 'line-ok', `${rec.tool} ${compact(rec.args)}`, rec.step, rec);
        return;
      case 'tool.error':
        line(r, 'line-warn', `${rec.tool} failed: ${rec.error}`, rec.step, rec);
        return;
      case 'step.skip':
        line(r, 'line-muted', `skipped${rec.tool ? ' ' + rec.tool : ''}${rec.reason ? ': ' + rec.reason : ' (condition false or denied)'}`, rec.step, rec);
        return;
      case 'policy.refuse':
        line(r, 'line-refuse', `policy refused ${rec.tool}: ${rec.reason}`, rec.step, rec);
        return;
      case 'quarantine': {
        const res = rec.result && typeof rec.result === 'object' && 'value' in rec.result ? rec.result.value : rec.result;
        line(r, 'line-quarantine', `reader ${rec.schema} → ${compact(res)}`, rec.step, rec);
        return;
      }
      case 'quarantine.error':
        line(r, 'line-warn', `reader failed: ${rec.error}`, rec.step, rec);
        return;
      default:
        line(r, 'line-muted', `${rec.event || 'event'} ${compact(rec)}`, rec.step, rec);
    }
  }

  function renderShow(evt) {
    const r = runFor(evt.run_id);
    if (!r) return;
    const v = evt.value;
    const badge = el('span', { class: 'badge badge-untrusted', text: 'untrusted' });
    const card = el('div', { class: 'card card-untrusted', 'data-kind': 'show' },
      el('div', { class: 'card-head' }, el('span', { class: 'card-title', text: 'Result' }), badge),
      typeof v === 'string' ? md(v)
        : (v && typeof v === 'object' && typeof v.answer === 'string') ? answerBlock(v)
        : el('pre', { class: 'pre', text: str(v) }));
    r.events.append(card);
    state.pendingShow = badge;
  }

  function answerBlock(v) {
    const wrap = el('div', {}, el('div', { class: 'answer-text' }, md(v.answer)));
    const pre = el('pre', { class: 'pre hidden', text: typeof v.details === 'string' ? v.details : str(v.details) });
    const t = el('button', { class: 'btn btn-link btn-small', type: 'button', text: 'Show details' });
    t.addEventListener('click', () => { const h = pre.classList.toggle('hidden'); t.textContent = h ? 'Show details' : 'Hide details'; });
    wrap.append(t, pre);
    return wrap;
  }

  function setSources(badge, sources) {
    if (!Array.isArray(sources) || !sources.length) return;
    const trusted = sources.every((s) => s === 'user' || s === 'planner');
    const mac = sources.every((s) => s === 'system' || s === 'user' || s === 'planner');
    badge.textContent = trusted ? 'from planner' : mac ? 'from this Mac' : 'untrusted · ' + sources.join(', ');
    if (trusted || mac) {
      // Our own text: shown as a normal reply instead of a warning-coloured card.
      const card = badge.closest('.card');
      if (card) {
        const body = [...card.children].filter((c) => !c.classList.contains('card-head'));
        const msg = botMsg(...body);
        if (mac && !trusted) msg.querySelector('.msg-bot-body').append(el('span', { class: 'src-note', text: 'from this Mac' }));
        card.replaceWith(msg);
      }
    }
  }

  function renderApproval(evt) {
    const r = runFor(evt.run_id);
    if (!r) return;
    const card = el('div', { class: 'card card-approval', 'data-approval-id': evt.approval_id });
    card.append(el('div', { class: 'card-head' },
      el('span', { class: 'card-title', text: 'Approval needed' }),
      el('span', { class: 'badge badge-warn', text: evt.tool })));
    const args = el('dl', { class: 'args' });
    for (const [k, v] of Object.entries(evt.args || {})) {
      args.append(el('dt', { text: k }), el('dd', {}, el('pre', { class: 'pre pre-args', text: str(v) })));
    }
    card.append(args);
    const yes = el('button', { class: 'btn btn-primary', type: 'button', text: 'Approve', 'data-decision': 'yes' });
    const no = el('button', { class: 'btn btn-danger', type: 'button', text: 'Deny', 'data-decision': 'no' });
    const all = el('button', { class: 'btn btn-ghost', type: 'button', text: `Approve all ${evt.tool} this run`, 'data-decision': 'all' });
    const row = el('div', { class: 'actions-row' }, yes, no, all);
    for (const b of [yes, no, all]) {
      b.addEventListener('click', async () => {
        for (const x of [yes, no, all]) x.disabled = true;
        try { await api('POST', `/approval/${evt.approval_id}`, { decision: b.dataset.decision }); }
        catch (e) { toast(e.message); for (const x of [yes, no, all]) x.disabled = false; }
      });
    }
    card.append(row);
    r.events.append(card);
  }

  // The options usually come from a web page: shown as untrusted text, picked only by the owner.
  function renderChoice(evt) {
    const r = runFor(evt.run_id);
    if (!r) return;
    const card = el('div', { class: 'card card-approval card-choice', 'data-choice-id': evt.choice_id });
    card.append(el('div', { class: 'card-head' },
      el('span', { class: 'card-title', text: evt.question || 'Choose' }),
      el('span', { class: 'badge badge-untrusted', text: 'options from the page' })));
    const list = el('div', { class: 'choice-list' });
    const name = 'c' + evt.choice_id;
    for (const opt of evt.options || []) {
      const input = el('input', { type: evt.multiple ? 'checkbox' : 'radio', name, value: opt });
      input.checked = (evt.suggested || []).includes(opt);
      list.append(el('label', { class: 'choice-opt' }, input, el('span', { class: 'show-text', text: opt })));
    }
    const ok = el('button', { class: 'btn btn-primary', type: 'button', text: 'Confirm' });
    const none = el('button', { class: 'btn btn-ghost', type: 'button', text: 'None of these' });
    const send = async (selected) => {
      ok.disabled = none.disabled = true;
      try { await api('POST', `/choice/${evt.choice_id}`, { selected }); }
      catch (e) { toast(e.message); ok.disabled = none.disabled = false; }
    };
    ok.addEventListener('click', () => send($$('input:checked', list).map((i) => i.value)));
    none.addEventListener('click', () => send([]));
    card.append(list, el('div', { class: 'actions-row' }, ok, none));
    r.events.append(card);
  }

  function resolveChoiceCard(evt) {
    const card = stream.querySelector(`.card-choice[data-choice-id="${evt.choice_id}"]`);
    if (!card) return;
    const by = evt.by === 'timeout' ? ' (timed out)' : evt.by === 'cancel' ? ' (run stopped)' : '';
    const picked = evt.selected && evt.selected.length ? 'You picked: ' + evt.selected.join(', ') : 'Nothing picked';
    card.querySelector('.choice-list').classList.add('hidden');
    card.querySelector('.actions-row').replaceChildren(el('span', { class: 'resolved resolved-yes', text: picked + by }));
    card.classList.add('card-resolved');
  }

  function resolveApprovalCard(evt) {
    const card = stream.querySelector(`.card-approval[data-approval-id="${evt.approval_id}"]`);
    if (!card) return;
    const labels = { yes: 'Approved', no: 'Denied', all: 'Approved, and all further ' + evt.tool + ' this run' };
    const by = evt.by === 'timeout' ? ' (timed out)' : evt.by === 'cancel' ? ' (run stopped)' : '';
    card.querySelector('.actions-row').replaceChildren(
      el('span', { class: 'resolved ' + (evt.decision === 'no' ? 'resolved-no' : 'resolved-yes'), text: (labels[evt.decision] || evt.decision) + by }));
    card.classList.add('card-resolved');
  }

  // ---------- conversations ----------------------------------------------------------------
  async function pickConversation() {
    const saved = load('agent_conversation');
    if (saved) {
      try { await api('GET', `/conversations/${saved}`); return saved; } catch (e) { /* gone */ }
    }
    const list = await api('GET', '/conversations');
    if (list.length) return list[0].id;
    return (await api('POST', '/conversations')).id;
  }

  async function loadConversation() {
    if (!state.conversationId) { resetStream(); return; }
    const conv = await api('GET', `/conversations/${state.conversationId}`);
    resetStream();
    showMode(conv.mode);
    for (const turn of conv.turns) renderTurn(turn);
    scrollDown(true);
    markActiveChat();
  }

  // A reply to /chat or /examples names the conversation it went to (the core creates one when
  // none is given), so adopting it keeps the page in step even if "New chat" is still in flight.
  function adoptConversation(id) {
    if (!id || id === state.conversationId) return;
    state.conversationId = id;
    store('agent_conversation', id);
    markActiveChat();
  }

  const MODE_LABEL = { isolated: 'Isolated chat · no memory', incognito: 'Incognito · not saved' };
  function showMode(mode) {
    const chip = $('#mode-chip');
    chip.textContent = MODE_LABEL[mode] || '';
    chip.classList.toggle('hidden', !MODE_LABEL[mode]);
  }

  async function newChat(mode) {
    mode = typeof mode === 'string' ? mode : 'normal';
    if (mode !== 'normal') {
      try {
        const c = await api('POST', '/conversations', { mode });
        state.conversationId = c.id;
        store('agent_conversation', mode === 'incognito' ? null : c.id);
        resetStream();
        showMode(mode);
        showView('chat');
        refreshChats();
      } catch (e) { toast(e.message); }
      return;
    }
    showMode('normal');
    state.conversationId = null;
    store('agent_conversation', null);
    resetStream();
    showView('chat');
    input.focus();
    try {
      const c = await api('POST', '/conversations');
      if (state.conversationId === null) adoptConversation(c.id);
      refreshChats();
    } catch (e) { toast(e.message); }
  }

  async function switchConversation(id) {
    if (id === state.conversationId) { showView('chat'); return; }
    state.conversationId = id;
    store('agent_conversation', id);
    showView('chat');
    try { await loadConversation(); } catch (e) { toast(e.message); }
  }

  async function deleteConversation(id, title) {
    if (!window.confirm(`Delete "${title}"? This removes the stored chat; the audit log keeps its records.`)) return;
    try {
      await api('DELETE', `/conversations/${id}`);
      if (id === state.conversationId) {
        state.conversationId = null;
        store('agent_conversation', null);
        resetStream();
      }
      await refreshChats();
    } catch (e) { toast(e.message); }
  }

  function markActiveChat() {
    for (const b of $$('.chat-item, #more-chats .list-item')) b.classList.toggle('active', b.dataset.cid === state.conversationId);
  }

  async function refreshChats() {
    let list;
    try { list = await api('GET', '/conversations'); } catch (e) { return; }
    const side = $('#chat-list');
    const more = $('#more-chats');
    side.replaceChildren();
    more.replaceChildren();
    if (!list.length) {
      side.append(el('li', { class: 'muted', text: 'No chats yet' }));
      more.append(el('li', { class: 'muted', text: 'No chats yet' }));
    }
    for (const c of list) {
      const title = (c.mode === 'incognito' ? '🕶 ' : c.mode === 'isolated' ? '◌ ' : '') + (c.title || 'New chat');
      const open = el('button', { class: 'chat-item', type: 'button', 'data-cid': c.id },
        el('span', { class: 'chat-title', text: title }),
        el('span', { class: 'muted', text: `${when(c.updated || c.created)} · ${c.turns} turn${c.turns === 1 ? '' : 's'}` }));
      open.addEventListener('click', () => switchConversation(c.id));
      const del = el('button', { class: 'chat-del', type: 'button', 'aria-label': 'Delete chat', title: 'Delete chat' }, iconEl('trash-2', 13));
      del.addEventListener('click', (e) => { e.stopPropagation(); deleteConversation(c.id, title); });
      side.append(el('li', {}, open, del));
      const item = el('button', { class: 'list-item', type: 'button', 'data-cid': c.id },
        el('span', { class: 'list-title', text: title }),
        el('span', { class: 'muted', text: when(c.updated || c.created) }));
      item.addEventListener('click', () => switchConversation(c.id));
      more.append(el('li', {}, item));
    }
    markActiveChat();
  }

  // ---------- saved actions (examples) -----------------------------------------------------
  async function refreshActions() {
    let items;
    try { items = await api('GET', '/examples'); } catch (e) { return; }
    for (const [listSel, cls] of [['#action-list', 'action-item'], ['#more-actions', 'list-item']]) {
      const ul = $(listSel);
      ul.replaceChildren();
      if (!items.length) ul.append(el('li', { class: 'muted', text: 'No saved actions in examples/' }));
      for (const it of items) {
        const b = el('button', { class: cls, type: 'button', 'data-example': it.name },
          el('span', { class: cls === 'action-item' ? 'action-title' : 'list-title', text: it.name }),
          el('span', { class: 'muted', text: it.goal || `${it.steps} steps` }));
        if (it.unavailable) {
          b.disabled = true;
          b.title = it.unavailable;
          b.classList.add('item-off');
          b.append(el('span', { class: 'off-reason', text: 'Not available yet (hover for why)' }));
        } else {
          b.addEventListener('click', () => loadExample(it.name));
        }
        ul.append(el('li', {}, b));
      }
    }
  }
  async function loadExample(name) {
    hide('#more');
    showView('chat');
    try {
      const res = await api('POST', `/examples/${name}/load`, { conversation_id: state.conversationId });
      adoptConversation(res.conversation_id);
      $('#empty').classList.add('hidden');
      renderTurn(res.turn);
      scrollDown(true);
      refreshChats();
    } catch (e) { toast(e.message); }
  }

  // ---------- composer ---------------------------------------------------------------------
  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  }
  function setComposer(enabled) {
    input.disabled = !enabled;
    $('#btn-send').disabled = !enabled;
    input.placeholder = state.paused ? 'On standby' : 'Ask anything…';
  }
  async function send() {
    const text = input.value.trim();
    if (!text || state.planning || state.paused) return;
    state.planning = true;
    input.value = ''; autosize(); setComposer(false);
    $('#empty').classList.add('hidden');
    const ph = el('section', { class: 'turn turn-pending' },
      el('div', { class: 'msg-user', text }),
      el('div', { class: 'planning' }, el('span', { class: 'typing' }, el('i'), el('i'), el('i')), el('span', { text: 'Thinking…' })));
    stream.append(ph);
    scrollDown(true);
    try {
      let res;
      try {
        res = await api('POST', '/chat', { message: text, conversation_id: state.conversationId });
      } catch (e) {
        if (e.message !== 'unknown conversation') throw e;
        // the open chat was deleted elsewhere (another window): carry on in a new chat
        state.conversationId = null;
        store('agent_conversation', null);
        res = await api('POST', '/chat', { message: text, conversation_id: null });
        toast('That chat was deleted, so this went to a new chat.', 'info');
      }
      adoptConversation(res.conversation_id);
      ph.remove();
      renderTurn(res.turn);
      scrollDown(true);
      refreshChats();
      if (res.turn.auto_run && res.turn.plan_id) {
        // reads and shows only: no Execute press needed; risky steps inside still ask
        api('POST', `/plan/${res.turn.plan_id}/run`).catch((e) => toast(e.message));
      }
    } catch (e) {
      ph.remove();
      if (e.message !== 'unauthorized') toast(e.message);
      input.value = text; autosize();
    } finally {
      state.planning = false;
      setComposer(!state.paused);
      input.focus();
    }
  }

  // ---------- audit view -------------------------------------------------------------------
  function auditSummary(rec) {
    switch (rec.event) {
      case 'tool.call': return `${rec.tool} ${compact(rec.args, 120)}`;
      case 'tool.error': return `${rec.tool} failed: ${rec.error}`;
      case 'approval': return `${rec.tool} ${rec.approved ? 'approved' : 'denied'} ${compact(rec.args, 80)}`;
      case 'policy.refuse': return `${rec.tool}: ${rec.reason}`;
      case 'quarantine': return `${rec.schema} → ${compact(rec.result && rec.result.value !== undefined ? rec.result.value : rec.result, 120)}`;
      case 'quarantine.error': return rec.error;
      case 'step.skip': return rec.reason || 'condition false or denied';
      case 'plan.start': case 'plan.end': return rec.goal || '';
      default: return compact(rec, 120);
    }
  }
  function renderAuditRows() {
    const body = $('#audit-body');
    const q = $('#audit-filter').value.trim().toLowerCase();
    const type = $('#audit-event').value;
    body.replaceChildren();
    const rows = state.audit.filter((rec) => (!type || rec.event === type) && (!q || JSON.stringify(rec).toLowerCase().includes(q)));
    if (!rows.length) { body.append(el('p', { class: 'muted', text: state.audit.length ? 'Nothing matches.' : 'The audit log is empty.' })); return; }
    for (const rec of rows.slice().reverse()) {
      const row = el('div', { class: 'audit-row', 'data-event': rec.event || '' });
      const pre = el('pre', { class: 'pre hidden', text: str(rec) });
      row.append(el('span', { class: 'when', text: clock(rec.ts || 0) }),
        el('span', { class: 'what' }, el('span', { class: 'evt', text: rec.event || '?' }),
          el('span', { class: 'muted', text: (rec.step ? rec.step + ' · ' : '') + (rec.run ? 'run ' + String(rec.run).slice(0, 6) + ' · ' : '') }),
          el('span', { text: auditSummary(rec) })),
        pre);
      row.addEventListener('click', () => pre.classList.toggle('hidden'));
      body.append(row);
    }
  }
  async function loadAudit() {
    if (!state.audit.length) $('#audit-body').replaceChildren(el('p', { class: 'muted', text: 'Loading…' }));
    try { state.audit = await api('GET', '/audit?limit=500'); renderAuditRows(); }
    catch (e) { toast(e.message); }
  }

  // ---------- settings view ----------------------------------------------------------------
  function kv(k, v) {
    return el('div', { class: 'kv' }, el('span', { class: 'k', text: k }), el('span', { class: 'v', text: v == null || v === '' ? '—' : String(v) }));
  }
  function renderPermissions(list, into) {
    into.replaceChildren();
    for (const p of list) {
      into.append(el('div', { class: 'perm', 'data-permission': p.name },
        el('div', { class: 'perm-head' },
          el('span', {}, el('span', { class: 'status-dot status-' + p.status }), el('strong', { text: p.title })),
          el('span', { class: 'muted', text: p.status.replace('_', ' ') + (p.source === 'shell' ? ' · reported by the app' : '') })),
        el('p', { class: 'perm-why', text: p.why }),
        el('p', { class: 'perm-how', text: p.how })));
    }
  }
  function renderSettings(s, perms) {
    const f = document.createDocumentFragment();
    const p = s.policy;
    f.append(el('h3', { text: 'Agent' }));
    const pauseBtn = el('button', { class: 'btn', type: 'button', text: state.paused ? 'Resume agent' : 'Standby' });
    pauseBtn.addEventListener('click', async () => { await togglePause(); pauseBtn.textContent = state.paused ? 'Resume agent' : 'Standby'; });
    const watchBtn = el('button', { class: 'btn', type: 'button', text: state.watcher ? 'Watcher: on' : 'Watcher: off' });
    watchBtn.title = 'A second check on every action that changes something. It can only add a question or stop the task.';
    watchBtn.addEventListener('click', async () => {
      try { const v = await api('POST', '/watcher', { enabled: !state.watcher }); setAgentState(v); watchBtn.textContent = state.watcher ? 'Watcher: on' : 'Watcher: off'; }
      catch (e) { toast(e.message); }
    });
    f.append(el('div', { class: 'actions-row' }, el('span', { class: 'chip chip-' + state.agentState, text: STATE_LABEL[state.agentState] || state.agentState }), pauseBtn, watchBtn));
    f.append(el('h3', { text: 'Memory (encrypted)' }));
    const memBox = el('div', { id: 'memory-box' });
    f.append(memBox);
    renderMemory(memBox);
    f.append(el('h3', { text: 'Models' }),
      kv('planner', s.models.planner), kv('reader', s.models.reader),
      kv('mode', s.fake ? 'fake: scripted models, recorded tools' : 'real'));

    f.append(el('h3', { text: 'Where your data goes' }));
    const ollama = s.external_services.find((x) => x.service === 'ollama');
    const localAI = !ollama || ollama.local;
    f.append(el('p', { class: localAI ? 'muted' : '', text: localAI
      ? 'Prompts and content go only to Ollama on this Mac. No cloud AI is involved. The services below are the ones the agent uses on your behalf.'
      : 'Ollama is not on this Mac: prompts and content are sent to ' + ollama.endpoints.join(', ') + '.' }));
    const st = el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, el('th', { text: 'service' }), el('th', { text: 'status' }), el('th', { text: 'endpoints' }))));
    const stb = el('tbody');
    for (const x of s.external_services) {
      stb.append(el('tr', { class: x.enabled ? '' : 'svc-off' },
        el('td', { text: x.service }),
        el('td', { text: x.enabled ? (x.local ? 'on · this Mac' : 'on') : 'off' }),
        el('td', {}, el('div', { text: x.endpoints.join('\n') }), el('div', { class: 'muted', text: x.note }))));
    }
    st.append(stb);
    f.append(st);

    f.append(el('h3', { text: 'macOS permissions' }));
    f.append(el('p', { class: 'muted', text: 'Each one is requested only when first needed, with the explanation below.' }));
    const permBox = el('div', { id: 'permissions' });
    renderPermissions(perms, permBox);
    const probe = el('button', { class: 'btn', type: 'button', text: 'Check folder access' });
    probe.addEventListener('click', async () => {
      probe.disabled = true;
      try { renderPermissions(await api('POST', '/permissions/probe'), permBox); }
      catch (e) { toast(e.message); }
      probe.disabled = false;
    });
    f.append(permBox, el('div', { class: 'actions-row' }, probe, el('span', { class: 'muted', text: 'Touches each folder once; macOS may ask.' })));

    f.append(el('h3', { text: 'Tools and data flow' }));
    const table = el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, el('th', { text: 'tool' }), el('th', { text: 'may receive data from' }), el('th', { text: 'asks' }), el('th', { text: 'on' }))));
    const tb = el('tbody');
    for (const t of p.tools) {
      tb.append(el('tr', { class: t.available ? '' : 'svc-off' }, el('td', { text: t.name }), el('td', { text: t.allow_sources.join(', ') }),
        el('td', { text: t.approval ? 'yes' : '' }), el('td', { text: t.available ? 'yes' : 'off' })));
    }
    table.append(tb);
    f.append(table, el('p', { class: 'muted', text: `At most ${p.max_steps} steps per plan.` }));
    f.append(el('h3', { text: 'Folders' }),
      kv('readable', p.filesystem.readable_roots.join('\n')), kv('writable', p.filesystem.writable_roots.join('\n')),
      kv('never', p.filesystem.denied_paths.join('\n')), kv('never (patterns)', p.filesystem.denied_globs.join('  ')));
    f.append(el('h3', { text: 'Browser' }),
      kv('browser', p.browser.enabled ? 'Google Chrome (asks on first visit to a new site)' : 'off'),
      kv('extension', s.chrome_extension ? (s.chrome_extension.connected ? 'connected' + (s.chrome_extension.version ? ' (v' + s.chrome_extension.version + ')' : '') : 'not connected: run scripts/install_chrome_bridge.py') : '—'),
      kv('never visit', p.browser.blocked_hosts.join('\n')));
    f.append(el('h3', { text: 'Connected browsers' }));
    const list = (s.chrome_extension && s.chrome_extension.browsers) || [];
    if (!list.length) f.append(el('p', { class: 'muted', text: 'None. Install the Rozan Bridge extension (scripts/install_chrome_bridge.py).' }));
    for (const b of list) {
      const input = el('input', { class: 'label-input', type: 'text', value: b.label || '', placeholder: b.browser, 'aria-label': 'Name for this browser' });
      const save = el('button', { class: 'btn btn-small', type: 'button', text: 'Save name' });
      save.addEventListener('click', async () => {
        try { await api('POST', `/browsers/${b.id}/label`, { label: input.value }); toast('Saved', 'info'); }
        catch (e) { toast(e.message); }
      });
      f.append(el('div', { class: 'kv' }, el('span', { class: 'k', text: b.browser }),
        el('span', { class: 'v' }, el('div', { text: 'id ' + b.id + (b.incognito_allowed ? ' · incognito allowed' : '') }),
          el('div', { class: 'actions-row' }, input, save))));
    }
    f.append(el('h3', { text: 'Shell' }),
      kv('programs', (p.shell.allowed_programs || []).join(', ') || 'any'),
      kv('cwd', p.shell.cwd), kv('timeout', p.shell.timeout_s + ' s'), kv('denied', p.shell.denied_patterns.join('\n')));
    f.append(el('h3', { text: 'Paths' }),
      kv('audit log', s.paths.audit), kv('chats', s.paths.conversations), kv('secrets', s.paths.secrets),
      kv('approval timeout', s.approval_timeout_s + ' s'));
    f.append(el('p', { class: 'muted', text: 'Read-only. Edit policy.yaml and restart the core to change these.' }));
    return f;
  }
  async function renderMemory(box) {
    let m;
    try { m = await api('GET', '/memory'); } catch (e) { box.replaceChildren(el('p', { class: 'muted', text: e.message })); return; }
    box.replaceChildren();
    const pass = el('input', { class: 'label-input', type: 'password', placeholder: 'passphrase', autocomplete: 'off' });
    const act = async (path, body) => {
      try { await api('POST', path, body); renderMemory(box); } catch (e) { toast(e.message); }
    };
    if (!m.set_up) {
      box.append(el('p', { class: 'muted', text: 'Your saved preferences are encrypted with a passphrase only you know. Choose one (8+ characters).' }));
      const go = el('button', { class: 'btn', type: 'button', text: 'Set up memory' });
      go.addEventListener('click', () => act('/memory/setup', { passphrase: pass.value }));
      box.append(el('div', { class: 'actions-row' }, pass, go));
      return;
    }
    if (!m.unlocked) {
      box.append(el('p', { class: 'muted', text: `Locked. It locks itself after ${m.auto_lock_minutes} min unused.` }));
      const go = el('button', { class: 'btn', type: 'button', text: 'Unlock' });
      go.addEventListener('click', () => act('/memory/unlock', { passphrase: pass.value }));
      pass.addEventListener('keydown', (e) => { if (e.key === 'Enter') go.click(); });
      box.append(el('div', { class: 'actions-row' }, pass, go));
      return;
    }
    const lock = el('button', { class: 'btn btn-small', type: 'button', text: 'Lock now' });
    lock.addEventListener('click', () => act('/memory/lock'));
    box.append(el('div', { class: 'actions-row' }, el('span', { class: 'muted', text: 'Unlocked. Used by normal chats only.' }), lock));
    for (const p of m.preferences || []) {
      const del = el('button', { class: 'chat-del', type: 'button', text: '×', title: 'Forget this', 'aria-label': 'Forget this' });
      del.style.visibility = 'visible';
      del.addEventListener('click', async () => {
        try { await api('DELETE', `/memory/preferences/${p.id}`); renderMemory(box); } catch (e) { toast(e.message); }
      });
      box.append(el('div', { class: 'kv' }, el('span', { class: 'k', text: 'remember' }), el('span', { class: 'v' }, el('span', { text: p.text }), del)));
    }
    const add = el('input', { class: 'label-input', type: 'text', placeholder: 'e.g. Prefer archive over delete', maxLength: 300 });
    const save = el('button', { class: 'btn btn-small', type: 'button', text: 'Remember' });
    save.addEventListener('click', async () => {
      if (!add.value.trim()) return;
      try { await api('POST', '/memory/preferences', { text: add.value }); renderMemory(box); } catch (e) { toast(e.message); }
    });
    box.append(el('div', { class: 'actions-row' }, add, save));
  }

  async function loadSettings() {
    const body = $('#settings-body');
    body.replaceChildren(el('p', { class: 'muted', text: 'Loading…' }));
    try {
      const [s, perms] = await Promise.all([api('GET', '/settings'), api('GET', '/permissions')]);
      body.replaceChildren(renderSettings(s, perms));
    } catch (e) { toast(e.message); }
  }

  // ---------- websocket --------------------------------------------------------------------
  function setConnected(on) {
    const d = $('#conn');
    d.classList.toggle('on', on);
    d.title = on ? 'connected to the core' : 'disconnected; reconnecting';
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    let ws;
    try { ws = new WebSocket(`${proto}://${location.host}/events`); }
    catch (e) { scheduleReconnect(); return; }
    state.ws = ws;
    ws.addEventListener('open', () => ws.send(JSON.stringify({ token: state.token })));
    ws.addEventListener('message', (m) => {
      let evt;
      try { evt = JSON.parse(m.data); } catch (e) { return; }
      if (evt.type === 'hello') {
        setConnected(true);
        state.wsAttempt = 0;
        setAgentState(evt);
        loadConversation().catch((e) => { if (e.message !== 'unauthorized') toast(e.message); });
        refreshChats();
        return;
      }
      renderEvent(evt);
    });
    ws.addEventListener('close', (e) => {
      setConnected(false);
      if (e.code === 1008) { showTokenScreen('The core rejected this token.'); return; }
      scheduleReconnect();
    });
    ws.addEventListener('error', () => { /* close follows */ });
  }
  function scheduleReconnect() {
    const delay = Math.min(10000, 1000 * Math.pow(2, state.wsAttempt++));
    setTimeout(connect, delay);
  }

  // ---------- wiring -----------------------------------------------------------------------
  function wire() {
    $('#btn-send').addEventListener('click', send);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    input.addEventListener('input', autosize);
    for (const b of [$('#btn-new'), $('#side-new')]) b.addEventListener('click', () => newChat('normal'));
    for (const b of $$('[data-new-mode]')) b.addEventListener('click', () => { hide('#more'); newChat(b.dataset.newMode); });
    $('#btn-more').addEventListener('click', () => {
      if ($('#more').classList.contains('hidden')) { refreshChats(); refreshActions(); show('#more'); } else hide('#more');
    });
    $('#btn-estop').addEventListener('click', async () => {
      try { setAgentState(await api('POST', '/stop')); toast('Stopped. The agent is paused until you resume.', 'info'); }
      catch (e) { toast(e.message); }
    });
    $('#btn-settings').addEventListener('click', () => showView(state.view === 'settings' ? 'chat' : 'settings'));
    $('#btn-back').addEventListener('click', () => showView('chat'));
    $('#more-audit').addEventListener('click', () => showView('audit'));
    for (const b of [$('#side-pause'), $('#more-pause'), $('#btn-resume')]) b.addEventListener('click', togglePause);
    for (const b of $$('.side-link')) b.addEventListener('click', () => showView(b.dataset.view));
    $('#audit-refresh').addEventListener('click', loadAudit);
    $('#audit-filter').addEventListener('input', renderAuditRows);
    $('#audit-event').addEventListener('change', renderAuditRows);
    for (const b of $$('[data-close]')) b.addEventListener('click', () => hide('#' + b.dataset.close));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { hide('#more'); if (state.view !== 'chat' && !wide()) showView('chat'); } });
    document.addEventListener('click', (e) => {
      const m = $('#more');
      if (!m.classList.contains('hidden') && !m.contains(e.target) && !$('#btn-more').contains(e.target)) hide('#more');
    });
    window.matchMedia('(min-width: 720px)').addEventListener('change', () => showView(state.view));
    $('#token-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const t = $('#token-input').value.trim();
      if (!t) return;
      store('agent_token', t);
      location.reload();
    });
    for (const b of $$('.suggestion')) b.addEventListener('click', () => { input.value = b.textContent; autosize(); send(); });
    if (shell) {
      show('#btn-mic');
      $('#btn-mic').addEventListener('click', () => shell.postMessage({ action: 'dictate' }));
      show('#btn-fullapp');
      $('#btn-fullapp').addEventListener('click', () => shell.postMessage({ action: 'openFullApp' }));
    }
  }

  function paintIcons() {
    for (const i of $$('i[data-icon]')) {
      const span = document.createElement('span');
      span.className = 'ic-wrap';
      span.innerHTML = window.icon(i.dataset.icon, Number(i.dataset.size || 16));
      i.replaceWith(span);
    }
  }

  async function init() {
    paintIcons();
    wire();
    state.token = discoverToken();
    if (!state.token) { showTokenScreen(); return; }
    try {
      const st = await api('GET', '/status');
      for (const b of [$('#mode-badge'), $('#mode-badge-side')]) b.classList.toggle('hidden', !st.fake);
      setAgentState(st);
      state.conversationId = await pickConversation();
      store('agent_conversation', state.conversationId);
      refreshActions();
      connect();
      input.focus();
    } catch (e) {
      if (e.message !== 'unauthorized') toast(e.message);
    }
  }

  init();
})();
