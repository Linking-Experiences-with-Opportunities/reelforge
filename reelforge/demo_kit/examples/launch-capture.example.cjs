// Reference capture for a LAUNCH FILM (format=launch), shot for examples/feature.launch.json.
// Hi-res frames with NO cursor baked in (build-launch draws the oversized arrow from mouse.json), one beat per footage shot, a directed
// camera before every action that frames the control AND what it changes, press() for clicks that change the layout.
// Selectors are placeholders for an imaginary "team invites" feature: replace them with your app's.
// Run from your app's repo:  KIT="$KIT" BASE_URL=http://localhost:3000 node "$KIT/examples/launch-capture.example.cjs"
// It records into $PROJECT/raw (default ~/Documents/Demos/projects/team-invites-launch/raw). The spec's paths resolve against its own
// folder, so put the spec beside raw/:   cp "$KIT/examples/feature.launch.json" "$PROJECT/launch.json"
// then, from $PROJECT: detect-beats --focus --mouse, build-launch --stage prepare, LOOK, --stage render ("Launch format" in ../README.md).
const os = require('node:os'), path = require('node:path');
const kit = require(process.env.KIT + '/scripts/capture-kit.cjs');
const baseURL = process.env.BASE_URL || 'http://localhost:3000';
const PROJECT = process.env.PROJECT || path.join(os.homedir(), 'Documents/Demos/projects/team-invites-launch');
(async () => {
  const s = await kit.start({ playwright: require('playwright'), baseURL, outDir: path.join(PROJECT, 'raw'), hires: true, cursor: 'none' });
  const me = await s.seat('main');
  const p = me.page;
  // Launch films are light. If your app has a theme switch, force light before the first goto, e.g.:
  // await me.context.addInitScript(() => { try { localStorage.setItem('theme', 'light'); } catch {} });
  const box = async (loc) => loc.first().boundingBox();

  // ---- set up OFF camera (nothing before the first beat is used) ----
  await p.goto(baseURL + '/settings/team', { waitUntil: 'networkidle' });
  await me.moveTo(700, 470);                                   // park the mouse where the film's cursor first appears
  await me.wait(2000);

  // ---- shot 1 (screen): open the invite form ----
  await s.beat('main', 'open');
  await me.wait(1600);                                         // the wide establishing framing
  const header = await box(p.getByRole('button', { name: 'Invite' }));
  await me.cam({ x: header.x - 420, y: header.y - 60, w: 700 }); // camera FIRST, then the action
  await me.wait(1000);
  await me.press(p.getByRole('button', { name: 'Invite' }));   // held press: it reads before the dialog opens
  await s.beat('main', 'pressed');
  await me.wait(900);

  // ---- shot 2 (focus): type the email, the input isolated big on the canvas ----
  await s.beat('main', 'email');
  await me.look(p.getByLabel('Email'));
  await me.type(p.getByLabel('Email'), 'sam@example.com', 120);
  await me.wait(1200);

  // ---- shot 3 (screen): send, then the pending row appears ----
  await s.beat('main', 'send');
  const dialog = await box(p.getByRole('dialog'));
  await me.cam({ x: dialog.x - 20, y: dialog.y - 20, w: 720 }); await me.wait(900);
  await me.click(p.getByRole('button', { name: 'Send invite' })); await me.wait(700);
  const list = await box(p.getByRole('table'));
  await me.cam({ x: list.x - 20, y: list.y - 40, w: 760 }); await me.wait(2200);

  const out = await s.finish();                                // raw/main.mp4 (2880x1800), marks.json, focus.json, mouse.json
  console.log(out.videos, out.focus.length + ' focus events', out.mouse.length + ' mouse events');
})().catch((e) => { console.error(e); process.exit(1); });
