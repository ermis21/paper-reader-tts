/* Real-DOM tests for creating folders by hand: the toolbar button makes one in
   the folder on screen, a tree row's + makes one inside that row's folder, and
   the folder actions stay reachable on a device with no hover (a phone). Also:
   search still finds a paper by the name it was uploaded as after its card was
   retitled from the PDF. */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const PAGE = path.join(__dirname, '..', 'static', 'index.html');
const HTML = fs.readFileSync(PAGE, 'utf8');

const paper = (id, title, folder_id, orig) => ({
  id, kind: 'paper', folder_id, title, name: title + '.pdf', orig_name: orig, title_source: 'layout',
  order: 1, authors: 'uploaded', venue: 'uploaded', year: '', created: 1,
  pdf: { present: true, bytes: 1000, url: '/pdf/' + id + '.pdf', name: title + '.pdf' },
  text: { present: false, bytes: 0, url: null },
  audio: { present: false, bytes: 0, seconds: 0, url: null, name: null },
  chunks: 0, suspect: 0, state: 'not rendered',
});
const CURRENT = {
  folders: [{ id: 1, parent_id: 0, name: 'Scaling', created: 1 }],
  items: [paper('chinchilla', 'Training Compute-Optimal Large Language Models', 1, 'Chinchilla.pdf')],
  jobs: [], total_seconds: 0, total_chunks: 0, suspect: 0, ready: 0,
  documents: 1, files: 0, m4b: false, host: 'test',
};

const calls = [];
const dom = new JSDOM(HTML, {
  url: 'http://localhost:3002/', runScripts: 'dangerously', pretendToBeVisual: true,
  beforeParse(win) {
    win.fetch = (url, opts = {}) => {
      const method = opts.method || 'GET';
      const body = opts.body ? JSON.parse(opts.body) : null;
      if (method !== 'GET') calls.push({ url, method, body });
      const reply = method === 'POST' && url === '/api/folders'
        ? { id: 2, parent_id: body.parent_id, name: body.name } : CURRENT;
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(reply) });
    };
  },
});
const win = dom.window, doc = win.document;
const tick = () => new Promise(r => win.setTimeout(r, 0));
const ticks = async (n = 4) => { for (let i = 0; i < n; i++) await tick(); };

let failures = 0;
function check(name, cond, extra = '') {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (extra ? '   ' + extra : ''));
  if (!cond) failures++;
}

(async () => {
  await ticks();
  const modal = doc.querySelector('#modal'), input = doc.querySelector('#minput');
  const create = async name => { input.value = name; doc.querySelector('#mok').click(); await ticks(); };
  const row = fid => doc.querySelector(`#tree .tnode[data-fid="${fid}"]`);

  // ---- 1. toolbar button, at the workspace root ----
  const nf = doc.querySelector('#newfolder');
  check('toolbar New folder button is a visible (non-quiet) button',
        !!nf && !nf.classList.contains('quiet'), nf ? nf.className : 'missing');
  nf.click(); await tick();
  check('New folder opens the name prompt', modal.hidden === false && input.hidden === false);
  await create('Reading list');
  let c = calls.pop();
  check('creates it with POST /api/folders', !!c && c.method === 'POST' && c.url === '/api/folders');
  check('... named as typed, in the root', !!c && c.body.name === 'Reading list' && c.body.parent_id === 0,
        JSON.stringify(c && c.body));
  check('prompt closes after creating', modal.hidden === true);

  nf.click(); await tick();
  input.value = '   '; doc.querySelector('#mok').click(); await ticks();
  check('a blank name is refused and the prompt stays open', modal.hidden === false && calls.length === 0);
  doc.querySelector('#mcancel').click(); await tick();
  check('cancel creates nothing', modal.hidden === true && calls.length === 0);

  // ---- 2. toolbar button, inside the folder on screen ----
  row(1).querySelector('.tlbl').click(); await ticks();
  nf.click(); await tick();
  await create('Replications');
  c = calls.pop();
  check('toolbar creates inside the folder on screen', !!c && c.body.parent_id === 1, JSON.stringify(c && c.body));

  // ---- 3. the tree row's + ----
  row(0).querySelector('.tlbl').click(); await ticks();          // back to the root view
  const plus = row(1).querySelector('.fbtn[data-act="sub"]');
  check('each folder row in the tree has a + (new folder inside)', !!plus);
  plus.click(); await tick();
  check('+ opens the prompt, naming the parent', modal.hidden === false &&
        doc.querySelector('#mtitle').textContent.includes('Scaling'), doc.querySelector('#mtitle').textContent);
  await create('Chinchilla follow-ups');
  c = calls.pop();
  check('+ creates inside that row\'s folder, not the one on screen', !!c && c.body.parent_id === 1,
        JSON.stringify(c && c.body));

  // ---- 4. no hover, no invisible buttons ----
  check('folder actions are shown on no-hover devices',
        /@media \(hover:none\)\{\.tnode \.fbtn\{opacity:1/.test(HTML));

  // ---- 5. search by the name it was uploaded as ----
  row(1).querySelector('.tlbl').click(); await ticks();
  const q = doc.querySelector('#q');
  q.value = 'chinchilla'; q.dispatchEvent(new win.Event('input', { bubbles: true }));
  const card = [...doc.querySelector('#lib').children]
    .find(el => el.querySelector('.ttl').textContent.startsWith('Training Compute-Optimal'));
  check('search finds a retitled paper by its upload name', !!card && card.hidden === false);

  win.eval('clearInterval(timer)');
  console.log(failures ? `\n${failures} FAILURE(S)` : '\nALL FOLDER CHECKS PASSED');
  win.close();
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error('TEST ERROR', e); process.exit(2); });
