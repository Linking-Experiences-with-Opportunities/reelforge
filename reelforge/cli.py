"""reelforge command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import analyze as analyze_mod
from . import captions as captions_mod
from . import ingest as ingest_mod
from . import recipe as recipe_mod
from . import render as render_mod
from . import sheet as sheet_mod
from .shell import ToolError, have

DEFAULT_PROJECTS = Path.home() / "reelforge-projects"


def _print(message: str = "") -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #

def cmd_ingest(args: argparse.Namespace) -> int:
    source = ingest_mod.ingest(
        args.source,
        projects_root=Path(args.projects).expanduser(),
        cookies_from_browser=args.cookies_from_browser,
        cookies_file=Path(args.cookies).expanduser() if args.cookies else None,
        name=args.name,
        timeout=args.download_timeout,
    )
    _print(f"-> project {source.project_dir}")
    _print(f"   video   {source.video.name}")

    probe = ingest_mod.probe(source.video)
    _print(f"   {probe['width']}x{probe['height']} {probe['orientation']}"
           f" @ {probe['fps']}fps  |  {probe['duration']}s")

    _print("   analyzing cuts, audio, on-screen text and visuals ...")
    analysis = analyze_mod.analyze(
        source.video,
        probe,
        source.project_dir,
        do_transcribe=not args.no_transcribe,
        do_ocr=not args.no_ocr,
        do_visuals=not args.no_visuals,
        whisper_model=args.whisper_model,
        scene_threshold=args.scene_threshold,
        frame_fps=args.frame_fps,
    )

    analysis_path = source.project_dir / "analysis.json"
    analysis_path.write_text(json.dumps(analysis.as_dict(), indent=2) + "\n", "utf-8")

    built = recipe_mod.build_recipe(
        analysis,
        name=source.slug,
        origin=source.origin,
        platform=source.platform,
        meta=source.meta,
        copy_source_text=not args.blank_text,
    )
    recipe_path = source.project_dir / "recipe.json"
    recipe_mod.save_recipe(built, recipe_path)

    sheet_path = None
    if analysis.frames and sheet_mod.PILLOW_AVAILABLE:
        slot_map, kind_map = sheet_mod.expand_maps(built, analysis.frames)
        sheet_path = sheet_mod.build_contact_sheet(
            analysis.frames,
            source.project_dir / "contact-sheet.jpg",
            columns=args.sheet_columns,
            annotate=True,
            slot_by_time=slot_map,
            kind_by_time=kind_map,
        )
        sheet_mod.build_contact_sheet(
            analysis.frames,
            source.project_dir / "contact-sheet-plain.jpg",
            columns=args.sheet_columns,
            annotate=False,
        )

    _print()
    _print(recipe_mod.describe(built))
    _print()
    _print(f"   recipe   {recipe_path}")
    _print(f"   analysis {analysis_path}")
    if sheet_path:
        _print(f"   sheet    {sheet_path}")
        _print(f"            {sheet_path.parent / 'contact-sheet-plain.jpg'} (unlabelled)")
    _print()

    required = (built.get("assets") or {}).get("required", 0)
    if required:
        _print(f"   This video needs {required} image/diagram(s) from you.")
        _print(f"   See them with:  reelforge assets --recipe {recipe_path}")
        _print()
        _print("   next:  reelforge render --recipe "
               f"{recipe_path} \\\n"
               "             --clips ./my-clips \\\n"
               "             --assets "
               + " ".join(f"img{i}.png" for i in range(1, required + 1))
               + " --out out.mp4")
    else:
        _print("   next:  reelforge render --recipe "
               f"{recipe_path} --clips ./my-clips --out out.mp4")
    return 0


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #

def cmd_show(args: argparse.Namespace) -> int:
    data = recipe_mod.load_recipe(Path(args.recipe).expanduser())
    if args.json:
        _print(json.dumps(data, indent=2))
    else:
        _print(recipe_mod.describe(data))
    return 0


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

def _progress(index: int, total: int, segment: dict) -> None:
    source = render_mod.segment_source(segment) or "?"
    kind = "image" if segment.get("fill") == "asset" else "clip "
    _print(f"   [{index}/{total}] {segment['role']:<6} {segment['duration']:>5.2f}s"
           f"  {kind} {Path(source).name}")


def cmd_assets(args: argparse.Namespace) -> int:
    """List exactly what images/diagrams this recipe needs."""
    recipe_path = Path(args.recipe).expanduser()
    data = recipe_mod.load_recipe(recipe_path)
    project_dir = recipe_path.parent

    slots = (data.get("assets") or {}).get("slots") or []
    if not slots:
        _print("This recipe needs no images or diagrams - "
               "every segment is filled from --clips.")
        return 0

    _print(f"This recipe needs {len(slots)} image/diagram(s), in this order:")
    _print()
    for position, slot in enumerate(slots, start=1):
        _print(f"  {position}. segment {slot['segment']}  ({slot['kind']}, "
               f"confidence {slot.get('confidence', 0):.2f})")
        _print(f"     on screen  {slot['start']:.2f}s for {slot['duration']:.2f}s")
        text = slot.get("source_text") or "(no text detected)"
        _print(f"     source had {text[:70]}")
        reference = slot.get("reference_frame")
        if reference:
            full = project_dir / reference
            _print(f"     reference  {full if full.exists() else reference}")
        _print()

    _print("Supply them in this order:")
    _print(f"  reelforge render --recipe {recipe_path} \\")
    _print("      --clips ./my-footage \\")
    _print("      --assets " + " ".join(f"image{i}.png" for i in range(1, len(slots) + 1)))
    return 0


def cmd_sheet(args: argparse.Namespace) -> int:
    """Rebuild the contact sheet from a project's saved frames."""
    recipe_path = Path(args.recipe).expanduser()
    data = recipe_mod.load_recipe(recipe_path)
    project_dir = recipe_path.parent

    frame_step = float((data.get("analysis") or {}).get("frame_step") or 0.5)
    frames = sheet_mod.frames_from_project(project_dir, frame_step)

    slot_map, kind_map = ({}, {})
    if not args.plain:
        slot_map, kind_map = sheet_mod.expand_maps(data, frames)

    out_path = Path(args.out).expanduser() if args.out else (
        project_dir / "contact-sheet.jpg"
    )
    result = sheet_mod.build_contact_sheet(
        frames,
        out_path,
        columns=args.columns,
        sheet_width=args.width,
        annotate=not args.plain,
        slot_by_time=slot_map,
        kind_by_time=kind_map,
        gap=args.gap,
    )
    _print(f"-> {result}  ({len(frames)} frames, {args.columns} columns)")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    recipe_path = Path(args.recipe).expanduser()
    data = recipe_mod.load_recipe(recipe_path)

    clips = render_mod.collect_clips(args.clips) if args.clips else []
    assets = render_mod.collect_clips(args.assets) if args.assets else []
    if clips:
        _print(f"-> {len(clips)} clip(s) available")
    if assets:
        _print(f"-> {len(assets)} image/diagram(s) supplied")

    if args.music:
        data.setdefault("audio", {})["mode"] = "music"
    if args.audio_mode:
        data.setdefault("audio", {})["mode"] = args.audio_mode
    if args.no_captions:
        data.setdefault("captions", {})["enabled"] = False

    out_path = Path(args.out).expanduser()
    _print(f"-> rendering {len(data['segments'])} segments")

    result = render_mod.render(
        data,
        out_path,
        clips=clips,
        assets=assets,
        music=Path(args.music).expanduser() if args.music else None,
        fit=args.fit,
        asset_fit=args.asset_fit,
        font=args.font,
        keep_temp=args.keep_temp,
        on_progress=_progress,
    )

    if args.save_assignments:
        recipe_mod.save_recipe(data, recipe_path)
        _print(f"   clip assignments saved back to {recipe_path}")

    size_mb = result.stat().st_size / (1024 * 1024)
    _print()
    _print(f"-> {result}  ({size_mb:.1f} MB)")
    return 0


