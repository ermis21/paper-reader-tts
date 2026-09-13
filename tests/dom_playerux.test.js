/* Real-DOM tests for the player-UX layer: mutual exclusion between players,
   the now-playing marker, the speed control, client-side search and sort --
   all under the same invariant as dom_playback.test.js: nothing may detach a
   live <audio>. */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const PAGE = path.join(__dirname, '..', 'static', 'index.html');
const HTML = fs.readFileSync(PAGE, 'utf8');

function paper(id, title, folder_id, order, wav = true) {
  return {
    id, kind: 'paper', folder_id, title, name: title + '.pdf', order,
    authors: 'A. Author', venue: 'Venue', year: '2020', created: order,
    pdf: { present: true, bytes: 1000, url: '/pdf/' + id + '.pdf', name: title + '.pdf' },
    text: { present: true, bytes: 100, url: '/text/' + id + '.txt' },
    audio: wav ? { present: true, bytes: 5000, seconds: 900, url: '/audio/' + id + '.wav', name: id + '.wav' }
                : { present: false, bytes: 0, seconds: 0, url: null, name: null },
    chunks: wav ? 40 : 0, suspect: 0, state: wav ? 'narrated' : 'not rendered',
  };
}
function payload(items, folders = []) {
  const papers = items.filter(i => i.kind === 'paper');
  return {
    folders, items, jobs: [],
    total_seconds: papers.reduce((a, b) => a + b.audio.seconds, 0),
    total_chunks: papers.reduce((a, b) => a + b.chunks, 0),
    suspect: 0, ready: papers.filter(p => p.audio.present).length,
    documents: papers.length, files: items.length - papers.length,
    m4b: false, host: 'test',
  };
}

let CURRENT = payload([paper('a', 'Alpha', 0, 1), paper('b', 'Beta', 0, 2),
                       paper('c', 'Gamma', 0, 3)]);

const dom = new JSDOM(HTML, {
  url: 'http://localhost:3002/', runScripts: 'dangerously', pretendToBeVisual: true,
  beforeParse(win) {
    win.fetch = () => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(CURRENT),
    });
  },
});
const win = dom.window, doc = win.document;
const tick = () => new Promise(r => win.setTimeout(r, 0));
const fire = (el, type) => el.dispatchEvent(new win.Event(type, { bubbles: true }));

let failures = 0;
function check(name, cond, extra = '') {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (extra ? '   ' + extra : ''));
  if (!cond) failures++;
}

(async () => {
  await tick(); await tick(); await tick();

  const lib = doc.querySelector('#lib');
  const cardOf = t => [...lib.children].find(c => c.querySelector('.ttl').textContent === t);
  const cardA = cardOf('Alpha'), cardB = cardOf('Beta'), cardC = cardOf('Gamma');
  const audioA = cardA.querySelector('audio'), audioB = cardB.querySelector('audio');
  check('three cards rendered', lib.children.length === 3, `children=${lib.children.length}`);

  // ---- 1. search hides non-matching cards, never detaches audio ----
  const q = doc.querySelector('#q');
  q.value = 'alph'; fire(q, 'input');
  check('search: non-matching cards hidden', cardB.hidden === true && cardC.hidden === true);
  check('search: match stays visible', cardA.hidden === false);
  check('search: no <audio> detached', doc.contains(audioA) && doc.contains(audioB));
  q.value = ''; fire(q, 'input');
  check('search cleared: cards visible again', cardB.hidden === false && cardC.hidden === false);

  // search also matches authors
  q.value = 'author'; fire(q, 'input');
  check('search: matches on author field', cardA.hidden === false);
  q.value = ''; fire(q, 'input');

  // ---- 2. sort reorders via style.order only ----
  const sort = doc.querySelector('#sort');
  sort.value = '-title'; fire(sort, 'change');
  check('sort Z-A: Gamma first by CSS order', cardC.style.order === '1' && cardA.style.order === '3',
        `a=${cardA.style.order} c=${cardC.style.order}`);
  check('sort: DOM nodes never move', [...lib.children].indexOf(cardA) === 0);
  check('sort: no <audio> detached', doc.contains(audioA) && doc.contains(audioB));
  sort.value = 'order'; fire(sort, 'change');
  check('sort back to manual order', cardA.style.order === '1' && cardC.style.order === '3');
  check('sort choice persisted', win.localStorage.getItem('pa:sort') === 'order');

  // ---- 3. one player at a time + now-playing marker ----
  let pausedA = false;
  audioA.pause = () => { pausedA = true; };
  audioB.dispatchEvent(new win.Event('play'));
  check('starting one player pauses the others', pausedA === true);
  check('now-playing card is marked', cardB.classList.contains('playing'));
  audioB.dispatchEvent(new win.Event('pause'));
  check('pause clears the now-playing marker', !cardB.classList.contains('playing'));

  // ---- 4. speed control ----
  const rate = cardA.querySelector('.rate');
  check('speed control exists on the player', !!rate);
  rate.value = '1.5'; fire(rate, 'change');
  check('speed control sets playbackRate', audioA.playbackRate === 1.5, String(audioA.playbackRate));
  check('speed persisted per document', win.localStorage.getItem('pa:rate:a') === '1.5');

  // ---- 5. dialogs replace prompt()/confirm() ----
  cardA.querySelector('.b-rename').click();
  check('rename opens the modal', doc.querySelector('#modal').hidden === false);
  doc.querySelector('#mcancel').click();
  check('cancel closes the modal', doc.querySelector('#modal').hidden === true);

  // ---- 6. theme toggle ----
  doc.querySelector('#themebtn').click();
  check('theme toggle sets data-theme', doc.documentElement.getAttribute('data-theme') === 'dark',
        doc.documentElement.getAttribute('data-theme'));
  check('theme persisted', win.localStorage.getItem('pa:theme') === 'dark');

  // ---- 7. a failed render is visible and retryable ----
  CURRENT = payload([paper('a', 'Alpha', 0, 1)]);
  CURRENT.jobs = [{ id: 'a', title: 'Alpha', state: 'error', stage: 'error',
                    pct: 0, error: 'synth blew up', recent: true }];
  await win.eval('load()'); await tick(); await tick();
  const pill = cardA.querySelector('.pill');
  check('failed render: distinct pill', pill.className.includes('p-fail') &&
        pill.textContent === 'render failed', pill.textContent);
  const err = cardA.querySelector('.err');
  check('failed render: error text on the card',
        err.hidden === false && err.textContent.includes('synth blew up'),
        JSON.stringify(err.textContent));
  check('failed render: card button offers retry',
        cardA.querySelector('.b-render').textContent === 'Retry');
  check('failed render: queue row has a retry button', !!doc.querySelector('#queue .qretry'));
  check('failed render: queue row shows the error',
        (doc.querySelector('#queue .qerr') || {}).textContent === 'synth blew up');

  // ---- 8. poll failure raises the banner, recovery drops it ----
  const realFetch = win.fetch;
  win.fetch = () => Promise.reject(new Error('down'));
  await win.eval('load()'); await tick(); await tick();
  check('dead backend: banner visible', doc.querySelector('#netbar').hidden === false);
  win.fetch = realFetch;
  await win.eval('load()'); await tick(); await tick();
  check('backend back: banner gone', doc.querySelector('#netbar').hidden === true);

  win.eval('clearInterval(timer)');
  console.log(failures ? `\n${failures} FAILURE(S)` : '\nALL PLAYER-UX CHECKS PASSED');
  win.close();
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error('TEST ERROR', e); process.exit(2); });
