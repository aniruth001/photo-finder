/* Originals, fetched fast and kept: shared by the dashboard and the editor (window.PFOriginal).

   A big original (40 MB) is slow over a home line or a phone's hotspot behind a tunnel, and a tunnel often limits
   each connection, so:
   - it is downloaded in pieces at once (HTTP ranges, several connections), not as one stream;
   - the dashboard starts it while you look at a photo in the viewer, so it is usually here when you choose Edit;
   - the editor's tab joins a download another tab has started (BroadcastChannel) instead of starting a second one;
   - the last few are kept in the browser (IndexedDB), so opening a photo again is instant. Signing out clears them. */
(function () {
  const DB = 'pf-originals', STORE = 'files', KEEP = 8, MAX_BYTES = 800 * 1048576;
  // pieces at once: over HTTPS (HTTP/2, one shared connection) six; over plain HTTP the browser allows six connections to
  // a server in all, so three, leaving room for the page's own requests and live updates. The first piece tells the size.
  const PARTS = location.protocol === 'https:' ? 6 : 3, PIECE = 2 * 1048576;
  const flights = new Map();             // url -> {promise, got, total, ctrl, prefetch, shared, listeners}
  const bc = window.BroadcastChannel ? new BroadcastChannel('pf-originals') : null;
  const remote = new Map();              // url -> {got, total, waiters} : downloads running in another tab

  // ---- the store
  let dbp = null;
  function idb() {
    if (!dbp) dbp = new Promise((res, rej) => {
      if (!window.indexedDB) return rej(new Error('no IndexedDB'));
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => r.result.createObjectStore(STORE);
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    });
    return dbp;
  }
  async function get(key) {
    try {
      const d = await idb();
      return await new Promise(res => { const q = d.transaction(STORE).objectStore(STORE).get(key);
        q.onsuccess = () => res(q.result ? q.result.blob : null); q.onerror = () => res(null); });
    } catch (e) { return null; }
  }
  async function put(key, blob) {
    try {
      const d = await idb();
      await new Promise(res => {
        const tx = d.transaction(STORE, 'readwrite'), st = tx.objectStore(STORE), all = [];
        st.put({ blob, at: Date.now(), size: blob.size }, key);
        st.openCursor().onsuccess = e => { const c = e.target.result;
          if (c) { all.push([c.key, c.value.at, c.value.size]); c.continue(); return; }
          all.sort((a, b) => b[1] - a[1]); let tot = 0;   // newest first: keep a few, within a size
          all.forEach(([k, , s], i) => { tot += s; if (i >= KEEP || tot > MAX_BYTES) st.delete(k); }); };
        tx.oncomplete = tx.onerror = tx.onabort = () => res();
      });
    } catch (e) {}
  }
  async function clear() { try { const d = await idb(); d.transaction(STORE, 'readwrite').objectStore(STORE).clear(); } catch (e) {} }

  // ---- the download: pieces at once
  async function failure(r) { let j = {}; try { j = await r.json(); } catch (e) {} return new Error(j.message || "This photo couldn't be opened for editing."); }
  async function readAll(r, add) { const rd = r.body.getReader(), out = [];
    for (;;) { const { done, value } = await rd.read(); if (done) break; out.push(value); add(value.length); } return out; }
  async function download(url, f) {
    const opt = range => ({ credentials: 'same-origin', signal: f.ctrl.signal, headers: { Range: range } });
    const add = n => { f.got += n; tell(f, url); };
    const first = await fetch(url, opt(`bytes=0-${PIECE - 1}`));
    if (!first.ok) throw await failure(first);
    const type = first.headers.get('content-type') || '';
    if (first.status !== 206) {   // no ranges here (a converted RAW or HEIC): one stream
      f.total = +first.headers.get('content-length') || 0;
      return new Blob(await readAll(first, add), { type });
    }
    f.total = +((first.headers.get('content-range') || '').split('/')[1]) || 0;
    const head = await readAll(first, add);
    const start = f.got, rest = f.total - start;
    if (rest <= 0) return new Blob(head, { type });
    const n = Math.max(1, Math.min(PARTS, Math.ceil(rest / PIECE))), size = Math.ceil(rest / n);
    const parts = await Promise.all([...Array(n)].map(async (_, i) => {
      const a = start + i * size, b = Math.min(f.total, a + size) - 1;
      const r = await fetch(url, opt(`bytes=${a}-${b}`));
      if (r.status !== 206) throw await failure(r);
      return new Blob(await readAll(r, add));
    }));
    return new Blob([...head, ...parts], { type });
  }
  let lastTell = 0;
  function tell(f, url) {
    f.listeners.forEach(fn => { try { fn(f.got, f.total); } catch (e) {} });
    const now = Date.now(); if (bc && f.shared && now - lastTell > 300) { lastTell = now; bc.postMessage({ t: 'prog', url, got: f.got, total: f.total }); }
  }
  function start(url, prefetch) {
    const f = { got: 0, total: 0, ctrl: new AbortController(), prefetch, shared: false, listeners: new Set() };
    f.promise = download(url, f).then(async blob => { await put(url, blob); if (bc) bc.postMessage({ t: 'done', url }); return blob; })
      .catch(e => { if (bc) bc.postMessage({ t: 'fail', url }); throw e; })
      .finally(() => flights.delete(url));
    flights.set(url, f);
    return f;
  }

  // ---- tabs helping each other
  if (bc) {
    bc.onmessage = ({ data: m }) => {
      if (!m || !m.url) return;
      if (m.t === 'who') { const f = flights.get(m.url); if (f) { f.shared = true; f.prefetch = false; bc.postMessage({ t: 'have', url: m.url, got: f.got, total: f.total }); } return; }
      const w = remote.get(m.url); if (!w) return;
      if (m.t === 'have' || m.t === 'prog') { w.seen = Date.now(); w.got = m.got; w.total = m.total; w.progress(m.got, m.total); }
      else if (m.t === 'done' || m.t === 'fail') w.end(m.t);
    };
    addEventListener('pagehide', () => flights.forEach((f, url) => { if (f.shared) bc.postMessage({ t: 'fail', url }); }));
  }
  function fromOtherTab(url, onProgress) {   // -> 'done' | 'fail' | null (nobody has it)
    if (!bc) return Promise.resolve(null);
    return new Promise(res => {
      const w = { seen: 0, got: 0, total: 0, progress: onProgress || (() => {}), end: t => { clearInterval(w.timer); remote.delete(url); res(t); } };
      remote.set(url, w); bc.postMessage({ t: 'who', url });
      w.timer = setInterval(() => {
        if (!w.seen && Date.now() - w.asked > 400) w.end(null);        // nobody answered
        else if (w.seen && Date.now() - w.seen > 20000) w.end('fail'); // that tab went quiet
      }, 200);
      w.asked = Date.now();
    });
  }

  window.PFOriginal = {
    /* The original as a Blob: kept, joined from another tab, joined from this one, or downloaded. onProgress(got, total). */
    async load(url, onProgress) {
      const kept = await get(url); if (kept) return kept;
      let f = flights.get(url);
      if (!f) {
        const other = await fromOtherTab(url, onProgress);
        if (other === 'done') { const b = await get(url); if (b) return b; }
        f = flights.get(url) || start(url, false);
      }
      f.prefetch = false; if (onProgress) { f.listeners.add(onProgress); onProgress(f.got, f.total); }
      return f.promise;
    },
    async has(url) { return !!(await get(url)); },
    /* Start fetching while the user looks at the photo; one at a time. */
    async prefetch(url) {
      flights.forEach((f, u) => { if (u !== url && f.prefetch && !f.shared) f.ctrl.abort(); });
      if (flights.has(url) || await get(url)) return;
      start(url, true).promise.catch(() => {});
    },
    cancelPrefetch() { flights.forEach(f => { if (f.prefetch && !f.shared) f.ctrl.abort(); }); },
    clear,
  };
})();