# --------------------------------------------------------------------------- #
# make (ingest + render in one shot)
# --------------------------------------------------------------------------- #

def cmd_make(args: argparse.Namespace) -> int:
    ingest_args = argparse.Namespace(
        source=args.source,
        projects=args.projects,
        cookies_from_browser=args.cookies_from_browser,
        cookies=args.cookies,
        name=args.name,
        download_timeout=args.download_timeout,
        no_transcribe=args.no_transcribe,
        no_ocr=args.no_ocr,
        no_visuals=args.no_visuals,
        whisper_model=args.whisper_model,
        scene_threshold=args.scene_threshold,
        frame_fps=args.frame_fps,
        sheet_columns=args.sheet_columns,
        blank_text=args.blank_text,
    )
    status = cmd_ingest(ingest_args)
    if status != 0:
        return status

    projects_root = Path(args.projects).expanduser()
    newest = max(projects_root.glob("*/recipe.json"), key=lambda p: p.stat().st_mtime)

    render_args = argparse.Namespace(
        recipe=str(newest),
        clips=args.clips,
        assets=args.assets,
        music=args.music,
        out=args.out,
        fit=args.fit,
        asset_fit=args.asset_fit,
        font=args.font,
        keep_temp=False,
        audio_mode=args.audio_mode,
        no_captions=args.no_captions,
        save_assignments=True,
    )
    _print()
    return cmd_render(render_args)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #

