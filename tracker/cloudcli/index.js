// CloudCLI "Tracker" tab. Self-contained ES module (CloudCLI imports it via a blob URL,
// so no relative imports). All API text goes through textContent, never innerHTML.

const PHASE = {
  needs_input: ['#d97706', '在等你输入'],
  running: ['#16a34a', '运行中'],
  stale: ['#dc2626', '疑似卡住'],
  done: ['#2563eb', '已完成'],
  exited: ['#6b7280', '已退出'],
};

const THEMES = {
  light: { '--bg': '#ffffff', '--fg': '#1f2328', '--mut': '#6b7280', '--line': '#e5e7eb', '--card': '#f6f8fa', '--acc': '#2563eb', '--warn': '#b45309', '--ok': '#15803d' },
  dark: { '--bg': '#0d1117', '--fg': '#e6edf3', '--mut': '#8b949e', '--line': '#30363d', '--card': '#161b22', '--acc': '#58a6ff', '--warn': '#e3b341', '--ok': '#3fb950' },
};

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (k === 'style') el.style.cssText = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) el.setAttribute(k, v);
    }
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function fmtAge(s) {
  if (s == null) return '';
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 129600) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function normPath(p) {
  return String(p || '').replace(/\/+$/, '');
}

// session.id first, then the workspace whose path is the longest prefix of project.path.
function locate(board, ctx) {
  const wss = (board && board.workspaces) || [];
  const sid = ctx.session && ctx.session.id;
  if (sid) {
    for (const ws of wss) {
      const s = (ws.sessions || []).find((x) => x.session_id === sid);
      if (s) return { ws, session: s };
    }
  }
  const pp = normPath(ctx.project && ctx.project.path);
  let best = null;
  if (pp) {
    for (const ws of wss) {
      const wp = normPath(ws.path);
      if (!wp) continue;
      if (pp === wp || pp.startsWith(wp + '/') || wp.startsWith(pp + '/')) {
        if (!best || wp.length > normPath(best.path).length) best = ws;
      }
    }
  }
  return { ws: best, session: null };
}

