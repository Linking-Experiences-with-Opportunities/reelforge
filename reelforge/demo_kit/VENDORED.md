# Where this folder comes from

This is the demo kit from [record-demo-kit](https://github.com/maxMyers13/record-demo-kit)
(MIT, commit `d82b55d`), copied in so the helper can record demos on a computer that has
nothing but reelforge and a coding agent.

- `SKILL.md` is that repo's `plugins/record-demo/commands/record-demo.md`.
- Everything else is its `plugins/record-demo/demo-kit/`, except `brand/`, which holds the
  Pathion brand pack (logos, colors) in place of the neutral example.

Changed here, and not yet in that repo (bring them over, or re-apply after an update):

- the render commands in `SKILL.md` and the walkthrough example keep `--beats`;
- `scripts/brand.mjs`: `npx()` starts npx through a shell on Windows, and GSAP downloads to a
  temporary file first so a broken download is retried;
- `scripts/capture-kit.cjs` writes absolute frame paths, so a relative `outDir` works;
- `scripts/build-launch.mjs`: a numeric narration time in a footage shot is a source time;
- the capture examples find `playwright` from the folder they are run in.

To update: copy the two paths again, keep `brand/` and this file, and re-apply the list above.