def cmd_doctor(_args: argparse.Namespace) -> int:
    checks = [
        ("ffmpeg", True, "brew install ffmpeg", "render + analyze"),
        ("ffprobe", True, "brew install ffmpeg", "media probing"),
        ("yt-dlp", False, "brew install yt-dlp", "downloading from URLs"),
        ("whisper", False, "pip install -U openai-whisper", "speech transcript"),
        ("tesseract", False, "brew install tesseract", "on-screen text (OCR)"),
    ]
    missing_required = False
    for tool, required, hint, purpose in checks:
        present = have(tool)
        tag = "ok " if present else ("MISSING" if required else "absent ")
        _print(f"  [{tag}] {tool:<10} {purpose}")
        if not present:
            _print(f"           -> {hint}")
            if required:
                missing_required = True

    font = render_mod.find_font()
    _print(f"  [{'ok ' if font else 'absent '}] {'font':<10} "
           f"{font or 'no caption font found; pass --font'}")

    backend = captions_mod.backend()
    label = {
        "pillow": "Pillow rasterizer (recommended)",
        "drawtext": "ffmpeg drawtext filter",
        "none": "unavailable - pip3 install Pillow, or use --no-captions",
    }[backend]
    _print(f"  [{'ok ' if backend != 'none' else 'MISSING'}] {'captions':<10} {label}")
    if backend == "none":
        missing_required = True

    _print()
    _print("required tools present" if not missing_required
           else "missing required tools - see hints above")
    return 1 if missing_required else 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def _add_ingest_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--projects", default=str(DEFAULT_PROJECTS),
                        help=f"where projects are stored (default: {DEFAULT_PROJECTS})")
    parser.add_argument("--name", help="project name (defaults to the post id or filename)")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="reuse a browser login for yt-dlp: chrome, firefox, safari, edge, brave")
    parser.add_argument("--cookies", metavar="FILE",
                        help="Netscape-format cookies file for yt-dlp")
    parser.add_argument("--download-timeout", type=float,
                        default=ingest_mod.DEFAULT_DOWNLOAD_TIMEOUT, metavar="SECONDS",
                        help="give up on a stalled download after this long "
                             f"(default: {ingest_mod.DEFAULT_DOWNLOAD_TIMEOUT:.0f})")
    parser.add_argument("--no-transcribe", action="store_true",
                        help="skip whisper speech-to-text")
    parser.add_argument("--no-ocr", action="store_true",
                        help="skip on-screen text extraction")
    parser.add_argument("--no-visuals", action="store_true",
                        help="skip diagram/image detection; every segment becomes footage")
    parser.add_argument("--sheet-columns", type=int, default=5,
                        help="columns in the generated contact sheet (default: 5)")
    parser.add_argument("--frame-fps", type=float, default=2.0,
                        help="frames sampled per second for OCR and visual detection "
                             "(default: 2.0)")
    parser.add_argument("--whisper-model", default="base",
                        help="whisper model size (tiny, base, small, medium; default: base)")
    parser.add_argument("--scene-threshold", type=float, default=0.28,
                        help="cut sensitivity, 0-1; lower finds more cuts (default: 0.28)")
    parser.add_argument("--blank-text", action="store_true",
                        help="leave segment 'text' empty instead of copying the source's words")


