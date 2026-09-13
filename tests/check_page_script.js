/* Syntax-check the inline page script, and statically assert the DOM
   invariant that keeps playback alive.
 *
 * The page is one self-contained HTML file, so `node --check` cannot be
 * pointed at it directly: this pulls the <script> body out and compiles it
 * with vm.Script, which is exactly what `node --check` does to a file.
 *
 * The grep-style assertions afterwards guard the rule the whole front end is
 * built around: #lib holds live <audio> elements, so it must never have its
 * innerHTML assigned. Rebuilding it on every poll is the bug that stopped
 * playback after two or three seconds. jsdom proves the behaviour at runtime
 * (dom_playback.test.js); this catches the mistake at a glance, with no
 * dependencies, so `npm test` is useful even before `npm i`.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PAGE = path.join(__dirname, '..', 'static', 'index.html');
const html = fs.readFileSync(PAGE, 'utf8');

let failures = 0;
const check = (name, cond, extra = '') => {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (extra ? '   ' + extra : ''));
  if (!cond) failures++;
};

const blocks = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(m => m[1]);
check('page contains exactly one inline script', blocks.length === 1, `found ${blocks.length}`);

const src = blocks.join('\n');
try {
  new vm.Script(src, { filename: 'static/index.html <script>' });
  check('inline script parses (node --check equivalent)', true);
} catch (e) {
  check('inline script parses (node --check equivalent)', false, e.message);
}

// The invariant. Any of these would tear a playing <audio> out of the DOM.
const banned = [
  [/\$\(\s*['"]#lib['"]\s*\)\s*\.innerHTML\s*=/, "$('#lib').innerHTML = ..."],
  [/\blib\s*\.innerHTML\s*=/, 'lib.innerHTML = ...'],
  [/getElementById\(\s*['"]lib['"]\s*\)\s*\.innerHTML\s*=/, "getElementById('lib').innerHTML = ..."],
];
for (const [rx, label] of banned) {
  check(`#lib is never rebuilt via innerHTML  (${label})`, !rx.test(src));
}
check('cards are created once, guarded by an existence check',
      /!slot\.querySelector\(\s*['"]audio['"]\s*\)/.test(src));
check('cards are hidden, not detached, on a folder switch',
      /el\.hidden\s*=/.test(src));
check('reordering happens in CSS, not by moving DOM nodes',
      /style\.order\s*=/.test(src));
check('a card whose audio is playing is never reaped',
      /if\s*\(\s*playing\(\s*el\s*\)\s*\)/.test(src));

console.log(failures ? `\n${failures} FAILURE(S)` : '\nPAGE SCRIPT OK');
process.exit(failures ? 1 : 0);