export function mount(container, api) {
  const state = {
    board: null, error: null, pick: null, // pick: session id chosen from the overview
    checks: {}, checkPending: {}, checkError: {},
    recaps: {}, recapPending: {}, recapError: {}, expanded: {},
    timers: new Set(), alive: true, ctx: api.context || {},
    usage: null,
    narrow: false, // container width <= 700 (ResizeObserver); the only switch for the phone layout
  };
  const root = h('div');
  container.replaceChildren(root);

  const NARROW_MAX = 700;
  const widthOf = () => (root.clientWidth || (container.getBoundingClientRect && container.getBoundingClientRect().width) || 0);
  state.narrow = widthOf() > 0 && widthOf() <= NARROW_MAX;
  let ro = null;
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver((entries) => {
      const w = entries && entries[0] ? entries[0].contentRect.width : widthOf();
      if (!(w > 0)) return;
      const n = w <= NARROW_MAX;
      if (n !== state.narrow) { state.narrow = n; render(); } // re-render only when the layout flips
    });
    ro.observe(root);
  }

  const isDark = () => {
    const c = state.ctx || {};
    return c.isDarkMode === true || c.theme === 'dark';
  };

  // Plan-usage bar (5h / weekly). Separate request: /api/usage may take a few seconds on a cold cache.
  async function loadUsage() {
    try {
      state.usage = await api.rpc('GET', '/api/usage');
    } catch (_) {
      state.usage = null;
    }
    if (state.alive) render();
  }

  function usageBar() {
    const u = state.usage;
    if (!u) return null;
    const ths = u.thresholds || [70, 90];
    const shown = (u.windows || []).filter((w) => w.id === 'five_hour' || w.id === 'seven_day');
    if (!shown.length) return h('div', { style: 'font-size:12px;color:var(--mut);margin-bottom:8px', title: u.error || '' }, '额度取不到');
    const row = h('div', { style: 'display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;font-size:12px;color:var(--mut);margin-bottom:10px' });
    for (const w of shown) {
      const color = w.percent >= ths[ths.length - 1] ? '#dc2626' : w.percent >= ths[0] ? 'var(--warn)' : 'var(--ok)';
      const track = h('span', { style: 'position:relative;display:inline-block;width:84px;height:8px;border-radius:4px;background:var(--card);border:1px solid var(--line);overflow:hidden' },
        h('i', { style: `position:absolute;left:0;top:0;bottom:0;width:${Math.min(100, w.percent)}%;background:${color}` }),
        ...ths.map((t) => h('i', { style: `position:absolute;top:0;bottom:0;width:1px;left:${t}%;background:var(--mut);opacity:.7` })));
      const tip = `${w.label} ${w.percent}%` + (w.resets_text ? `，重置于 ${w.resets_text}` : '') + (u.stale ? '（数据可能过期）' : '');
      row.append(h('span', { style: 'display:inline-flex;align-items:center;gap:6px;white-space:nowrap', title: tip },
        w.id === 'five_hour' ? '5h' : '周', track, `${Math.round(w.percent)}%${u.stale ? '?' : ''}`));
    }
    return row;
  }

  async function load() {
    try {
      state.board = await api.rpc('GET', '/api/board');
      state.error = null;
    } catch (e) {
      state.error = String((e && e.message) || e);
    }
    render();
  }


  function stopCheck(sid) {
    const t = state.checkPending[sid];
    if (t) { clearInterval(t.timer); state.timers.delete(t.timer); }
    delete state.checkPending[sid];
  }

  async function checkPollOnce(sid) {
    try {
      const r = await api.rpc('GET', '/api/check?session_id=' + encodeURIComponent(sid));
      if (!state.alive) return;
      if (r.check) state.checks[sid] = r.check;
      if (r.error) state.checkError[sid] = r.error; else delete state.checkError[sid];
      if (!r.pending) stopCheck(sid);
    } catch (e) {
      state.checkError[sid] = String((e && e.message) || e);
      stopCheck(sid);
    }
    render();
  }

  // One request per session, shared by every change block of that session.
  async function runCheck(sid) {
    if (state.checkPending[sid]) return;
    delete state.checkError[sid];
    const t = { n: 0 };
    t.timer = setInterval(() => {
      if (++t.n > 75) { stopCheck(sid); state.checkError[sid] = '核对超时（150s）'; render(); return; }
      checkPollOnce(sid);
    }, 2000);
    state.timers.add(t.timer);
    state.checkPending[sid] = t;
    render();
    try {
      await api.rpc('POST', '/api/check', { session_id: sid });
    } catch (e) {
      stopCheck(sid);
      state.checkError[sid] = String((e && e.message) || e);
      render();
    }
  }

  function stopRecap(sid) {
    const t = state.recapPending[sid];
    if (t) { clearInterval(t.timer); state.timers.delete(t.timer); }
    delete state.recapPending[sid];
  }

  async function recapPollOnce(sid) {
    try {
      const r = await api.rpc('GET', '/api/recap?session_id=' + encodeURIComponent(sid));
      if (!state.alive) return;
      if (r.recap) state.recaps[sid] = r.recap;
      if (r.error) state.recapError[sid] = r.error; else delete state.recapError[sid];
      if (!r.pending) stopRecap(sid);
    } catch (e) {
      state.recapError[sid] = String((e && e.message) || e);
      stopRecap(sid);
    }
    render();
  }

  async function runRecap(sid) {
    if (state.recapPending[sid]) return;
    delete state.recapError[sid];
    const t = { n: 0 };
    t.timer = setInterval(() => {
      if (++t.n > 80) { stopRecap(sid); state.recapError[sid] = '生成超时（240s）'; render(); return; }
      recapPollOnce(sid);
    }, 3000);
    state.timers.add(t.timer);
    state.recapPending[sid] = t;
    render();
    try {
      await api.rpc('POST', '/api/recap', { session_id: sid });
    } catch (e) {
      stopRecap(sid);
      state.recapError[sid] = String((e && e.message) || e);
      render();
    }
  }

  function section(title, ...kids) {
    return h('div', { style: 'margin-top:16px' },
      h('div', { style: 'font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px' }, title),
      ...kids);
  }

  function phaseBadge(s) {
    const [color, label] = PHASE[s.phase] || ['#6b7280', s.phase || '?'];
    return h('span', { style: 'display:inline-flex;align-items:center;gap:6px;font-weight:600' },
      h('span', { style: `width:9px;height:9px;border-radius:50%;background:${color};display:inline-block` }), label);
  }

  const MONO = 'font-family:ui-monospace,Menlo,monospace;font-size:12.5px;line-height:1.7';

  const VERDICT = {
    ok: ['✓', 'var(--mut)', ''],
    unsupported: ['⚠ 无证据', 'var(--warn)', ''],
    unticked_done: ['⏳ 似乎已做完', 'var(--acc)', ''],
  };

  // One task row (group): "[x] title  n/m", children indented. rows: [{id,done,text,level,current}]
  function taskRow(title, done, count, rows, marks) {
    const wrap = h('div', { style: 'margin-top:4px' });
    const sum = h('div', { style: MONO + ';font-weight:600' + (done ? ';color:var(--mut)' : '') },
      `${done ? '[x]' : '[ ]'} ${title}`,
      count ? h('span', { style: 'color:var(--mut);font-weight:400;margin-left:8px' }, count) : null);
    const needOpen = rows.some((r) => marks && marks[r.id] && marks[r.id].verdict !== 'ok');
    if (done && rows.length && !needOpen) {
      const d = h('details', null, h('summary', { style: 'cursor:pointer;list-style:none' }, sum));
      for (const r of rows) d.append(...subRow(r, marks));
      wrap.append(d);
    } else {
      wrap.append(sum);
      for (const r of rows) wrap.append(...subRow(r, marks));
    }
    return wrap;
  }

  // Returns [row, optional evidence line]. mark = check verdict for this item.
  function subRow(r, marks) {
    const m = marks && r.id ? marks[r.id] : null;
    const style = MONO + `;margin-left:${14 + 14 * (r.level || 0)}px` +
      (r.done ? ';color:var(--mut)' : r.current ? ';color:var(--warn);font-weight:600' : '');
    const v = m && VERDICT[m.verdict];
    const row = h('div', { style, title: m && m.verdict === 'ok' && m.evidence ? m.evidence : null },
      `${r.done ? '[x]' : '[ ]'} `, r.id ? r.id + ' ' : '', r.text, r.current ? '  ← 正在这条' : '',
      v ? h('span', { style: `margin-left:8px;font-weight:400;color:${v[1]}` }, v[0]) : null);
    const out = [row];
    if (m && m.verdict !== 'ok' && m.evidence) {
      out.push(h('div', { style: `font-size:11.5px;color:var(--mut);margin-left:${14 + 14 * (r.level || 0) + 22}px;line-height:1.4` }, m.evidence));
    }
    return out;
  }

  const BTN = 'cursor:pointer;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font-size:12.5px';
  const PRE = 'margin:6px 0;padding:8px 10px;background:var(--card);border:1px solid var(--line);border-radius:6px;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;overflow-x:auto';

  function sectionHead(title, ...right) {
    return h('div', { style: 'display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:16px;margin-bottom:6px' },
      h('div', { style: 'font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em' }, title),
      h('div', { style: 'display:flex;align-items:center;gap:8px' }, ...right));
  }

  function checkFor(sid, s) {
    return state.checks[sid] || (s && s.check) || null;
  }

  // Free execution status: one row per session linked to this change.
  function execRows(ws, ch, chk) {
    const out = h('div', { style: 'margin:4px 0 6px;padding:6px 8px;background:var(--card);border:1px solid var(--line);border-radius:6px' });
    for (const sid of ch.session_ids || []) {
      const s = (ws.sessions || []).find((x) => x.session_id === sid);
      if (!s) continue;
      out.append(h('div', { style: 'display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center' },
        phaseBadge(s), h('span', null, s.name || sid.slice(0, 8)),
        s.phase_age_s != null ? h('span', { style: 'color:var(--mut)' }, fmtAge(s.phase_age_s)) : null));
    }
    if (chk && chk.blocked_on) {
      out.append(h('div', { style: 'margin-top:4px;color:var(--warn)' }, '⏸ 在等：' + chk.blocked_on));
    }
    return out;
  }

  // OpenSpec change block: header (+ check button) / exec status / tree with check marks / off_spec.
  // sess = the session being viewed (null in the workspace overview: no button, no marks).
  function changeBlock(ws, ch, sess) {
    const wrap = h('div', { style: 'margin-bottom:14px' });
    const p = ch.progress || {};
    const sid = sess && sess.session_id;
    const chk = sess ? checkFor(sid, sess) : null;
    const pending = sess ? (!!state.checkPending[sid] || !!sess.check_pending) : false;
    const right = [];
    if (sess && !state.narrow) {
      const btn = h('button', { style: BTN, onclick: () => runCheck(sid) }, pending ? '核对中…' : chk ? '重新核对' : '核对勾选');
      if (pending) btn.disabled = true;
      right.push(h('span', { style: 'color:var(--mut);font-size:12px' }, '点按钮才调用 · sonnet'), btn);
    }
    wrap.append(h('div', { style: 'display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap' },
      h('div', { style: 'font-weight:600' }, ch.title || ch.id,
        h('span', { style: 'color:var(--mut);font-weight:400;margin-left:8px' }, `${p.done || 0}/${p.total || 0}`)),
      h('div', { style: 'display:flex;align-items:center;gap:8px' }, ...right)));
    if (sess && state.checkError[sid]) wrap.append(h('div', { style: 'color:#dc2626;margin-top:4px' }, state.checkError[sid]));
    wrap.append(execRows(ws, ch, chk));
    const marks = {};
    const cc = chk && (chk.changes || []).find((c) => c.id === ch.id);
    for (const it of (cc && cc.items) || []) marks[it.id] = it;
    if (chk && chk.stale) wrap.append(h('div', { style: 'color:var(--warn);font-size:12px' }, '已过期：session 之后又有新内容'));
    const groups = [];
    for (const it of ch.items || []) {
      const g = it.group || null;
      const last = groups[groups.length - 1];
      if (last && last.g === g) last.items.push(it); else groups.push({ g, items: [it] });
    }
    for (const { g, items } of groups) {
      if (g === null) { for (const it of items) wrap.append(...subRow({ ...it, level: it.level || 0 }, marks)); continue; }
      const n = items.filter((x) => x.done).length;
      wrap.append(taskRow(g, n === items.length, `${n}/${items.length}`, items, marks));
    }
    if (chk && (chk.off_spec || []).length) {
      wrap.append(h('div', { style: 'margin-top:8px;font-size:12px;font-weight:600;color:var(--mut)' }, '规格外的工作（考虑补进 tasks.md）'));
      for (const t of chk.off_spec) wrap.append(h('div', { style: 'margin-left:14px' }, '• ' + t));
    }
    if (chk) {
      wrap.append(h('div', { style: 'margin-top:6px;color:var(--mut);font-size:11.5px' },
        `核对于 ${chk.generated_at || '?'}${chk.model ? ' · ' + chk.model : ''}`));
    }
    return wrap;
  }

  // Collapsed by default: the only place last_text / last_tool show up.
  function processDetails(s) {
    const lastText = s.last_text || (!s.last_tool ? s.last_action : null);
    if (!lastText && !s.last_tool) return null;
    const d = h('details', { style: 'margin-top:16px' },
      h('summary', { style: 'cursor:pointer;font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em' }, '过程细节'));
    if (lastText) {
      d.append(h('div', { style: 'color:var(--mut);font-size:12px;margin-top:6px' }, '最近一句话'),
        h('div', { style: 'white-space:pre-wrap;word-break:break-word' }, lastText));
    }
    if (s.last_tool) {
      d.append(h('div', { style: 'color:var(--mut);font-size:12px;margin-top:8px' }, '最近动作'),
        h('div', { style: 'white-space:pre-wrap;word-break:break-word;' + MONO }, s.last_tool));
    }
    return d;
  }

  function copyBtn(text) {
    const b = h('button', { style: BTN + ';padding:2px 8px;font-size:11.5px' }, '复制');
    b.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(text); b.textContent = '已复制'; } catch (e) { b.textContent = '复制失败'; }
      setTimeout(() => { b.textContent = '复制'; }, 1500);
    });
    return b;
  }

  // Inline markdown: **bold** and `code` only; everything else is plain text. No innerHTML.
  function inline(text) {
    const frag = document.createDocumentFragment();
    for (const part of String(text).split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/)) {
      if (!part) continue;
      if (/^\*\*[^*\n]+\*\*$/.test(part)) {
        const el = document.createElement('strong'); el.textContent = part.slice(2, -2); frag.append(el);
      } else if (/^`[^`\n]+`$/.test(part)) {
        const el = document.createElement('code');
        el.style.cssText = 'font:12px ui-monospace,Menlo,monospace;background:var(--card);border:1px solid var(--line);border-radius:4px;padding:0 4px;word-break:break-all;white-space:pre-wrap';
        el.textContent = part.slice(1, -1); frag.append(el);
      } else frag.append(document.createTextNode(part));
    }
    return frag;
  }

  // Minimal markdown: "## " heading, "- "/"* "/"1. " list, ``` fenced code (no inline inside), paragraphs.
  function renderMd(md) {
    const out = h('div');
    const lines = String(md || '').split('\n');
    let para = [];
    const flush = () => {
      if (para.length) {
        const p = h('div', { style: 'white-space:pre-wrap;word-break:break-word;margin:4px 0' });
        para.forEach((l, i) => { if (i) p.append('\n'); p.append(inline(l)); });
        out.append(p);
      }
      para = [];
    };
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      let m;
      if (/^\s*```/.test(ln)) {
        flush();
        const code = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
        const text = code.join('\n');
        out.append(h('div', { style: 'position:relative' },
          h('pre', { style: PRE }, text),
          h('div', { style: 'position:absolute;top:10px;right:6px' }, copyBtn(text))));
      } else if (/^##+\s/.test(ln)) {
        flush();
        out.append(h('div', { style: 'font-weight:700;margin-top:12px' }, inline(ln.replace(/^#+\s*/, ''))));
      } else if ((m = ln.match(/^(\s*)([-*]|\d+[.)])\s+(.*)$/))) {
        flush();
        const bullet = /\d/.test(m[2]) ? m[2] : '•';
        const indent = 4 + Math.min(m[1].length, 8) * 4;
        out.append(h('div', { style: `margin:2px 0 2px ${indent}px;padding-left:18px;text-indent:-18px;white-space:pre-wrap;word-break:break-word` }, bullet + ' ', inline(m[3])));
      } else if (!ln.trim()) {
        flush();
      } else {
        para.push(ln);
      }
    }
    flush();
    return out;
  }

  function recapBlock(s) {
    const sid = s.session_id;
    const rc = state.recaps[sid] || s.recap;
    const pending = !!state.recapPending[sid] || s.recap_pending;
    const btn = h('button', { style: BTN, onclick: () => runRecap(sid) }, pending ? '生成中…' : rc ? '重新生成' : '▶ 生成 Session Summary');
    if (pending) btn.disabled = true;
    const box = h('div', null, state.narrow ? sectionHead('Session Summary') : sectionHead('Session Summary',
      h('span', { style: 'color:var(--mut);font-size:12px' }, '按 session_recap 流程 · 另起进程 · 不写入原 session'), btn));
    if (state.recapError[sid]) box.append(h('div', { style: 'color:#dc2626;margin-top:6px' }, state.recapError[sid]));
    if (!rc) {
      box.append(h('div', { style: 'color:var(--mut)' }, state.narrow ? '尚未生成。' : '尚未生成。只在点按钮时执行（约 1-3 分钟）。'));
      return box;
    }
    if (rc.stale) box.append(h('div', { style: 'color:var(--warn);font-size:12px' }, '已过期：session 之后又有新内容'));
    if (rc.skill_missing) box.append(h('div', { style: 'color:var(--warn);font-size:12px' }, '未找到 session_recap skill，使用了内置的最小说明'));
    box.append(renderMd(rc.markdown),
      h('div', { style: 'margin-top:6px;color:var(--mut);font-size:11.5px' }, `生成于 ${rc.generated_at || '?'}${rc.model ? ' · ' + rc.model : ''}`));
    return box;
  }


  const LIVE = new Set(['running', 'needs_input', 'stale']);
  function openBtn(s) {
    const sid = s.session_id;
    const label = '在 CloudCLI 打开';
    const live = LIVE.has(s.phase);
    const b = h('button', { style: BTN + ';font-weight:600', 'data-role': 'open-cloudcli' }, label);
    let armed = null;
    b.addEventListener('click', () => {
      if (live && !armed) {
        b.textContent = '还在运行，再点一次确认打开';
        armed = setTimeout(() => { armed = null; b.textContent = label; }, 4000);
        state.timers.add(armed);
        return;
      }
      if (armed) { clearTimeout(armed); state.timers.delete(armed); armed = null; }
      location.assign(location.origin + '/session/' + encodeURIComponent(sid));
    });
    return b;
  }

  function copyResumeBtn(s) {
    const text = s.cwd ? `cd ${s.cwd} && claude --resume ${s.session_id}` : null;
    if (!text) return null;
    const b = h('button', { style: BTN, 'data-role': 'copy-resume' }, '复制 resume 命令');
    b.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(text); b.textContent = '已复制'; } catch (e) { b.textContent = '复制失败'; }
      setTimeout(() => { b.textContent = '复制 resume 命令'; }, 1500);
    });
    return b;
  }

  function sessionView(ws, s) {
    const out = h('div');
    const d = s.diff || {};
    out.append(
      h('div', { style: 'font-size:16px;font-weight:700' }, s.name || s.session_id),
      h('div', { style: 'margin-top:6px;display:flex;flex-wrap:wrap;gap:6px 16px' },
        phaseBadge(s),
        s.phase_age_s != null ? h('span', { style: 'color:var(--mut)' }, `持续 ${fmtAge(s.phase_age_s)}`) : null,
        s.branch ? h('span', null, '分支 ' + s.branch) : null,
        d.files_changed != null ? h('span', null, `diff ${d.files_changed} 文件 +${d.insertions || 0} / −${d.deletions || 0}`) : null));
    out.append(h('div', { style: 'margin-top:10px;display:flex;flex-wrap:wrap;gap:8px' }, openBtn(s), copyResumeBtn(s)));
    const ids = s.change_ids || [];
    const changes = ids.map((id) => (ws.changes || []).find((c) => c.id === id)).filter(Boolean);
    if (changes.length) out.append(section('任务进度（OpenSpec）', ...changes.map((c) => changeBlock(ws, c, s))));
    else out.append(section('任务进度', h('div', { style: 'color:var(--mut)' }, '没有 spec：在这个 session 里用 /opsx:new 建一个 change，或用 /harness-spec-backfill 补登')));
    out.append(recapBlock(s));
    const pd = processDetails(s);
    if (pd) out.append(pd);
    if (s.resume_cmd) {
      out.append(section('Resume', h('code', { style: 'display:block;padding:6px 8px;background:var(--card);border:1px solid var(--line);border-radius:6px;font-size:12px;overflow-x:auto;white-space:nowrap' }, s.resume_cmd)));
    }
    return out;
  }

  function overview(ws) {
    const out = h('div');
    out.append(h('div', { style: 'font-size:16px;font-weight:700' }, ws.name),
      h('div', { style: 'color:var(--mut);font-size:12px' }, `${(ws.sessions || []).length} 个 session · ${(ws.changes || []).length} 个 change`));
    const rows = (ws.sessions || []).map((s) => h('div', {
      style: 'display:flex;gap:10px;align-items:center;padding:6px 8px;border:1px solid var(--line);border-radius:6px;margin-bottom:4px;cursor:pointer;background:var(--card)',
      onclick: () => { state.pick = s.session_id; render(); },
    }, phaseBadge(s), h('span', { style: 'flex:1' }, s.name || s.session_id),
      s.phase_age_s != null ? h('span', { style: 'color:var(--mut);font-size:12px' }, fmtAge(s.phase_age_s)) : null));
    out.append(section('Sessions', rows.length ? rows : h('div', { style: 'color:var(--mut)' }, '无 session')));
    const chs = (ws.changes || []).map((c) => changeBlock(ws, c, null));
    if (chs.length) out.append(section('Changes', ...chs));
    return out;
  }

  const RANK = { needs_input: 0, stale: 1, running: 2, done: 3, exited: 4 };
  const rankOf = (s) => (s.phase in RANK ? RANK[s.phase] : 9);
  function sortedSessions(ws) {
    return [...(ws.sessions || [])].sort((a, b) => rankOf(a) - rankOf(b) ||
      (a.phase_age_s == null ? Infinity : a.phase_age_s) - (b.phase_age_s == null ? Infinity : b.phase_age_s));
  }

  // Phone overview: every workspace, stacked and grouped; cards sorted within each workspace.
  function narrowOverview(board) {
    const out = h('div');
    const wss = board.workspaces || [];
    for (const ws of wss) {
      const list = sortedSessions(ws);
      const grp = h('div', { style: 'margin-bottom:18px', 'data-ws': ws.name });
      grp.append(h('div', { style: 'font-size:16px;font-weight:700;word-break:break-word' }, ws.name),
        h('div', { style: 'color:var(--mut);font-size:12px;margin-bottom:6px' }, `${list.length} 个 session`));
      for (const s of list) {
        grp.append(h('div', {
          style: 'display:flex;gap:10px;align-items:center;min-height:48px;box-sizing:border-box;padding:8px 12px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px;cursor:pointer;background:var(--card)',
          'data-sid': s.session_id,
          onclick: () => { state.pick = s.session_id; render(); },
        }, phaseBadge(s), h('span', { style: 'flex:1;min-width:0;word-break:break-word' }, s.name || s.session_id),
          s.phase_age_s != null ? h('span', { style: 'color:var(--mut);font-size:12px' }, fmtAge(s.phase_age_s)) : null));
      }
      if (!list.length) grp.append(h('div', { style: 'color:var(--mut)' }, '无 session'));
      out.append(grp);
    }
    if (!wss.length) out.append(h('div', { style: 'color:var(--mut)' }, '没有 workspace'));
    return out;
  }

  // Phone layout: always overview first (ctx.session ignored); detail only after a card is picked.
  function narrowKids() {
    const board = state.board;
    if (state.pick) {
      for (const w of board.workspaces || []) {
        const s = (w.sessions || []).find((x) => x.session_id === state.pick);
        if (s) {
          return [h('a', { style: 'cursor:pointer;color:var(--acc);font-size:14px;display:flex;align-items:center;min-height:44px', onclick: () => { state.pick = null; render(); } }, '← 返回总览'),
            sessionView(w, s)];
        }
      }
      state.pick = null;
    }
    return [narrowOverview(board)];
  }

  function tapTargets() {
    for (const el of root.querySelectorAll('button,a,summary')) {
      el.style.minHeight = '44px';
      el.style.boxSizing = 'border-box';
    }
  }

  function render() {
    if (!state.alive) return;
    const vars = THEMES[isDark() ? 'dark' : 'light'];
    root.style.cssText = Object.entries(vars).map(([k, v]) => `${k}:${v}`).join(';') +
      ';background:var(--bg);color:var(--fg);padding:16px;min-height:100%;box-sizing:border-box;font:13.5px/1.5 system-ui,sans-serif';
    const kids = [];
    const ub = usageBar();
    if (ub) kids.push(ub);
    if (state.error && !state.board) {
      kids.push(h('div', { style: 'color:#dc2626' }, '无法读取 tracker：' + state.error),
        h('div', { style: 'color:var(--mut);margin-top:6px' }, '检查 harness tracker 是否在运行，或在终端运行 harness doctor。'));
    } else if (state.board && state.narrow) {
      kids.push(...narrowKids());
    } else if (state.board) {
      let { ws, session } = locate(state.board, state.ctx);
      let back = null;
      if (state.pick) {
        for (const w of state.board.workspaces || []) {
          const s = (w.sessions || []).find((x) => x.session_id === state.pick);
          if (s) { ws = w; session = s; back = true; }
        }
      }
      if (back) {
        kids.push(h('a', { style: 'cursor:pointer;color:var(--acc);font-size:12px', onclick: () => { state.pick = null; render(); } }, '← 返回 workspace 总览'));
      }
      if (ws && session) {
        kids.push(sessionView(ws, session));
      } else if (ws) {
        if (state.ctx.session) {
          kids.push(h('div', { style: 'color:var(--mut);margin-bottom:8px' }, '当前 session 未被 tracker 识别，显示所在 workspace 总览。'));
        }
        kids.push(overview(ws));
      } else {
        kids.push(h('div', { style: 'color:var(--mut)' }, '当前项目不在 tracker 的任何 workspace 中。'));
        for (const w of state.board.workspaces || []) kids.push(h('div', { style: 'margin-top:4px' }, `${w.name}（${(w.sessions || []).length} 个 session）`));
      }
    } else {
      kids.push(h('div', { style: 'color:var(--mut)' }, '加载中…'));
    }
    root.replaceChildren(...kids);
    if (state.narrow) tapTargets();
  }

  const unsub = api.onContextChange ? api.onContextChange((ctx) => { state.ctx = ctx; state.pick = null; render(); }) : null;
  const refresh = setInterval(load, 30000);
  state.timers.add(refresh);
  const usageTimer = setInterval(loadUsage, 120000);
  state.timers.add(usageTimer);
  container.__harnessTracker = { state, unsub, ro };
  render();
  load();
  loadUsage();
}

export function unmount(container) {
  const inst = container && container.__harnessTracker;
  if (inst) {
    inst.state.alive = false;
    for (const t of inst.state.timers) clearInterval(t);
    inst.state.timers.clear();
    if (typeof inst.unsub === 'function') inst.unsub();
    if (inst.ro) inst.ro.disconnect();
    delete container.__harnessTracker;
  }
  if (container) container.replaceChildren();
}