def _add_render_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--clips", nargs="+", metavar="PATH",
                        help="your footage: directories and/or individual video/image files")
    parser.add_argument("--assets", nargs="+", metavar="PATH",
                        help="the diagrams/screenshots/images the recipe asks for, "
                             "in timeline order")
    parser.add_argument("--music", metavar="FILE", help="audio track to lay under the render")
    parser.add_argument("--fit", default="cover", choices=["cover", "contain", "blur"],
                        help="how footage fills the frame (default: cover)")
    parser.add_argument("--asset-fit", default="blur", choices=["cover", "contain", "blur"],
                        help="how images/diagrams fill the frame; 'blur' and 'contain' "
                             "never crop them (default: blur)")
    parser.add_argument("--font", metavar="FILE", help="TTF/OTF font for captions")
    parser.add_argument("--audio-mode", choices=["music", "source", "silent"],
                        help="override the recipe's audio mode")
    parser.add_argument("--no-captions", action="store_true",
                        help="render without any text overlays")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reelforge",
        description="Learn a video's structure, then rebuild it with your own footage.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="analyze a video or social URL into a recipe")
    p_ingest.add_argument("source", help="local .mp4/.mov path, or an Instagram/TikTok/YouTube URL")
    _add_ingest_options(p_ingest)
    p_ingest.set_defaults(func=cmd_ingest)

    p_show = sub.add_parser("show", help="print a recipe")
    p_show.add_argument("recipe")
    p_show.add_argument("--json", action="store_true", help="dump raw JSON")
    p_show.set_defaults(func=cmd_show)

    p_render = sub.add_parser("render", help="build a new video from a recipe + your clips")
    p_render.add_argument("--recipe", required=True)
    p_render.add_argument("--out", default="out.mp4", help="output path (default: out.mp4)")
    p_render.add_argument("--keep-temp", action="store_true",
                          help="keep intermediate segment files for debugging")
    p_render.add_argument("--save-assignments", action="store_true",
                          help="write the chosen clip per segment back into the recipe")
    _add_render_options(p_render)
    p_render.set_defaults(func=cmd_render)

    p_sheet = sub.add_parser(
        "sheet", help="rebuild the contact sheet (frame grid) for a project")
    p_sheet.add_argument("--recipe", required=True)
    p_sheet.add_argument("--out", help="output image (default: <project>/contact-sheet.jpg)")
    p_sheet.add_argument("--columns", type=int, default=5, help="grid columns (default: 5)")
    p_sheet.add_argument("--width", type=int, default=1500,
                         help="total sheet width in pixels (default: 1500)")
    p_sheet.add_argument("--gap", type=int, default=0,
                         help="pixel gap between tiles (default: 0)")
    p_sheet.add_argument("--plain", action="store_true",
                         help="no timestamps or slot badges, just the frames")
    p_sheet.set_defaults(func=cmd_sheet)

    p_assets = sub.add_parser(
        "assets", help="list the images/diagrams a recipe needs, with reference stills")
    p_assets.add_argument("--recipe", required=True)
    p_assets.set_defaults(func=cmd_assets)

    p_make = sub.add_parser("make", help="ingest a source and render in one step")
    p_make.add_argument("source")
    p_make.add_argument("--out", default="out.mp4")
    _add_ingest_options(p_make)
    _add_render_options(p_make)
    p_make.set_defaults(func=cmd_make)

    p_doctor = sub.add_parser("doctor", help="check that required tools are installed")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ToolError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
