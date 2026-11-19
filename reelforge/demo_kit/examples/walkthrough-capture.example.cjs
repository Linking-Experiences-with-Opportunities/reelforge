// Reference capture for a WALKTHROUGH (the default format), shot for examples/walkthrough.demo.json.
// The URLs and selectors are placeholders for an imaginary "team invites" feature: replace them with your app's.
// Run from your app's repo (so `playwright` resolves), dev server up:
//   KIT="$KIT" BASE_URL=http://localhost:3000 node "$KIT/examples/walkthrough-capture.example.cjs"
// Records into $PROJECT/raw (default ~/Documents/Demos/projects/team-invites/raw). Then, from $PROJECT:
//   cp "$KIT/examples/walkthrough.demo.json" demo.json
//   node "$KIT/scripts/detect-beats.mjs" --marks raw/marks.json --video main=raw/main.mp4 --out beats.json
//   node "$KIT/scripts/build-demo.mjs" --spec demo.json --beats beats.json --project hf --stage prepare   # LOOK at the contact sheets
//   node "$KIT/scripts/build-demo.mjs" --spec demo.json --project hf --stage render --out ~/Documents/Demos/features/team-invites-walkthrough.mp4
const os = require('node:os'), path = require('node:path');
const kit = require(process.env.KIT + '/scripts/capture-kit.cjs');
const baseURL = process.env.BASE_URL || 'http://localhost:3000';
const PROJECT = process.env.PROJECT || path.join(os.homedir(), 'Documents/Demos/projects/team-invites');
(async () => {
  const s = await kit.start({
    playwright: require('playwright'), baseURL, outDir: path.join(PROJECT, 'raw'),
    hideText: [],                 // exact text of your app's dev-only floating buttons, e.g. ['Reset onboarding']
    hideSelectors: '',            // or CSS selectors for them
  });
  const me = await s.seat('main' /*, { cookies: [kit.supabaseCookie(session, baseURL)] } */);
  const p = me.page;

  // set up OFF camera: nothing before the first beat is used
  await p.goto(baseURL + '/settings/team', { waitUntil: 'networkidle' });
  await me.wait(1500);

  await s.beat('main', 'settings');                         // chapter 1 starts here
  await me.wait(2000);

  await s.beat('main', 'invite');                           // chapter 2
  await me.click(p.getByRole('button', { name: 'Invite' }));
  await me.type(p.getByLabel('Email'), 'sam@example.com');  // demo addresses only
  await me.click(p.getByLabel('Role'));
  await me.click(p.getByRole('option', { name: 'Editor' }));
  await me.click(p.getByRole('button', { name: 'Send invite' }));
  await me.wait(1200);

  await s.beat('main', 'pending');                          // chapter 3
  await me.wait(3500);                                      // hold on the result

  const out = await s.finish();                             // raw/main.mp4 (25 fps, libx264) + marks.json
  console.log(out.videos, out.marksPath);
})().catch((e) => { console.error(e); process.exit(1); });
