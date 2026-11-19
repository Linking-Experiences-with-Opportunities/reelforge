// Reference capture for a `clip` (changelog / docs embed): hi-res frames + a directed camera. Pairs with examples/feature.clip.json.
// Selectors are placeholders: replace them with your app's.
// Run from your app's repo:  KIT="$KIT" BASE_URL=http://localhost:3000 node clip-capture.example.cjs   then follow "Clip format" in ../README.md
const kit = require(process.env.KIT + '/scripts/capture-kit.cjs');
const baseURL = process.env.BASE_URL || 'http://localhost:3000';
(async () => {
  const s = await kit.start({ playwright: require('playwright'), baseURL, outDir: './raw', hires: true });   // true 2880x1800 frames
  const me = await s.seat('main');
  const p = me.page;
  await p.goto(baseURL + '/settings/team', { waitUntil: 'networkidle' }); await me.wait(2000);
  // ...set up state OFF camera here: the clip starts at the first beat...
  await s.beat('main', 'go');
  // Camera regions come from measured element boxes (CSS px). Each zoomed shot must be <= 900 px wide (500 for narrow embeds).
  const form = await p.getByRole('button', { name: 'Invite' }).boundingBox();
  await me.cam({ x: form.x - 300, y: form.y - 80, w: 760 }); await me.wait(800);   // move the camera, THEN act: the click gets an accent ring
  await me.click(p.getByRole('button', { name: 'Invite' })); await me.wait(800);
  await me.type(p.getByLabel('Email'), 'sam@example.com'); await me.wait(600);
  const list = await p.getByRole('table').boundingBox();
  await me.click(p.getByRole('button', { name: 'Send invite' })); await me.wait(400);
  await me.cam({ x: list.x, y: list.y - 40, w: 860 }); await me.wait(2400);        // pan to where the result appears
  const out = await s.finish();                                                  // raw/main.mp4 (2880x1800), marks.json, focus.json
  console.log(out.videos, out.focus.length + ' focus events');
})().catch((e) => { console.error(e); process.exit(1); });
