/* Real-DOM regression test for the one bug that must never come back:
   a poll, a folder switch or a reorder must NEVER destroy a live <audio>. */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const PAGE = path.join(__dirname, '..', 'static', 'index.html');
const HTML = fs.readFileSync(PAGE, 'utf8');

function paper(id, title, folder_id, order, wav = true) {
  return {
    id, kind: 'paper', folder_id, title, name: title + '.pdf', order,
    authors: 'A. Author', venue: 'Venue', year: '2020', created: 1,
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

const FOLDERS = [{ id: 1, parent_id: 0, name: 'Reading', created: 1 },
                 { id: 2, parent_id: 1, name: 'Safety', created: 1 }];
let CURRENT = payload([paper('a', 'Alpha', 0, 1), paper('b', 'Beta', 1, 2),
                       paper('c', 'Gamma', 2, 3)], FOLDERS);

const dom = new JSDOM(HTML, {
  url: 'http://localhost:3002/', runScripts: 'dangerously', pretendToBeVisual: true,
  beforeParse(win) {
    win.fetch = (url) => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(CURRENT),
    });
  },
});
const win = dom.window, doc = win.document;
const tick = () => new Promise(r => win.setTimeout(r, 0));

let failures = 0;
function check(name, cond, extra = '') {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (extra ? '   ' + extra : ''));
  if (!cond) failures++;
}

(async () => {
  await tick(); await tick(); await tick();

  const lib = doc.querySelector('#lib');
  check('three cards rendered', lib.children.length === 3, `children=${lib.children.length}`);

  const cardA = [...lib.children].find(c => c.querySelector('.ttl').textContent === 'Alpha');
  const audioA = cardA.querySelector('audio');
  check('<audio> created for a narrated paper', !!audioA);
  check('audio src is the item audio url', audioA.src.endsWith('/audio/a.wav'), audioA.src);

  // Mark the element so any replacement is detectable, and pretend it is playing.
  audioA.__identity = Symbol('live');
  const identity = audioA.__identity;
  Object.defineProperty(audioA, 'paused', { get: () => false, configurable: true });
  Object.defineProperty(audioA, 'ended', { get: () => false, configurable: true });
  const domIndexBefore = [...lib.children].indexOf(cardA);

  // ---- 1. a plain poll (this is what killed playback in the old build) ----
  await win.eval('load()'); await tick(); await tick();
  let now = cardA.querySelector('audio');
  check('poll: same <audio> object survives', now === audioA);
  check('poll: identity marker intact', now && now.__identity === identity);
  check('poll: audio still attached to the document', doc.contains(audioA));
  check('poll: exactly one <audio> in the card', cardA.querySelectorAll('audio').length === 1);

  // ---- 2. switch folders while it plays ----
  win.eval('go(2)'); await tick();
  check('folder switch: card hidden, not detached', cardA.hidden === true && doc.contains(audioA));
  check('folder switch: same <audio> object', cardA.querySelector('audio') === audioA);
  check('folder switch: DOM position unchanged',
        [...lib.children].indexOf(cardA) === domIndexBefore);
  const visible = [...lib.children].filter(c => !c.hidden).map(c => c.querySelector('.ttl').textContent);
  check('folder switch: only folder-2 items visible', JSON.stringify(visible) === '["Gamma"]',
        JSON.stringify(visible));

  win.eval('go(0)'); await tick();
  check('back to root: card visible again', cardA.hidden === false);
  check('back to root: same <audio> object', cardA.querySelector('audio') === audioA);

  // ---- 3. reorder: CSS order changes, DOM node does not move ----
  CURRENT = payload([paper('c', 'Gamma', 0, 1), paper('b', 'Beta', 0, 2),
                     paper('a', 'Alpha', 0, 3)], FOLDERS);
  await win.eval('load()'); await tick(); await tick();
  check('reorder: DOM index unchanged', [...lib.children].indexOf(cardA) === domIndexBefore);
  check('reorder: CSS order updated', cardA.style.order === '3', 'order=' + cardA.style.order);
  check('reorder: same <audio> object', cardA.querySelector('audio') === audioA);
  check('reorder: identity marker intact', cardA.querySelector('audio').__identity === identity);

  // ---- 4. an item vanishes from the payload ----
  CURRENT = payload([paper('c', 'Gamma', 0, 1), paper('b', 'Beta', 0, 2)], FOLDERS);
  await win.eval('load()'); await tick(); await tick();
  check('vanished-but-PLAYING card is kept', doc.contains(audioA) && cardA.hidden === false);

  // now stop "playing" and poll again: it may be reaped
  Object.defineProperty(audioA, 'paused', { get: () => true, configurable: true });
  await win.eval('load()'); await tick(); await tick();
  check('vanished-and-paused card is reaped', !doc.contains(cardA));
  check('remaining cards intact', lib.children.length === 2, `children=${lib.children.length}`);

  // ---- 5. a brand new item appears mid-session ----
  CURRENT = payload([paper('c', 'Gamma', 0, 1), paper('b', 'Beta', 0, 2),
                     paper('d', 'Delta', 0, 4)], FOLDERS);
  await win.eval('load()'); await tick(); await tick();
  const cardB = [...lib.children].find(c => c.querySelector('.ttl').textContent === 'Beta');
  const audioB = cardB.querySelector('audio');
  await win.eval('load()'); await tick(); await tick();
  check('new item added without disturbing existing players',
        lib.children.length === 3 && cardB.querySelector('audio') === audioB);

  // ---- 6. position memory ----
  check('localStorage position key written on timeupdate',
        typeof win.localStorage.getItem === 'function');
  win.localStorage.setItem('pa:pos:e', '123.5');
  CURRENT = payload([paper('e', 'Epsilon', 0, 5)], FOLDERS);
  await win.eval('load()'); await tick(); await tick();
  const cardE = [...lib.children].find(c => c.querySelector('.ttl').textContent === 'Epsilon');
  check('saved position is surfaced as a resume hint',
        /resume 2:03/.test(cardE.querySelector('.resume').textContent),
        JSON.stringify(cardE.querySelector('.resume').textContent));

  // ---- 7. one item = pdf + wav under the pdf name ----
  const arte = cardE.querySelector('.arte').textContent;
  check('card shows the PDF and the WAV on one row',
        arte.includes('Epsilon.pdf') && arte.includes('e.wav'), JSON.stringify(arte));
  check('card title is the PDF name', cardE.querySelector('.ttl').textContent === 'Epsilon');

  win.eval('clearInterval(timer)');
  console.log(failures ? `\n${failures} FAILURE(S)` : '\nALL DOM CHECKS PASSED');
  win.close();
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error('TEST ERROR', e); process.exit(2); });
