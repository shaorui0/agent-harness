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
    summaries: {}, pending: {}, sumError: {},
    timers: new Set(), alive: true, ctx: api.context || {},
  };
  const root = h('div');
  container.replaceChildren(root);

  const isDark = () => {
    const c = state.ctx || {};
    return c.isDarkMode === true || c.theme === 'dark';
  };

  async function load() {
    try {
      state.board = await api.rpc('GET', '/api/board');
      state.error = null;
    } catch (e) {
      state.error = String((e && e.message) || e);
    }
    render();
  }

  function stopPoll(sid) {
    const t = state.pending[sid];
    if (t) { clearInterval(t.timer); state.timers.delete(t.timer); }
    delete state.pending[sid];
  }

  async function pollOnce(sid) {
    try {
      const r = await api.rpc('GET', '/api/summary?session_id=' + encodeURIComponent(sid));
      if (!state.alive) return;
      if (r.summary) state.summaries[sid] = r.summary;
      if (r.error) state.sumError[sid] = r.error; else delete state.sumError[sid];
      if (!r.pending) stopPoll(sid);
    } catch (e) {
      state.sumError[sid] = String((e && e.message) || e);
      stopPoll(sid);
    }
    render();
  }

  async function generate(sid) {
    if (state.pending[sid]) return;
    delete state.sumError[sid];
    const t = { n: 0 };
    t.timer = setInterval(() => {
      if (++t.n > 45) { stopPoll(sid); state.sumError[sid] = '生成超时'; render(); return; }
      pollOnce(sid);
    }, 2000);
    state.timers.add(t.timer);
    state.pending[sid] = t;
    render();
    try {
      await api.rpc('POST', '/api/summary', { session_id: sid });
    } catch (e) {
      stopPoll(sid);
      state.sumError[sid] = String((e && e.message) || e);
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

  function checklist(ch) {
    const wrap = h('div', { style: 'margin-bottom:12px' });
    const p = ch.progress || {};
    wrap.append(h('div', { style: 'font-weight:600;margin-bottom:4px' }, ch.title || ch.id,
      h('span', { style: 'color:var(--mut);font-weight:400;margin-left:8px' }, `${p.done || 0}/${p.total || 0}`)));
    let lastGroup = null;
    const ul = h('div', { style: 'font-family:ui-monospace,Menlo,monospace;font-size:12.5px;line-height:1.7' });
    for (const it of ch.items || []) {
      if (it.group && it.group !== lastGroup) {
        lastGroup = it.group;
        ul.append(h('div', { style: 'color:var(--mut);margin-top:6px;font-family:inherit' }, it.group));
      }
      const style = it.done ? 'color:var(--mut)' : it.current ? 'color:var(--warn);font-weight:600' : '';
      ul.append(h('div', { style },
        `${it.done ? '[x]' : '[ ]'} `, it.id ? it.id + ' ' : '', it.text, it.current ? '  ← 正在这条' : ''));
    }
    wrap.append(ul);
    return wrap;
  }

  function summaryBlock(s) {
    const sid = s.session_id;
    const sum = state.summaries[sid] || s.summary;
    const pending = !!state.pending[sid] || s.summary_pending;
    const btn = h('button', {
      style: 'cursor:pointer;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font-size:12.5px',
      onclick: () => generate(sid),
    }, pending ? '生成中…' : '↻ 生成摘要');
    if (pending) btn.disabled = true;
    const box = section('Summary', btn);
    if (state.sumError[sid]) box.append(h('div', { style: 'color:#dc2626;margin-top:6px' }, state.sumError[sid]));
    if (!sum) {
      box.append(h('div', { style: 'color:var(--mut);margin-top:6px' }, '尚未生成。摘要只在点按钮时生成。'));
      return box;
    }
    if (sum.stale) {
      box.append(h('span', { style: 'margin-left:8px;color:var(--warn);font-size:12px' }, '已过期：session 之后又有新内容'));
    }
    const list = (label, arr, mark) => arr && arr.length
      ? h('div', { style: 'margin-top:8px' }, h('div', { style: 'font-weight:600' }, label),
        ...arr.map((t) => h('div', { style: 'margin-left:4px' }, `${mark} ${t}`)))
      : null;
    box.append(list('已完成', sum.done, '[x]'), list('还剩', sum.remaining, '[ ]'));
    if (sum.blocked_on) box.append(h('div', { style: 'margin-top:8px;color:var(--warn)' }, '卡在：' + sum.blocked_on));
    if (sum.diff_note) box.append(h('div', { style: 'margin-top:8px;color:var(--mut)' }, sum.diff_note));
    box.append(h('div', { style: 'margin-top:6px;color:var(--mut);font-size:11.5px' },
      `生成于 ${sum.generated_at || '?'}${sum.model ? ' · ' + sum.model : ''}`));
    return box;
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
        d.files_changed != null ? h('span', null, `diff ${d.files_changed} 文件 +${d.insertions || 0} / −${d.deletions || 0}`) : null),
      s.last_action ? h('div', { style: 'margin-top:6px;color:var(--mut)' }, '最近：' + s.last_action) : null);
    const ids = s.change_ids || [];
    const changes = ids.map((id) => (ws.changes || []).find((c) => c.id === id)).filter(Boolean);
    if (changes.length) out.append(section('任务清单', ...changes.map(checklist)));
    else out.append(section('任务清单', h('div', { style: 'color:var(--mut)' }, '无 spec（该 session 没有关联的 OpenSpec change）。')));
    out.append(summaryBlock(s));
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
    const chs = (ws.changes || []).map((c) => checklist(c));
    if (chs.length) out.append(section('Changes', ...chs));
    return out;
  }

  function render() {
    if (!state.alive) return;
    const vars = THEMES[isDark() ? 'dark' : 'light'];
    root.style.cssText = Object.entries(vars).map(([k, v]) => `${k}:${v}`).join(';') +
      ';background:var(--bg);color:var(--fg);padding:16px;min-height:100%;box-sizing:border-box;font:13.5px/1.5 system-ui,sans-serif';
    const kids = [];
    if (state.error && !state.board) {
      kids.push(h('div', { style: 'color:#dc2626' }, '无法读取 tracker：' + state.error),
        h('div', { style: 'color:var(--mut);margin-top:6px' }, '检查 harness tracker 是否在运行，或在终端运行 harness doctor。'));
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
  }

  const unsub = api.onContextChange ? api.onContextChange((ctx) => { state.ctx = ctx; state.pick = null; render(); }) : null;
  const refresh = setInterval(load, 30000);
  state.timers.add(refresh);
  container.__harnessTracker = { state, unsub };
  render();
  load();
}

export function unmount(container) {
  const inst = container && container.__harnessTracker;
  if (inst) {
    inst.state.alive = false;
    for (const t of inst.state.timers) clearInterval(t);
    inst.state.timers.clear();
    if (typeof inst.unsub === 'function') inst.unsub();
    delete container.__harnessTracker;
  }
  if (container) container.replaceChildren();
}
