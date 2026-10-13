'use strict';

(() => {
  const DEFAULT_CHANNEL = 'https://conda.biocpkgcontainers.org';
  const STOPPED = new Set(['skipped', 'unresolvable', 'failed', 'blocked']);
  const NAME_LIST_LIMIT = 24;

  const GROUPS = [
    { id: 'installable', label: 'Installable', tone: 'ok' },
    { id: 'builtdeps', label: 'Built, blocked', tone: 'warn' },
    { id: 'progress', label: 'In progress', tone: 'info' },
    { id: 'failed', label: 'Build failed', tone: 'bad' },
    { id: 'blocked', label: 'Blocked', tone: 'warn' },
    { id: 'skipped', label: 'Skipped', tone: 'muted' },
    { id: 'unavailable', label: 'Unavailable', tone: 'void' },
  ];
  const GROUP_IDS = new Set(GROUPS.map((g) => g.id));

  const REPOS = [
    { repo: 'bioc', key: 'software', label: 'Software' },
    { repo: 'data/annotation', key: 'annotation', label: 'Annotation' },
    { repo: 'data/experiment', key: 'experiment', label: 'Experiment' },
    { repo: 'workflows', key: 'workflows', label: 'Workflow' },
  ];
  const REPO_BY_KEY = new Map(REPOS.map((r) => [r.key, r]));
  const REPO_LABEL = new Map([...REPOS.map((r) => [r.repo, r.label]), ['cran', 'CRAN']]);

  const STAGES = [
    { id: 'recipe', text: 'rendering the recipe', short: 'Recipe' },
    { id: 'build', text: 'building and testing', short: 'Build and test' },
    { id: 'package', text: 'checking the package files', short: 'Package files' },
    { id: 'verify', text: 'verifying the artifact', short: 'Artifact' },
    { id: 'check', text: 'checking the package before publishing', short: 'Final check' },
  ];

  // Summary tiles, counted over Bioconductor packages by install status.
  const TILES = [
    { id: 'installable', label: 'Installable', tone: 'ok', icon: 'check', installs: ['built'], groups: ['installable'] },
    { id: 'blocked', label: 'Blocked', tone: 'warn', icon: 'blocked', installs: ['blocked'], groups: ['blocked', 'builtdeps'] },
    { id: 'failed', label: 'Build failed', tone: 'bad', icon: 'cross', installs: ['failed'], groups: ['failed'] },
    { id: 'skipped', label: 'Skipped', tone: 'muted', icon: 'skip', installs: ['skipped'], groups: ['skipped'] },
    { id: 'unavailable', label: 'Unavailable', tone: 'void', icon: 'slash', installs: ['unresolvable'], groups: ['unavailable'] },
    { id: 'progress', label: 'In progress', tone: 'info', icon: 'clock', installs: ['waiting', 'ready'], groups: ['progress'] },
  ];

  const CIRCLE = 'M13.5 8a5.5 5.5 0 1 1-11 0 5.5 5.5 0 0 1 11 0z';
  const ICONS = {
    check: ['M3.6 8.4 6.6 11.4 12.4 4.6'],
    cross: ['M4.6 4.6l6.8 6.8M11.4 4.6l-6.8 6.8'],
    blocked: [CIRCLE, 'M5.4 8h5.2'],
    box: ['M8 2.5 13 5v6l-5 2.5L3 11V5z', 'M3 5l5 2.5L13 5', 'M8 7.5v6'],
    skip: ['M4 4.5 8.5 8 4 11.5', 'M11.5 4.5v7'],
    slash: [CIRCLE, 'M4.2 11.8l7.6-7.6'],
    clock: [CIRCLE, 'M8 5.2V8l1.9 1.3'],
    queue: ['M3.5 4.5h9', 'M3.5 8h9', 'M3.5 11.5h5'],
    chevron: ['M6 3.8 10.2 8 6 12.2'],
    close: ['M4 4l8 8M12 4l-8 8'],
    external: ['M9.5 3h3.5v3.5', 'M13 3 7.5 8.5', 'M11.5 9.5V13H3V4.5h3.5'],
  };

  const fmt = new Intl.NumberFormat('en');
  const relative = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
  const absolute = new Intl.DateTimeFormat('en-GB', {
    day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'UTC',
  });

  const $ = (id) => document.getElementById(id);
  const wide = window.matchMedia('(min-width: 1080px)');

  let data = null;
  const state = {
    q: '', platform: 'both', groups: new Set(), repos: new Set(), cran: false, sort: 'name', pkg: null,
    tool: 'conda', treePlatform: 0,
  };
  const list = { results: [], rowH: 52, start: -1, end: -1, active: -1, raf: 0, probe: null };
  const treeNodes = new WeakMap();
  // Page content behind the full-screen package sheet, made inert while the sheet is open.
  const BACKGROUND = '.skip, .masthead, #notice, #summary, .browser, .footer';
  let sheetReturn = null;
  let sheetEntry = false;
  let leavingSheet = false;

  /* DOM helpers: data from status.json only ever reaches the page as text nodes or attribute values. */

  function h(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else node.setAttribute(key, value === true ? '' : String(value));
      }
    }
    append(node, children);
    return node;
  }

  function append(node, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function icon(name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    for (const d of ICONS[name] || []) {
      const path = document.createElementNS(ns, 'path');
      path.setAttribute('d', d);
      svg.append(path);
    }
    return svg;
  }

  const plural = (n, one, many) => `${fmt.format(n)} ${n === 1 ? one : many}`;
  const sentence = (text) => {
    const t = text.trim();
    if (!t) return '';
    const s = t[0].toUpperCase() + t.slice(1);
    return /[.!?]$/.test(s) ? s : `${s}.`;
  };
  const joinAnd = (items) => (items.length < 3 ? items.join(' and ') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`);

  /* Data */

  const str = (v) => (typeof v === 'string' ? v : '');
  const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

  function groupOf(install, build) {
    switch (install) {
      case 'built': return 'installable';
      case 'blocked': return build === 'built' ? 'builtdeps' : 'blocked';
      case 'waiting':
      case 'ready': return 'progress';
      case 'failed': return 'failed';
      case 'skipped': return 'skipped';
      case 'unresolvable': return 'unavailable';
      default: return 'unknown';
    }
  }

  function safeLogUrl(value) {
    if (typeof value !== 'string' || !value.startsWith('https://github.com/')) return '';
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password ? url.href : '';
    } catch {
      return '';
    }
  }

  function normalize(raw) {
    if (!isObject(raw)) throw new Error('status.json does not contain a status document.');
    if (raw.schema !== 1) throw new Error(`This page reads status format 1, but status.json uses format ${String(raw.schema).slice(0, 20)}.`);
    const platforms = [...new Set((Array.isArray(raw.platforms) ? raw.platforms : []).filter((p) => typeof p === 'string' && p))];
    if (!platforms.length) throw new Error('status.json lists no platforms.');
    const byName = new Map();
    const packages = isObject(raw.packages) ? raw.packages : {};
    for (const name of Object.keys(packages)) {
      const p = packages[name];
      if (!isObject(p) || !name) continue;
      const plats = isObject(p.platforms) ? p.platforms : {};
      const st = platforms.map((platform) => {
        const s = own(plats, platform) && isObject(plats[platform]) ? plats[platform] : {};
        const install = str(s.install);
        const build = str(s.build);
        return { install, build, reason: str(s.reason), root: str(s.root) || null, group: groupOf(install, build) };
      });
      byName.set(name, {
        name,
        lower: name.toLowerCase(),
        version: str(p.version),
        repo: str(p.repo),
        noarch: p.noarch === true,
        depends: Array.isArray(p.depends) ? p.depends.filter((d) => typeof d === 'string') : [],
        st,
        failures: [],
        dependents: [],
        blocks: platforms.map(() => []),
        blockTotal: 0,
      });
    }
    for (const pkg of byName.values()) {
      pkg.depends = [...new Set(pkg.depends)].filter((d) => d !== pkg.name && byName.has(d));
      for (const d of pkg.depends) byName.get(d).dependents.push(pkg.name);
      pkg.st.forEach((s, i) => {
        if (s.install === 'blocked' && s.root && byName.has(s.root)) byName.get(s.root).blocks[i].push(pkg.name);
      });
    }
    for (const pkg of byName.values()) {
      pkg.blockTotal = new Set(pkg.blocks.flat()).size;
    }
    const failures = isObject(raw.failures) ? raw.failures : {};
    for (const name of Object.keys(failures)) {
      const pkg = byName.get(name);
      if (!pkg || !Array.isArray(failures[name])) continue;
      for (const r of failures[name]) {
        if (!isObject(r)) continue;
        const attempts = Number.isFinite(r.attempts) ? Math.max(0, Math.floor(r.attempts)) : 0;
        pkg.failures.push({ subdir: str(r.subdir), version: str(r.version), attempts, url: safeLogUrl(r.run_url), stage: str(r.stage) });
      }
    }
    const collator = new Intl.Collator('en', { sensitivity: 'base', numeric: true });
    const all = [...byName.values()].sort((a, b) => collator.compare(a.name, b.name) || (a.name < b.name ? -1 : 1));
    const generated = new Date(str(raw.generated));
    return {
      generated: Number.isNaN(generated.getTime()) ? null : generated,
      channel: str(raw.channel).startsWith('https://') ? str(raw.channel) : DEFAULT_CHANNEL,
      bioc: str(raw.bioconductor),
      r: str(raw.r),
      platforms,
      byName,
      all,
    };
  }

  async function loadDocument() {
    const embedded = $('status-data');
    if (embedded && embedded.textContent.trim()) {
      try {
        return JSON.parse(embedded.textContent);
      } catch {
        throw new Error('The status data embedded in this page is not valid JSON.');
      }
    }
    let response;
    try {
      response = await fetch('status.json', { cache: 'no-cache', credentials: 'same-origin' });
    } catch {
      throw new Error('Could not reach status.json. Open this page from a web server that also serves status.json.');
    }
    if (!response.ok) throw new Error(`Could not load status.json: the server answered ${response.status}.`);
    try {
      return await response.json();
    } catch {
      throw new Error('status.json is not valid JSON.');
    }
  }

  /* Status wording */

  function condaName(pkg) {
    return `${pkg.repo === 'cran' ? 'r-' : 'bioconductor-'}${pkg.lower}`;
  }

  function view(s) {
    switch (s.group) {
      case 'installable': return { tone: 'ok', icon: 'check', text: 'Installable' };
      case 'builtdeps': return { tone: 'warn', icon: 'box', text: s.root ? `Built, blocked by ${s.root}` : 'Built, blocked' };
      case 'blocked': return { tone: 'warn', icon: 'blocked', text: s.root ? `Blocked by ${s.root}` : 'Blocked' };
      case 'failed': return { tone: 'bad', icon: 'cross', text: 'Build failed' };
      case 'skipped': return { tone: 'muted', icon: 'skip', text: 'Skipped' };
      case 'unavailable': return { tone: 'void', icon: 'slash', text: s.reason ? `Unavailable: ${s.reason}` : 'Unavailable' };
      case 'progress':
        return s.build === 'ready'
          ? { tone: 'info', icon: 'queue', text: 'Queued' }
          : { tone: 'info', icon: 'clock', text: 'Waiting for dependencies' };
      default: return { tone: 'muted', icon: 'slash', text: 'No status' };
    }
  }

  function shortState(s) {
    switch (s.group) {
      case 'installable': return 'Installable';
      case 'builtdeps': return 'Built, blocked';
      case 'blocked': return 'Blocked';
      case 'failed': return 'Build failed';
      case 'skipped': return 'Skipped';
      case 'unavailable': return 'Unavailable';
      case 'progress': return s.build === 'ready' ? 'Queued' : 'Waiting for dependencies';
      default: return 'No status';
    }
  }

  function pill(s) {
    const v = view(s);
    const node = h('span', { class: `pill tone-${v.tone}`, title: v.text }, icon(v.icon), h('span', { class: 'pill-text', text: v.text }));
    return node;
  }

  function stageInfo(stage) {
    return STAGES.find((s) => s.id === stage) || null;
  }

  function stoppedOn(root) {
    const r = data.byName.get(root);
    return r ? data.platforms.filter((_, i) => STOPPED.has(r.st[i].install)) : [];
  }

  // The platform to trace a blocked status on: one of idx where the root does not install, else any such
  // platform, since a noarch build waits for a root that may fail only on the platform it builds on.
  function causeIndex(idx, root) {
    const r = data.byName.get(root);
    if (!r) return idx[0];
    const here = idx.find((i) => STOPPED.has(r.st[i].install));
    if (here !== undefined) return here;
    const anywhere = r.st.findIndex((s) => STOPPED.has(s.install));
    return anywhere >= 0 ? anywhere : idx[0];
  }

  const isBlocked = (s) => (s.group === 'blocked' || s.group === 'builtdeps') && Boolean(s.root) && data.byName.has(s.root);

  /* Filtering */

  function considered() {
    if (state.platform === 'both') return data.platforms.map((_, i) => i);
    const i = data.platforms.indexOf(state.platform);
    return i < 0 ? data.platforms.map((_, j) => j) : [i];
  }

  function normalQuery() {
    let q = state.q.trim().toLowerCase();
    if (q.startsWith('bioconductor-')) q = q.slice(13);
    else if (q.startsWith('r-')) q = q.slice(2);
    return q;
  }

  function inScope(pkg) {
    if (pkg.repo === 'cran') return state.cran;
    if (!state.repos.size) return true;
    const repo = REPOS.find((r) => r.repo === pkg.repo);
    return repo ? state.repos.has(repo.key) : false;
  }

  function score(pkg, q) {
    if (!q) return 0;
    if (pkg.lower === q) return 1;
    if (pkg.lower.startsWith(q)) return 2;
    return pkg.lower.includes(q) ? 3 : -1;
  }

  function compute() {
    const q = normalQuery();
    const idx = considered();
    const counts = new Map(GROUPS.map((g) => [g.id, 0]));
    const results = [];
    const scores = new Map();
    let scope = 0;
    let cranMatches = 0;
    for (const pkg of data.all) {
      if (!inScope(pkg)) {
        if (q && pkg.repo === 'cran' && score(pkg, q) >= 0) cranMatches++;
        continue;
      }
      scope++;
      const s = score(pkg, q);
      if (s < 0) continue;
      const groups = new Set(idx.map((i) => pkg.st[i].group));
      for (const g of groups) if (counts.has(g)) counts.set(g, counts.get(g) + 1);
      if (state.groups.size && ![...groups].some((g) => state.groups.has(g))) continue;
      results.push(pkg);
      scores.set(pkg, s);
    }
    if (state.sort === 'blocks') {
      const blocked = (pkg) => Math.max(0, ...idx.map((i) => pkg.blocks[i].length));
      results.sort((a, b) => blocked(b) - blocked(a) || scores.get(a) - scores.get(b));
    } else if (q) {
      results.sort((a, b) => scores.get(a) - scores.get(b));
    }
    return { results, counts, scope, cranMatches };
  }

  function otherFiltersActive() {
    return state.platform !== 'both' || state.groups.size > 0 || state.repos.size > 0 || state.cran;
  }

  function filtersActive() {
    return Boolean(state.q.trim()) || otherFiltersActive();
  }

  /* URL hash */

  function readHash() {
    let params;
    try {
      params = new URLSearchParams(location.hash.replace(/^#/, ''));
    } catch {
      params = new URLSearchParams();
    }
    state.q = (params.get('q') || '').slice(0, 200);
    const platform = params.get('platform');
    state.platform = platform && data.platforms.includes(platform) ? platform : 'both';
    state.groups = new Set((params.get('status') || '').split(',').filter((g) => GROUP_IDS.has(g)));
    state.repos = new Set((params.get('repo') || '').split(',').filter((r) => REPO_BY_KEY.has(r)));
    state.cran = params.get('cran') === '1';
    state.sort = params.get('sort') === 'blocks' ? 'blocks' : 'name';
    const pkg = params.get('pkg');
    state.pkg = pkg && data.byName.has(pkg) ? pkg : null;
  }

  function writeHash() {
    const params = new URLSearchParams();
    if (state.q.trim()) params.set('q', state.q.trim());
    if (state.platform !== 'both') params.set('platform', state.platform);
    if (state.groups.size) params.set('status', GROUPS.filter((g) => state.groups.has(g.id)).map((g) => g.id).join(','));
    if (state.repos.size) params.set('repo', REPOS.filter((r) => state.repos.has(r.key)).map((r) => r.key).join(','));
    if (state.cran) params.set('cran', '1');
    if (state.sort !== 'name') params.set('sort', state.sort);
    if (state.pkg) params.set('pkg', state.pkg);
    const hash = params.toString();
    try {
      history.replaceState(null, '', `${location.pathname}${location.search}${hash ? `#${hash}` : ''}`);
    } catch {
      // History updates can be refused on some local files; the page keeps working without them.
    }
  }

  /* Header and summary */

  function renderHeader() {
    const versions = [data.bioc && `Bioconductor ${data.bioc}`, data.r && `R ${data.r}`].filter(Boolean);
    $('title').textContent = versions.length === 2 ? `${versions[0]} for ${versions[1]}` : versions[0] || 'Bioconductor packages on conda';
    document.title = data.bioc ? `Bioconductor ${data.bioc} package status | biocpkgconda` : 'biocpkgconda status';
    const platforms = data.platforms.length ? ` on ${joinAnd(data.platforms)}` : '';
    $('lede').replaceChildren('Build and install status of every package in ', h('span', { class: 'channel', text: data.channel }), `${platforms}.`);
    renderUpdated();
    renderInstallLine();
  }

  function renderUpdated() {
    if (!data.generated) {
      $('updated').hidden = true;
      return;
    }
    const time = $('updated-relative');
    time.dateTime = data.generated.toISOString();
    time.textContent = relativeTime(data.generated);
    $('updated-absolute').textContent = `${absolute.format(data.generated)} UTC`;
    $('updated').hidden = false;
  }

  function relativeTime(date) {
    const seconds = (date.getTime() - Date.now()) / 1000;
    const steps = [[60, 'second'], [3600, 'minute'], [86400, 'hour'], [604800, 'day'], [2629800, 'week'], [31557600, 'month'], [Infinity, 'year']];
    const sizes = { second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2629800, year: 31557600 };
    const abs = Math.abs(seconds);
    if (abs < 45) return 'just now';
    const [, unit] = steps.find(([limit]) => abs < limit);
    return relative.format(Math.round(seconds / sizes[unit]), unit);
  }

  function installCommand(name) {
    const channel = data ? data.channel : DEFAULT_CHANNEL;
    if (state.tool === 'pixi') return `pixi init -c ${channel} -c conda-forge && pixi add ${name}`;
    return `conda install --override-channels --strict-channel-priority -c ${channel} -c conda-forge ${name}`;
  }

  function renderInstallLine() {
    $('install-command').textContent = installCommand('bioconductor-<name>');
    for (const input of document.querySelectorAll('input[name="tool"]')) input.checked = input.value === state.tool;
  }

  function renderSummary() {
    const bioc = data.all.filter((p) => p.repo !== 'cran');
    const grid = $('summary-grid');
    grid.replaceChildren(...data.platforms.map((platform, i) => {
      const counts = TILES.map((tile) => bioc.filter((p) => tile.installs.includes(p.st[i].install)).length);
      const total = bioc.length;
      const installable = counts[0];
      const pct = total ? Math.round((installable / total) * 100) : 0;
      const bar = h('div', { class: 'bar', 'aria-hidden': 'true' });
      TILES.forEach((tile, t) => {
        if (!counts[t]) return;
        const seg = h('div', { class: `bar-seg tone-${tile.tone}`, 'data-tip': `${tile.label}: ${fmt.format(counts[t])} (${percent(counts[t], total)})` });
        seg.style.flexGrow = String(counts[t]);
        bar.append(seg);
      });
      const tiles = h('ul', { class: 'tiles' }, TILES.map((tile, t) => h('li', null,
        h('button', {
          type: 'button',
          class: `tile tone-${tile.tone}`,
          'data-platform': platform,
          'data-tile': tile.id,
          'aria-label': `${fmt.format(counts[t])} ${tile.label} on ${platform}. Show these packages.`,
        },
        h('span', { class: `tile-num${counts[t] ? '' : ' zero'}`, text: fmt.format(counts[t]) }),
        h('span', { class: 'tile-label' }, icon(tile.icon), h('span', { text: tile.label }))))));
      return h('article', { class: 'psum', 'aria-labelledby': `psum-${i}` },
        h('div', { class: 'psum-head' },
          h('h3', { id: `psum-${i}`, text: platform }),
          h('p', { class: 'psum-total', text: `${plural(total, 'Bioconductor package', 'Bioconductor packages')}` })),
        h('p', { class: 'psum-hero' }, h('span', { class: 'hero-num', text: fmt.format(installable) }), `install with every dependency (${pct}%)`),
        bar,
        tiles);
    }));
  }

  function percent(n, total) {
    if (!total) return '0%';
    const p = (n / total) * 100;
    return p > 0 && p < 1 ? 'under 1%' : `${Math.round(p)}%`;
  }

  /* Filter controls */

  function buildControls() {
    const platformSet = $('platform-filter');
    const options = [['both', data.platforms.length === 2 ? 'Both' : 'All'], ...data.platforms.map((p) => [p, p])];
    for (const [value, label] of options) {
      platformSet.append(h('label', null, h('input', { type: 'radio', name: 'platform', value }), h('span', { text: label })));
    }
    const statusSet = $('status-filter');
    for (const g of GROUPS) {
      statusSet.append(h('label', { class: `chip tone-${g.tone}` },
        h('input', { type: 'checkbox', name: 'status', value: g.id }),
        h('span', { class: 'chip-body' }, h('span', { class: 'chip-dot', 'aria-hidden': 'true' }), h('span', { text: g.label }),
          h('span', { class: 'chip-count', 'data-count': g.id }))));
    }
    const repoSet = $('repo-filter');
    const cran = repoSet.querySelector('.switch');
    for (const r of REPOS) {
      repoSet.insertBefore(h('label', { class: 'chip' },
        h('input', { type: 'checkbox', name: 'repo', value: r.key }),
        h('span', { class: 'chip-body' }, h('span', { text: r.label }))), cran);
    }
  }

  function syncControls() {
    const search = $('search');
    if (search.value !== state.q) search.value = state.q;
    for (const input of document.querySelectorAll('input[name="platform"]')) input.checked = input.value === state.platform;
    for (const input of document.querySelectorAll('input[name="status"]')) input.checked = state.groups.has(input.value);
    for (const input of document.querySelectorAll('input[name="repo"]')) input.checked = state.repos.has(input.value);
    $('cran').checked = state.cran;
    $('sort').value = state.sort;
  }

  /* Package list */

  let countTimer = 0;

  function update() {
    const { results, counts, scope, cranMatches } = compute();
    list.results = results;
    for (const node of document.querySelectorAll('[data-count]')) node.textContent = fmt.format(counts.get(node.dataset.count) || 0);
    const selected = state.pkg ? results.findIndex((p) => p.name === state.pkg) : -1;
    list.active = selected >= 0 ? selected : Math.min(list.active, results.length - 1);
    $('clear').hidden = !filtersActive();
    $('search').placeholder = `Search ${plural(scope, 'package', 'packages')}`;
    const hint = cranMatches ? `${plural(cranMatches, 'CRAN dependency matches', 'CRAN dependencies match')} this search.` : '';
    renderCount(results.length, scope, hint);
    renderCranHint(hint);
    renderListHead();
    const empty = results.length === 0;
    $('empty').hidden = !empty;
    if (empty) renderEmpty();
    layoutList();
    writeHash();
  }

  function renderCount(n, scope, hint) {
    const text = n === scope ? plural(n, 'package', 'packages') : `${fmt.format(n)} of ${plural(scope, 'package', 'packages')}`;
    clearTimeout(countTimer);
    // A short delay keeps screen readers from announcing every keystroke.
    countTimer = setTimeout(() => {
      $('count').replaceChildren(text, hint ? h('span', { class: 'sr-only', text: `. ${hint}` }) : '');
    }, 250);
  }

  function renderCranHint(hint) {
    const node = $('cran-hint');
    node.hidden = !hint;
    if (hint) node.replaceChildren(`${hint} `, h('button', { type: 'button', class: 'link-button', 'data-action': 'cran', text: 'Include CRAN dependencies' }));
  }

  function renderEmpty() {
    const q = state.q.trim();
    $('empty-title').textContent = q ? `No packages match “${q}”` : 'No packages match these filters';
    const clear = (text) => h('button', { type: 'button', class: 'link-button', 'data-action': 'clear', text });
    $('empty-text').replaceChildren(...(q
      ? ['Try a shorter name, or ', clear(otherFiltersActive() ? 'clear the filters' : 'clear the search'), '.']
      : [clear('Clear the filters'), ' to see every package.']));
  }

  function renderListHead() {
    const idx = considered();
    const cols = String(idx.length);
    const head = $('list-head');
    for (const node of [$('rows'), head]) {
      node.dataset.cols = cols;
      node.style.setProperty('--cols', cols);
    }
    head.replaceChildren(h('span', { text: 'Package' }), ...idx.map((i) => h('span', { text: data.platforms[i] })));
  }

  // The row height is set in CSS in rem and changes with the list width, so it is read from an element sized by it.
  function measureRow() {
    const value = list.probe ? list.probe.getBoundingClientRect().height : 0;
    return value > 0 ? value : 52;
  }

  function layoutList() {
    list.rowH = measureRow();
    $('rows').style.height = `${list.results.length * list.rowH}px`;
    list.start = -1;
    renderWindow();
  }

  function renderWindow() {
    const rows = $('rows');
    const n = list.results.length;
    const top = Math.max(0, -rows.getBoundingClientRect().top);
    const first = Math.max(0, Math.floor(top / list.rowH) - 6);
    const last = Math.min(n, Math.ceil((top + window.innerHeight) / list.rowH) + 6);
    if (first === list.start && last === list.end) return;
    list.start = first;
    list.end = last;
    const q = normalQuery();
    const idx = considered();
    const fragment = document.createDocumentFragment();
    for (let i = first; i < last; i++) fragment.append(rowElement(list.results[i], i, n, q, idx));
    rows.replaceChildren(list.probe, fragment);
    syncActive();
  }

  function scheduleWindow() {
    if (list.raf) return;
    list.raf = requestAnimationFrame(() => {
      list.raf = 0;
      renderWindow();
    });
  }

  function highlighted(name, q) {
    const at = q ? name.toLowerCase().indexOf(q) : -1;
    if (at < 0) return [name];
    return [name.slice(0, at), h('mark', { text: name.slice(at, at + q.length) }), name.slice(at + q.length)];
  }

  function rowElement(pkg, i, n, q, idx) {
    const row = h('div', {
      class: 'row',
      role: 'option',
      id: `opt-${i}`,
      'aria-selected': pkg.name === state.pkg ? 'true' : 'false',
      'aria-posinset': i + 1,
      'aria-setsize': n,
      'data-index': i,
    });
    row.style.transform = `translateY(${i * list.rowH}px)`;
    const badges = h('span', { class: 'badges' },
      h('span', { class: `badge${pkg.repo === 'cran' ? ' badge-cran' : ''}`, text: REPO_LABEL.get(pkg.repo) || pkg.repo || 'Unknown' }),
      pkg.noarch && h('span', { class: 'badge badge-noarch', text: 'noarch' }));
    row.append(
      h('div', { class: 'row-main' },
        h('span', { class: 'row-name', title: pkg.name }, highlighted(pkg.name, q)),
        h('span', { class: 'row-meta' }, h('span', { class: 'row-version', text: pkg.version }), badges)),
      h('div', { class: 'row-status' }, idx.map((p) => h('span', { class: 'cell' },
        h('span', { class: 'cell-platform', text: data.platforms[p] }), pill(pkg.st[p])))));
    return row;
  }

  function syncActive() {
    const rows = $('rows');
    for (const node of rows.querySelectorAll('.row.active')) node.classList.remove('active');
    const active = list.active >= 0 ? $(`opt-${list.active}`) : null;
    if (active) {
      active.classList.add('active');
      rows.setAttribute('aria-activedescendant', active.id);
    } else {
      rows.removeAttribute('aria-activedescendant');
    }
  }

  function revealRow(i) {
    const rows = $('rows');
    const rect = rows.getBoundingClientRect();
    const head = $('list-head').getBoundingClientRect().height || 0;
    const top = rect.top + i * list.rowH;
    const bottom = top + list.rowH;
    if (top < head + 8) window.scrollBy(0, top - head - 8);
    else if (bottom > window.innerHeight - 8) window.scrollBy(0, bottom - window.innerHeight + 8);
    list.start = -1;
    renderWindow();
  }

  function moveActive(to) {
    const n = list.results.length;
    if (!n) return;
    list.active = Math.max(0, Math.min(n - 1, to));
    revealRow(list.active);
    syncActive();
    if (wide.matches) select(list.results[list.active].name);
  }

  function onRowsKey(event) {
    const n = list.results.length;
    if (!n) return;
    const page = Math.max(1, Math.floor(window.innerHeight / list.rowH) - 2);
    const at = list.active < 0 ? -1 : list.active;
    switch (event.key) {
      case 'ArrowDown': moveActive(at + 1); break;
      case 'ArrowUp': moveActive(at <= 0 ? 0 : at - 1); break;
      case 'PageDown': moveActive(at + page); break;
      case 'PageUp': moveActive(at - page); break;
      case 'Home': moveActive(0); break;
      case 'End': moveActive(n - 1); break;
      case 'Enter':
      case ' ':
        if (at >= 0) select(list.results[at].name, { open: true });
        break;
      default: return;
    }
    event.preventDefault();
  }

  /* Selection and the detail panel */

  function select(name, { open = false } = {}) {
    if (!data.byName.has(name)) return;
    const changed = state.pkg !== name;
    state.pkg = name;
    const index = list.results.findIndex((p) => p.name === name);
    if (index >= 0) list.active = index;
    if (changed) {
      state.treePlatform = defaultTreePlatform(data.byName.get(name));
      renderDetail();
      writeHash();
      for (const row of $('rows').querySelectorAll('.row')) {
        row.setAttribute('aria-selected', list.results[Number(row.dataset.index)]?.name === name ? 'true' : 'false');
      }
    }
    syncActive();
    if (open && !wide.matches) openSheet();
    else if (open && wide.matches) $('detail').scrollTop = 0;
  }

  function defaultTreePlatform(pkg) {
    if (state.platform !== 'both') {
      const i = data.platforms.indexOf(state.platform);
      if (i >= 0) return i;
    }
    const i = pkg.st.findIndex((s) => s.group !== 'installable');
    if (i < 0) return 0;
    return isBlocked(pkg.st[i]) ? causeIndex([i], pkg.st[i].root) : i;
  }

  function setBackgroundInert(inert) {
    for (const node of document.querySelectorAll(BACKGROUND)) node.inert = inert;
  }

  function openSheet() {
    const detail = $('detail');
    if (detail.classList.contains('open')) return;
    sheetReturn = document.activeElement;
    detail.classList.add('open');
    detail.setAttribute('role', 'dialog');
    detail.setAttribute('aria-modal', 'true');
    document.body.classList.add('sheet-open');
    setBackgroundInert(true);
    detail.scrollTop = 0;
    const heading = $('detail-title');
    if (heading) heading.focus({ preventScroll: true });
    // A history entry of its own lets the Back button or gesture close the sheet.
    try {
      history.pushState(null, '', location.href);
      sheetEntry = true;
    } catch {
      sheetEntry = false;
    }
  }

  function closeSheet() {
    const detail = $('detail');
    if (!detail.classList.contains('open')) return;
    detail.classList.remove('open');
    detail.removeAttribute('role');
    detail.removeAttribute('aria-modal');
    document.body.classList.remove('sheet-open');
    setBackgroundInert(false);
    const target = sheetReturn && document.contains(sheetReturn) ? sheetReturn : $('rows');
    sheetReturn = null;
    target.focus({ preventScroll: true });
    if (sheetEntry) {
      sheetEntry = false;
      leavingSheet = true;
      history.back();
    }
  }

  function onPopState() {
    if (leavingSheet) {
      // Going back restored the URL from before the sheet opened; keep what was chosen inside the sheet.
      leavingSheet = false;
      writeHash();
    } else if ($('detail').classList.contains('open')) {
      sheetEntry = false;
      closeSheet();
    }
  }

  function tabbable(root) {
    return [...root.querySelectorAll('a[href], button, input, select, [tabindex]')].filter((node) => {
      if (node.tabIndex < 0 || node.disabled || !node.getClientRects().length) return false;
      if (node.type !== 'radio' || node.checked) return true;
      const group = [...root.querySelectorAll('input[type="radio"]')].filter((r) => r.name === node.name);
      return !group.some((r) => r.checked) && group[0] === node;
    });
  }

  function renderDetail() {
    const pkg = state.pkg ? data.byName.get(state.pkg) : null;
    $('detail-inner').replaceChildren(...(pkg ? detailView(pkg) : overview()));
  }

  function overview() {
    const roots = data.all
      .filter((p) => p.blockTotal > 0 && p.st.some((s) => s.group === 'failed' || s.group === 'skipped' || s.group === 'unavailable'))
      .sort((a, b) => b.blockTotal - a.blockTotal)
      .slice(0, 8);
    const nodes = [
      h('h2', { id: 'detail-title', class: 'overview-title', tabindex: '-1', text: 'Select a package' }),
      h('p', { class: 'overview-text', text: 'Choose a package to see whether it installs on each platform, where its build stopped, which dependency holds it back and how to install it.' }),
    ];
    if (roots.length) {
      nodes.push(h('section', { class: 'd-section' },
        h('h3', { text: 'Problems that block the most packages' }),
        h('ol', { class: 'roots' }, roots.map((p) => {
          const where = new Map();
          p.st.forEach((s, i) => {
            if (s.group === 'failed' || s.group === 'skipped' || s.group === 'unavailable') {
              const label = s.group === 'failed' ? 'Build failed' : s.group === 'skipped' ? 'Skipped' : 'Unavailable';
              where.set(label, [...(where.get(label) || []), data.platforms[i]]);
            }
          });
          const why = [...where].map(([label, platforms]) => (platforms.length === data.platforms.length ? label : `${label} on ${joinAnd(platforms)}`)).join('; ');
          return h('li', null,
            h('button', { type: 'button', class: 'pkg-link', 'data-pkg': p.name, text: p.name }),
            h('span', { class: 'blocks' }, h('strong', { text: fmt.format(p.blockTotal) }), ' blocked'),
            h('span', { class: 'why', text: why }));
        }))));
    }
    return nodes;
  }

  function detailView(pkg) {
    const nodes = [];
    const close = h('button', { type: 'button', class: 'icon-button close', 'aria-label': 'Close package details', 'data-action': 'close' }, icon('close'));
    nodes.push(h('div', { class: 'd-head' },
      h('div', { class: 'd-title-row' }, h('h2', { id: 'detail-title', tabindex: '-1', text: pkg.name }), close),
      h('p', { class: 'd-meta' },
        pkg.version && h('span', { class: 'version', text: `Version ${pkg.version}` }),
        h('span', { class: `badge${pkg.repo === 'cran' ? ' badge-cran' : ''}`, text: REPO_LABEL.get(pkg.repo) || pkg.repo || 'Unknown' }),
        h('span', { class: `badge${pkg.noarch ? ' badge-noarch' : ''}`, text: pkg.noarch ? 'noarch' : 'Compiled' }))));
    nodes.push(installSection(pkg));
    nodes.push(statusSection(pkg));
    nodes.push(dependencySection(pkg));
    nodes.push(usageSection(pkg));
    return nodes;
  }

  function installSection(pkg) {
    const name = condaName(pkg);
    const installs = data.platforms.filter((_, i) => pkg.st[i].group === 'installable');
    const command = state.tool === 'pixi' ? `pixi add ${name}` : installCommand(name);
    const section = h('section', { class: 'd-section' },
      h('div', { class: 'd-section-head' },
        h('h3', { text: 'Install' }),
        h('div', { class: 'seg seg-small', role: 'radiogroup', 'aria-label': 'Package manager' },
          ['conda', 'pixi'].map((tool) => h('label', null,
            h('input', { type: 'radio', name: 'detail-tool', value: tool, checked: state.tool === tool }),
            h('span', { text: tool }))))));
    if (!installs.length) section.append(h('p', { class: 'd-warn', text: 'This package does not install on any platform yet.' }));
    else if (installs.length < data.platforms.length) section.append(h('p', { class: 'd-warn', text: `Installs on ${joinAnd(installs)} only.` }));
    section.append(h('div', { class: 'code-line' },
      h('code', { id: 'detail-command', text: command }),
      h('button', { type: 'button', class: 'copy', 'data-copy': 'detail-command' }, 'Copy')));
    if (state.tool === 'pixi') {
      section.append(h('p', { class: 'd-note' }, 'In a workspace created with ', h('code', { text: `pixi init -c ${data.channel} -c conda-forge` }), '.'));
    }
    return section;
  }

  function statusSection(pkg) {
    const blocks = [];
    pkg.st.forEach((s, i) => {
      const key = [s.install, s.build, s.reason, s.root].join('\u0000');
      const same = blocks.find((b) => b.key === key);
      if (same) same.idx.push(i);
      else blocks.push({ key, idx: [i], s });
    });
    return h('section', { class: 'd-section' },
      h('h3', { text: 'Status' }),
      blocks.map((b) => platformBlock(pkg, b.idx, b.s)));
  }

  function statusText(pkg, s, idx) {
    const where = joinAnd(idx.map((i) => data.platforms[i]));
    const root = s.root || 'a package it depends on';
    const because = s.reason ? ` (${s.reason})` : '';
    switch (s.group) {
      case 'installable':
        return pkg.noarch
          ? `Built once as a noarch package. It and every package it depends on install on ${where}.`
          : `Built for ${where}. It and every package it depends on install there.`;
      case 'builtdeps':
        return `Built, but it cannot be installed on ${where} until ${root} installs there${because}.`;
      case 'blocked': {
        if (pkg.noarch && s.build === 'blocked') {
          const at = s.root ? stoppedOn(s.root) : [];
          return `Not built: it is built once as a noarch package, and that build waits for ${root}, which does not install on ${at.length ? joinAnd(at) : 'the platform it builds on'}${because}.`;
        }
        if (s.build === 'blocked') return `Not built for ${where}: it waits for ${root}, which does not install there${because}.`;
        const until = `it cannot be installed on ${where} until ${root} installs there${because}`;
        return s.build === 'ready' ? `${pkg.noarch ? 'Its noarch build is queued' : 'Queued'}, but ${until}.` : `Not built yet, and ${until}.`;
      }
      case 'failed':
        return sentence(s.reason || 'The build failed');
      case 'skipped':
        return sentence(`Skipped on purpose: ${s.reason || 'no reason recorded'}`);
      case 'unavailable':
        return sentence(`Cannot be built in this channel: ${s.reason || 'no reason recorded'}`);
      case 'progress': {
        if (s.build === 'built') return `Built. Waiting for packages it depends on to be built for ${where}.`;
        if (s.build !== 'ready') return 'Waiting for packages it depends on to be built.';
        if (!pkg.noarch) return `Queued: every package it depends on installs on ${where}, so it builds there in the next run.`;
        const pending = idx.filter((i) => pkg.depends.some((d) => data.byName.get(d).st[i].install !== 'built'));
        const later = pending.length ? ` It installs on ${joinAnd(pending.map((i) => data.platforms[i]))} once the packages it depends on are built there.` : '';
        return `Queued: it is built once as a noarch package in the next run.${later}`;
      }
      default:
        return 'The status file has no status for this platform.';
    }
  }

  function recordsFor(pkg, platformIdx) {
    const subdirs = new Set(platformIdx.map((i) => data.platforms[i]));
    subdirs.add('noarch');
    return pkg.failures.filter((r) => subdirs.has(r.subdir));
  }

  function platformBlock(pkg, idx, s) {
    const platforms = idx.map((i) => data.platforms[i]);
    const block = h('div', { class: 'pstat' },
      h('div', { class: 'pstat-head' }, h('h4', { text: joinAnd(platforms) }), pill(s)),
      h('p', { class: 'pstat-text', text: statusText(pkg, s, idx) }));
    if (isBlocked(s)) append(block, [trace(pkg, causeIndex(idx, s.root), s.root)]);
    const records = recordsFor(pkg, idx);
    if (records.length && s.group !== 'installable') {
      for (const r of records) block.append(recordView(r, s.group === 'failed' ? 'Failed build' : 'Earlier failed attempt'));
    }
    return block;
  }

  function findPath(start, i, root) {
    const previous = new Map([[start.name, null]]);
    const queue = [start.name];
    for (let head = 0; head < queue.length && !previous.has(root); head++) {
      for (const d of data.byName.get(queue[head]).depends) {
        if (previous.has(d)) continue;
        const s = data.byName.get(d).st[i];
        if (d !== root && !(s.root === root && STOPPED.has(s.install))) continue;
        previous.set(d, queue[head]);
        queue.push(d);
      }
    }
    if (!previous.has(root)) return [start.name, root];
    const path = [];
    for (let n = root; n !== null; n = previous.get(n)) path.push(n);
    return path.reverse();
  }

  function trace(pkg, i, root) {
    const path = findPath(pkg, i, root);
    const items = path.map((name, k) => {
      const p = data.byName.get(name);
      const s = p.st[i];
      const isRoot = k === path.length - 1;
      const tone = view(s).tone;
      const label = k === 0
        ? h('span', { class: 'trace-self', text: name })
        : h('button', { type: 'button', class: 'pkg-link', 'data-pkg': name, text: name });
      const li = h('li', { class: `tone-${tone}${isRoot ? ' is-root' : ''}` },
        h('span', { class: 'trace-dot', 'aria-hidden': 'true' }),
        h('div', { class: 'trace-row' }, label, h('span', { class: 'trace-state', text: shortState(s) })));
      if (isRoot) li.append(rootDetail(p, i));
      return li;
    });
    return [
      h('p', { class: 'trace-label', text: `Dependency path to the cause on ${data.platforms[i]}` }),
      h('ol', { class: 'trace' }, items),
    ];
  }

  function rootDetail(root, i) {
    const s = root.st[i];
    const box = h('div', { class: `root-detail tone-${view(s).tone}` });
    let text;
    if (s.group === 'failed') text = sentence(s.reason || 'The build failed');
    else if (s.group === 'skipped') text = sentence(`Skipped on purpose: ${s.reason || 'no reason recorded'}`);
    else if (s.group === 'unavailable') text = sentence(`Cannot be built in this channel: ${s.reason || 'no reason recorded'}`);
    else text = statusText(root, s, [i]);
    box.append(h('p', { class: 'pstat-text', text }));
    for (const r of recordsFor(root, [i])) box.append(recordView(r, 'Failed build'));
    return box;
  }

  function recordView(r, title) {
    const stage = stageInfo(r.stage);
    const head = h('p', { class: 'record-head' },
      h('span', { class: 'record-title', text: `${title} on ${r.subdir || 'unknown platform'}` }),
      r.version && h('span', { text: `Version ${r.version}` }),
      h('span', { text: plural(r.attempts, 'attempt', 'attempts') }),
      r.url
        ? h('a', { class: 'logs', href: r.url, target: '_blank', rel: 'noopener noreferrer' }, 'Build logs', icon('external'), h('span', { class: 'sr-only', text: ' (opens on GitHub)' }))
        : h('span', { class: 'muted', text: 'No build logs linked' }));
    const what = stage ? `Stopped while ${stage.text}.` : 'Stage unknown: this failure was recorded before build stages were tracked.';
    const failedAt = stage ? STAGES.indexOf(stage) : -1;
    const steps = h('ol', { class: `stages${stage ? '' : ' unknown'}`, 'aria-label': 'Build stages' }, STAGES.map((st, k) => {
      const cls = !stage ? '' : k < failedAt ? ' done' : k === failedAt ? ' failed' : '';
      const status = !stage ? 'unknown' : k < failedAt ? 'passed' : k === failedAt ? 'failed' : 'not reached';
      return h('li', { class: `stage${cls}`, 'aria-current': k === failedAt ? 'step' : null },
        h('span', { class: 'stage-bar', 'aria-hidden': 'true' }),
        h('span', { class: 'stage-name', text: st.short }),
        h('span', { class: 'sr-only', text: `: ${status}` }));
    }));
    return h('div', { class: 'record' }, head, h('p', { class: 'pstat-text', text: what }), steps);
  }

  function transitive(pkg) {
    const seen = new Set();
    const stack = [...pkg.depends];
    while (stack.length) {
      const name = stack.pop();
      if (seen.has(name) || name === pkg.name) continue;
      seen.add(name);
      stack.push(...data.byName.get(name).depends);
    }
    return seen;
  }

  function dependencySection(pkg) {
    const i = Math.min(state.treePlatform, data.platforms.length - 1);
    const section = h('section', { class: 'd-section', id: 'deps-section' },
      h('div', { class: 'd-section-head' },
        h('h3', { text: 'Dependencies' }),
        data.platforms.length > 1 && h('div', { class: 'seg seg-small platform-pick', role: 'radiogroup', 'aria-label': 'Platform for dependency status' },
          data.platforms.map((p, k) => h('label', null,
            h('input', { type: 'radio', name: 'tree-platform', value: String(k), checked: k === i }),
            h('span', { text: p }))))));
    if (!pkg.depends.length) {
      section.append(h('p', { class: 'pstat-text', text: 'It depends on no other package in this channel.' }));
      return section;
    }
    const all = transitive(pkg);
    const missing = [...all].filter((n) => data.byName.get(n).st[i].group !== 'installable').length;
    section.append(h('p', { class: 'pstat-text' },
      `${plural(pkg.depends.length, 'direct dependency', 'direct dependencies')} and ${fmt.format(all.size)} in total in this channel. `,
      missing ? `${plural(missing, 'of them does', 'of them do')} not install on ${data.platforms[i]}.` : `All install on ${data.platforms[i]}.`));
    const s = pkg.st[i];
    const rootHere = isBlocked(s) && STOPPED.has(data.byName.get(s.root).st[i].install);
    if (isBlocked(s) && !rootHere) {
      section.append(h('p', { class: 'pstat-text', text: `It is not built yet: its noarch build waits for ${s.root}, which does not install on ${joinAnd(stoppedOn(s.root))}.` }));
    }
    const path = rootHere ? findPath(pkg, i, s.root) : [];
    section.append(h('ul', { class: 'tree', 'aria-label': `Dependencies of ${pkg.name} on ${data.platforms[i]}` },
      pkg.depends.map((d) => treeNode(d, i, new Set([pkg.name]), path.slice(1), path[path.length - 1]))));
    return section;
  }

  function treeNode(name, i, ancestors, pathRest, root) {
    const p = data.byName.get(name);
    const onPath = pathRest.length > 0 && pathRest[0] === name;
    const isRoot = onPath && name === root;
    const expandable = p.depends.some((d) => !ancestors.has(d));
    const li = h('li');
    const row = h('div', { class: `node-row${onPath ? ' on-path' : ''}${isRoot ? ' is-root' : ''}` });
    if (expandable) {
      row.append(h('button', { type: 'button', class: 'toggle', 'aria-expanded': 'false', 'aria-label': `Dependencies of ${name}`, 'data-action': 'toggle' }, icon('chevron')));
    } else {
      row.append(h('span', { class: 'toggle-spacer', 'aria-hidden': 'true' }));
    }
    row.append(
      h('button', { type: 'button', class: 'pkg-link', 'data-pkg': name, text: name }),
      h('span', { class: 'node-version', text: p.version }),
      pill(p.st[i]));
    li.append(row);
    const next = new Set(ancestors).add(name);
    treeNodes.set(li, { name, i, ancestors: next, pathRest: onPath ? pathRest.slice(1) : [], root });
    if (onPath && expandable && pathRest.length > 1) expandNode(li);
    return li;
  }

  function expandNode(li) {
    const info = treeNodes.get(li);
    const toggle = li.querySelector(':scope > .node-row > .toggle');
    let children = li.querySelector(':scope > ul');
    if (!children) {
      const p = data.byName.get(info.name);
      children = h('ul', null, p.depends.filter((d) => !info.ancestors.has(d)).map((d) => treeNode(d, info.i, info.ancestors, info.pathRest, info.root)));
      li.append(children);
    }
    children.hidden = false;
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
  }

  function toggleNode(li) {
    const children = li.querySelector(':scope > ul');
    const toggle = li.querySelector(':scope > .node-row > .toggle');
    if (children && !children.hidden) {
      children.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
    } else {
      expandNode(li);
    }
  }

  function nameList(names, limit = NAME_LIST_LIMIT) {
    const sorted = [...names].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
    const ul = h('ul', { class: 'name-list' });
    const shown = sorted.slice(0, limit);
    for (const name of shown) ul.append(h('li', null, h('button', { type: 'button', class: 'pkg-link', 'data-pkg': name, text: name })));
    if (sorted.length > limit) {
      const more = h('button', { type: 'button', class: 'link-button', 'data-action': 'more', text: `Show all ${fmt.format(sorted.length)}` });
      more.addEventListener('click', () => {
        const full = nameList(sorted, Infinity);
        ul.replaceWith(full);
        full.querySelectorAll('.pkg-link')[limit]?.focus();
      }, { once: true });
      ul.append(h('li', null, more));
    }
    return ul;
  }

  function usageSection(pkg) {
    const section = h('section', { class: 'd-section' }, h('h3', { text: 'Used by' }));
    pkg.blocks.forEach((names, i) => {
      if (!names.length) return;
      section.append(h('div', { class: 'usage' },
        h('p', { class: 'usage-title' }, 'Blocks ', h('strong', { text: plural(names.length, 'package', 'packages') }), ` on ${data.platforms[i]}`),
        nameList(names)));
    });
    if (pkg.dependents.length) {
      section.append(h('div', { class: 'usage' },
        h('p', { class: 'usage-title' }, 'Required directly by ', h('strong', { text: plural(pkg.dependents.length, 'package', 'packages') })),
        nameList(pkg.dependents)));
    } else {
      section.append(h('p', { class: 'pstat-text', text: 'No other package in this channel depends on it.' }));
    }
    return section;
  }

  /* Copying */

  async function copyText(button) {
    const source = $(button.dataset.copy);
    if (!source) return;
    const text = source.textContent;
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      const area = h('textarea', { readonly: true, 'aria-hidden': 'true' });
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.append(area);
      area.select();
      try { ok = document.execCommand('copy'); } catch { ok = false; }
      area.remove();
    }
    const label = button.textContent;
    button.textContent = ok ? 'Copied' : 'Copy failed';
    button.classList.toggle('copied', ok);
    announce(ok ? 'Command copied' : 'Could not copy the command. Select it and copy it manually.');
    setTimeout(() => {
      button.textContent = label === 'Copied' || label === 'Copy failed' ? 'Copy' : label;
      button.classList.remove('copied');
    }, 1600);
  }

  function announce(text) {
    const node = $('announcer');
    node.textContent = '';
    setTimeout(() => { node.textContent = text; }, 30);
  }

  /* Tooltip for the summary bars */

  function showTip(target) {
    const tip = $('tooltip');
    tip.textContent = target.dataset.tip;
    tip.hidden = false;
    const r = target.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    const left = Math.min(window.innerWidth - t.width - 8, Math.max(8, r.left + r.width / 2 - t.width / 2));
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(8, r.top - t.height - 8)}px`;
  }

  /* Persistence of the package manager choice */

  function loadTool() {
    try {
      const tool = localStorage.getItem('biocpkgconda.tool');
      if (tool === 'conda' || tool === 'pixi') state.tool = tool;
    } catch {
      // Storage may be unavailable; conda stays the default.
    }
  }

  function saveTool() {
    try { localStorage.setItem('biocpkgconda.tool', state.tool); } catch { /* not persisted */ }
  }

  /* Events */

  function bind() {
    const search = $('search');
    $('filters').addEventListener('submit', (event) => event.preventDefault());
    search.addEventListener('input', () => {
      state.q = search.value;
      list.active = -1;
      update();
    });
    search.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown' && list.results.length) {
        event.preventDefault();
        $('rows').focus({ preventScroll: true });
        moveActive(list.active < 0 ? 0 : list.active);
      } else if (event.key === 'Escape' && search.value) {
        event.preventDefault();
        search.value = '';
        state.q = '';
        update();
      }
    });

    $('filters').addEventListener('change', (event) => {
      const input = event.target;
      if (input.name === 'platform') {
        state.platform = input.value;
        if (state.pkg) state.treePlatform = defaultTreePlatform(data.byName.get(state.pkg));
      } else if (input.name === 'status') {
        if (input.checked) state.groups.add(input.value); else state.groups.delete(input.value);
      } else if (input.name === 'repo') {
        if (input.checked) state.repos.add(input.value); else state.repos.delete(input.value);
      } else if (input.id === 'cran') {
        state.cran = input.checked;
      } else if (input.id === 'sort') {
        state.sort = input.value === 'blocks' ? 'blocks' : 'name';
      } else {
        return;
      }
      update();
      if (input.name === 'platform' && state.pkg) renderDetail();
    });

    const clear = () => {
      state.q = '';
      state.platform = 'both';
      state.groups.clear();
      state.repos.clear();
      state.cran = false;
      syncControls();
      update();
    };
    $('clear').addEventListener('click', () => {
      clear();
      search.focus();
    });
    $('empty').addEventListener('click', (event) => {
      if (!event.target.closest('[data-action="clear"]')) return;
      clear();
      search.focus();
    });
    $('cran-hint').addEventListener('click', (event) => {
      if (!event.target.closest('[data-action="cran"]')) return;
      state.cran = true;
      syncControls();
      update();
      $('cran').focus();
    });
    document.querySelector('.skip').addEventListener('click', (event) => {
      event.preventDefault();
      search.focus();
    });

    const rows = $('rows');
    rows.addEventListener('keydown', onRowsKey);
    rows.addEventListener('focus', () => {
      if (list.active < 0 && list.results.length) {
        list.active = 0;
        syncActive();
      }
    });
    rows.addEventListener('click', (event) => {
      const row = event.target.closest('.row');
      if (!row) return;
      list.active = Number(row.dataset.index);
      select(list.results[list.active].name, { open: true });
    });

    $('summary-grid').addEventListener('click', (event) => {
      const tile = event.target.closest('.tile');
      if (!tile) return;
      const spec = TILES.find((t) => t.id === tile.dataset.tile);
      state.platform = tile.dataset.platform;
      state.groups = new Set(spec.groups);
      state.repos.clear();
      state.cran = false;
      state.q = '';
      syncControls();
      update();
      $('filters').scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      announce(`Showing packages with status ${spec.label} on ${state.platform}`);
    });
    $('summary-grid').addEventListener('mouseover', (event) => {
      const seg = event.target.closest('.bar-seg');
      if (seg) showTip(seg);
    });
    $('summary-grid').addEventListener('mouseout', (event) => {
      if (event.target.closest('.bar-seg')) $('tooltip').hidden = true;
    });

    const detail = $('detail');
    detail.addEventListener('click', (event) => {
      const link = event.target.closest('[data-pkg]');
      if (link) {
        select(link.dataset.pkg);
        detail.scrollTop = 0;
        $('detail-title')?.focus({ preventScroll: true });
        return;
      }
      const action = event.target.closest('[data-action]');
      if (action?.dataset.action === 'close') closeSheet();
      else if (action?.dataset.action === 'toggle') toggleNode(action.closest('li'));
    });
    detail.addEventListener('change', (event) => {
      const input = event.target;
      if (input.name === 'detail-tool') {
        state.tool = input.value === 'pixi' ? 'pixi' : 'conda';
        saveTool();
        renderInstallLine();
        renderDetailKeepingScroll();
      } else if (input.name === 'tree-platform') {
        state.treePlatform = Number(input.value);
        const old = $('deps-section');
        const fresh = dependencySection(data.byName.get(state.pkg));
        old.replaceWith(fresh);
        fresh.querySelector(`input[name="tree-platform"][value="${state.treePlatform}"]`)?.focus();
      }
    });
    detail.addEventListener('keydown', (event) => {
      if (!detail.classList.contains('open')) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        closeSheet();
      } else if (event.key === 'Tab') {
        const focusable = tabbable(detail);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = document.activeElement;
        if (event.shiftKey && (active === first || active === $('detail-title') || active === detail)) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && active === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });

    $('install-line').addEventListener('change', (event) => {
      if (event.target.name !== 'tool') return;
      state.tool = event.target.value === 'pixi' ? 'pixi' : 'conda';
      saveTool();
      renderInstallLine();
      if (state.pkg) renderDetailKeepingScroll();
    });

    document.addEventListener('click', (event) => {
      const button = event.target.closest('.copy');
      if (button) copyText(button);
    });

    document.addEventListener('keydown', (event) => {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (target.closest('input, textarea, select, [contenteditable="true"]')) return;
      if ($('detail').classList.contains('open')) return;
      event.preventDefault();
      search.focus();
      search.select();
    });

    window.addEventListener('scroll', scheduleWindow, { passive: true });
    window.addEventListener('resize', () => {
      $('tooltip').hidden = true;
      scheduleWindow();
    });
    new ResizeObserver(() => {
      const rowH = measureRow();
      if (rowH !== list.rowH) layoutList();
    }).observe(list.probe);

    wide.addEventListener('change', () => {
      if (wide.matches) closeSheet();
    });

    window.addEventListener('popstate', onPopState);

    window.addEventListener('hashchange', () => {
      const previous = state.pkg;
      readHash();
      syncControls();
      update();
      if (state.pkg !== previous) {
        if (state.pkg) state.treePlatform = defaultTreePlatform(data.byName.get(state.pkg));
        renderDetail();
      }
    });

    setInterval(renderUpdated, 60000);
  }

  function renderDetailKeepingScroll() {
    const detail = $('detail');
    const top = detail.scrollTop;
    const focusedTool = document.activeElement?.name === 'detail-tool' ? document.activeElement.value : null;
    renderDetail();
    detail.scrollTop = top;
    if (focusedTool) detail.querySelector(`input[name="detail-tool"][value="${focusedTool}"]`)?.focus({ preventScroll: true });
  }

  function showError(message) {
    const notice = $('notice');
    notice.className = 'notice error';
    notice.replaceChildren(h('strong', { text: 'The package status could not be shown.' }), message);
    notice.hidden = false;
    $('summary').hidden = true;
    $('workspace').hidden = true;
  }

  async function start() {
    loadTool();
    renderInstallLine();
    const notice = $('notice');
    notice.setAttribute('role', 'status');
    notice.textContent = 'Loading package status.';
    notice.hidden = false;
    try {
      data = normalize(await loadDocument());
    } catch (error) {
      notice.setAttribute('role', 'alert');
      showError(error instanceof Error ? error.message : 'Unknown error.');
      return;
    }
    notice.hidden = true;
    renderHeader();
    renderSummary();
    buildControls();
    readHash();
    if (state.pkg) state.treePlatform = defaultTreePlatform(data.byName.get(state.pkg));
    syncControls();
    $('summary').hidden = false;
    $('workspace').hidden = false;
    list.probe = h('div', { class: 'row-probe', 'aria-hidden': 'true' });
    $('rows').append(list.probe);
    bind();
    update();
    renderDetail();
    if (state.pkg) {
      const index = list.results.findIndex((p) => p.name === state.pkg);
      if (index >= 0) revealRow(index);
      if (!wide.matches) openSheet();
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
