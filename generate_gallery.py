#!/usr/bin/env python3
"""
Static wallpaper gallery generator.

* Thumbnails (480px WebP) are built into ./site/thumbs, NEVER into the repo
  tree (add `site/` to .gitignore if you run this locally).
* Incremental: every thumbnail is named after the *content hash* of its source
  (for Git LFS files that is the oid in the pointer file, so unchanged images
  are not even downloaded). Existing valid thumbnails are reused; only new or
  changed images are processed.
* Strict: every thumbnail is written atomically, re-opened and decoded to
  verify it, failed ones are retried, and the script exits non-zero if any
  image still has no valid thumbnail (so CI never deploys a broken gallery).
* The page lazy-loads thumbnails through a throttled queue. The thumbnail is
  drawn as a CSS background of a link to the real image, so right click ->
  "Save link as" downloads the original from media.githubusercontent.com, and
  the lightbox shows the original too.

Storage backends for thumbnails:
  * local (default): ./site/thumbs, shipped inside the Pages artifact.
  * Cloudflare R2 (or any S3-compatible bucket): set R2_BUCKET and friends
    (see below). The bucket is the persistent cache: only thumbnails missing
    from it are built and uploaded, every upload is size-checked, and the
    Pages site shrinks to a single index.html.

    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
    R2_PUBLIC_URL  (e.g. https://pub-xxxx.r2.dev or https://thumbs.example.com)
    R2_ENDPOINT    (optional override, for other S3-compatible services)

Usage:
    pip install pillow boto3      # boto3 only for R2; ffmpeg only for the .mp4 frame
    python generate_gallery.py    # -> ./site
    python generate_gallery.py --prune   # R2: also delete thumbnails no longer used
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
SECTIONS = [("static", "static_wallpapers"), ("live", "live_wallpapers")]
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VID_EXT = {".mp4", ".webm", ".mov"}
THUMB_W = 480
THUMB_Q = 78
REPO = "NaiveInvestigator/wallpapers"
LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"


def media_url(rel: str, branch: str) -> str:
    return f"https://media.githubusercontent.com/media/{REPO}/refs/heads/{quote(branch)}/{quote(rel)}"


def content_id(path: Path):
    """(id, is_lfs_pointer). LFS pointer -> oid from the pointer; else sha256 of the file."""
    with open(path, "rb") as f:
        head = f.read(512)
        if head.startswith(LFS_MAGIC):
            m = re.search(rb"oid sha256:([0-9a-f]{64})", head)
            if m:
                return m.group(1).decode()[:20], True
        h = hashlib.sha256(head)
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:20], False


def download(url: str, dest: Path, tries: int = 4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wallpaper-gallery"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            if dest.stat().st_size == 0:
                raise IOError("empty download")
            return
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


_s3 = None


def r2_client():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        endpoint = os.environ.get("R2_ENDPOINT") or f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        _s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name="auto",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=Config(retries={"max_attempts": 6, "mode": "standard"}),
        )
    return _s3


def r2_list() -> dict:
    """{thumbnail_filename: size} for everything under thumbs/ in the bucket."""
    found = {}
    pages = r2_client().get_paginator("list_objects_v2").paginate(Bucket=os.environ["R2_BUCKET"], Prefix="thumbs/")
    for page in pages:
        for o in page.get("Contents", []):
            found[o["Key"][len("thumbs/"):]] = o["Size"]
    return found


def r2_put(name: str, path: Path):
    bucket, key = os.environ["R2_BUCKET"], f"thumbs/{name}"
    r2_client().upload_file(str(path), bucket, key, ExtraArgs={
        "ContentType": "image/webp",
        "CacheControl": "public, max-age=31536000, immutable"})
    head = r2_client().head_object(Bucket=bucket, Key=key)  # confirm it really landed
    if head["ContentLength"] != path.stat().st_size:
        raise IOError(f"upload size mismatch for {name}")


def thumb_url(name: str, use_r2: bool) -> str:
    if use_r2:
        return os.environ["R2_PUBLIC_URL"].rstrip("/") + "/thumbs/" + name
    return "thumbs/" + name


def make_thumb(job):
    """Returns (prefix, thumb_filename | None, status)."""
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None
    prefix, src, url, is_pointer, is_video, tdir, existing, use_r2 = job
    tdir = Path(tdir)

    if existing and use_r2:  # already in the bucket (uploads are size-verified)
        return prefix, existing, "cached"
    if existing:  # local: reuse only if it really decodes
        try:
            with Image.open(tdir / existing) as t:
                t.load()
            return prefix, existing, "cached"
        except Exception:  # noqa: BLE001
            (tdir / existing).unlink(missing_ok=True)

    work = Path(tempfile.mkdtemp())
    try:
        local = Path(src)
        if is_pointer:  # LFS pointer in checkout -> fetch just this one original
            local = work / ("src" + local.suffix)
            download(url, local)
        open_path = local
        if is_video:
            open_path = work / "frame.png"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(local),
                            "-frames:v", "1", str(open_path)], check=True, timeout=180)
        with Image.open(open_path) as im:
            ow, oh = im.size
            name = f"{prefix}-{ow}x{oh}.webp"
            im.draft("RGB", (THUMB_W * 2, THUMB_W * 2))
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGBA" if im.mode in ("P", "LA", "PA", "RGBA") else "RGB")
            im.thumbnail((THUMB_W, THUMB_W * 4), Image.LANCZOS)
            part = (work if use_r2 else tdir) / (name + ".part")
            im.save(part, "WEBP", quality=THUMB_Q, method=4)
        with Image.open(part) as chk:  # verify what we wrote
            chk.load()
        if use_r2:
            r2_put(name, part)
        else:
            os.replace(part, tdir / name)  # atomic: no half-written files in the cache
        return prefix, name, "new"
    except Exception as e:  # noqa: BLE001
        return prefix, None, f"failed: {e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def collect(branch: str):
    entries = []
    for section, folder in SECTIONS:
        d = ROOT / folder
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            ext = p.suffix.lower()
            if not p.is_file() or (ext not in IMG_EXT and ext not in VID_EXT):
                continue
            rel = f"{folder}/{p.name}"
            cid, is_ptr = content_id(p)
            entries.append({"rel": rel, "path": p, "s": section, "v": ext in VID_EXT,
                            "u": media_url(rel, branch), "ptr": is_ptr,
                            "prefix": f"{cid}w{THUMB_W}"})
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--branch", default=os.environ.get("GITHUB_REF_NAME", "main"))
    ap.add_argument("--prune", action="store_true", help="R2: delete thumbnails no longer referenced")
    ap.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 2) * 2))
    args = ap.parse_args()

    out = ROOT / args.out
    tdir = out / "thumbs"
    out.mkdir(parents=True, exist_ok=True)
    use_r2 = bool(os.environ.get("R2_BUCKET"))
    if use_r2:
        missing = [k for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_PUBLIC_URL") if not os.environ.get(k)]
        if missing or not (os.environ.get("R2_ACCOUNT_ID") or os.environ.get("R2_ENDPOINT")):
            sys.exit(f"R2_BUCKET is set but missing env: {missing or ['R2_ACCOUNT_ID']}")
        print(f"Thumbnail storage: R2 bucket '{os.environ['R2_BUCKET']}'")
    else:
        tdir.mkdir(parents=True, exist_ok=True)
        print("Thumbnail storage: local ./site/thumbs")

    entries = collect(args.branch)
    print(f"Found {len(entries)} files")

    if use_r2:
        cached = {n.split("-")[0]: n for n, size in r2_list().items() if size > 0}
    else:
        for f in tdir.glob("*.part"):  # leftovers from an interrupted run
            f.unlink()
        cached = {f.name.split("-")[0]: f.name for f in tdir.glob("*.webp")}

    uniq = {}  # identical content -> one thumbnail (also avoids write races)
    for e in entries:
        uniq.setdefault(e["prefix"], e)
    jobs = [(pfx, str(e["path"]), e["u"], e["ptr"], e["v"], str(tdir), cached.get(pfx), use_r2)
            for pfx, e in uniq.items()]
    print(f"{sum(1 for j in jobs if j[6])} cached candidates, {sum(1 for j in jobs if not j[6])} to build")

    results = {}
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, r in enumerate(ex.map(make_thumb, jobs, chunksize=4), 1):
            results[r[0]] = r
            if i % 100 == 0 or i == len(jobs):
                print(f"  processed {i}/{len(jobs)}")

    # retry failures once, serially
    failed = [j for j in jobs if results[j[0]][1] is None]
    for j in failed:
        print(f"  retrying {Path(j[1]).name}: {results[j[0]][2]}")
        results[j[0]] = make_thumb(j)

    still = [(e["rel"], results[e["prefix"]][2]) for e in entries if results[e["prefix"]][1] is None]
    # final verification: every referenced thumbnail exists and is non-empty
    remote = r2_list() if use_r2 else None
    for e in entries:
        name = results[e["prefix"]][1]
        if not name:
            continue
        if use_r2:
            ok = remote.get(name, 0) > 0
        else:
            ok = (tdir / name).is_file() and (tdir / name).stat().st_size > 0
        if not ok:
            still.append((e["rel"], "thumbnail missing/empty in storage"))
    if still:
        print("\nTHUMBNAIL FAILURES:", file=sys.stderr)
        for rel, why in still:
            print(f"  - {rel}: {why}", file=sys.stderr)
        sys.exit(1)

    stats = {"new": 0, "cached": 0}
    for r in results.values():
        stats[r[2]] += 1
    print(f"Thumbnails OK: {stats['new']} new, {stats['cached']} reused")

    keep = {r[1] for r in results.values()}
    if use_r2:
        stale = [n for n in remote if n not in keep]
        if args.prune and stale:
            for i in range(0, len(stale), 1000):
                r2_client().delete_objects(Bucket=os.environ["R2_BUCKET"], Delete={
                    "Objects": [{"Key": f"thumbs/{n}"} for n in stale[i:i + 1000]]})
            print(f"Pruned {len(stale)} unused thumbnails from the bucket")
        elif stale:
            print(f"{len(stale)} unused thumbnails left in bucket (use --prune to delete)")
    else:
        for f in tdir.iterdir():  # prune thumbnails of deleted/changed images
            if f.name not in keep:
                f.unlink()

    data = []
    for e in entries:
        name = results[e["prefix"]][1]
        w, h = map(int, name[:-5].split("-")[1].split("x"))
        data.append({"n": e["path"].name, "s": e["s"], "v": int(e["v"]), "u": e["u"],
                     "t": thumb_url(name, use_r2), "w": w, "h": h})

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    (out / "index.html").write_text(TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    print(f"Done -> {out / 'index.html'}  ({len(data)} items)")


TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wallpapers</title>
<style>
:root{color-scheme:light dark;--bg:#fafafa;--fg:#111;--card:#e6e6ea;--muted:#666;--accent:#7c5cff}
@media(prefers-color-scheme:dark){:root{--bg:#0e0e11;--fg:#eee;--card:#1b1b20;--muted:#9a9aa3}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;
  padding:.7rem 1rem;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px);
  border-bottom:1px solid color-mix(in srgb,var(--fg) 10%,transparent)}
h1{font-size:1.05rem;margin:0 .5rem 0 0}
button,input{font:inherit;color:inherit}
.tab{padding:.3rem .8rem;border-radius:99px;border:1px solid color-mix(in srgb,var(--fg) 18%,transparent);background:none;cursor:pointer}
.tab[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
input{flex:1;min-width:140px;max-width:320px;padding:.35rem .8rem;border-radius:99px;
  border:1px solid color-mix(in srgb,var(--fg) 18%,transparent);background:var(--card)}
#count{color:var(--muted);margin-left:auto;font-size:.85rem}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;padding:12px}
.card{position:relative;display:block;aspect-ratio:16/9;overflow:hidden;border-radius:8px;background:var(--card);
  content-visibility:auto;contain-intrinsic-size:auto 135px}
.card:not(.done)::before{content:"";position:absolute;inset:0;
  background:linear-gradient(100deg,transparent 30%,color-mix(in srgb,var(--fg) 8%,transparent) 50%,transparent 70%);
  background-size:200% 100%;animation:sh 1.4s linear infinite}
@keyframes sh{from{background-position:200% 0}to{background-position:-200% 0}}
/* thumbnail is a background (not an <img>) so the browser offers "Save link as" = the original, not the thumb */
.pic{position:absolute;inset:0;background-size:cover;background-position:center;opacity:0;transition:opacity .3s,transform .3s}
.pic.ok{opacity:1}
.card:hover .pic{transform:scale(1.04)}
.card .cap{position:absolute;left:0;right:0;bottom:0;padding:1.4rem .6rem .4rem;font-size:.75rem;color:#fff;
  background:linear-gradient(transparent,rgba(0,0,0,.7));opacity:0;transition:opacity .2s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card:hover .cap,.card:focus-visible .cap{opacity:1}
.badge{position:absolute;top:6px;right:6px;padding:1px 6px;border-radius:4px;font-size:.7rem;background:rgba(0,0,0,.6);color:#fff}
.card.err::after{content:"failed to load";position:absolute;inset:0;display:grid;place-items:center;color:var(--muted);font-size:.85rem}
#sentinel{height:1px}
#lb{position:fixed;inset:0;z-index:20;display:none;background:rgba(0,0,0,.92);align-items:center;justify-content:center}
#lb.open{display:flex}
#lb img,#lb video{max-width:96vw;max-height:88vh;object-fit:contain}
#lb .bar{position:absolute;left:0;right:0;bottom:0;display:flex;gap:1rem;justify-content:center;align-items:center;
  padding:.6rem;color:#ddd;font-size:.85rem;background:rgba(0,0,0,.5)}
#lb a{color:#bda9ff}
#lb .nav{position:absolute;top:0;bottom:0;width:15%;background:none;border:0;color:#fff;font-size:2.5rem;cursor:pointer;opacity:.5}
#lb .nav:hover{opacity:1}
#prev{left:0}#next{right:0}
#close{position:absolute;top:8px;right:14px;background:none;border:0;color:#fff;font-size:2rem;cursor:pointer;z-index:2}
</style>
</head>
<body>
<header>
  <h1>Wallpapers</h1>
  <button class="tab" data-tab="all" aria-pressed="true">All</button>
  <button class="tab" data-tab="static" aria-pressed="false">Static</button>
  <button class="tab" data-tab="live" aria-pressed="false">Live</button>
  <input id="q" type="search" placeholder="Search…" autocomplete="off">
  <span id="count"></span>
</header>
<main><div id="grid"></div><div id="sentinel"></div></main>

<div id="lb" role="dialog" aria-modal="true">
  <button id="close" aria-label="Close">×</button>
  <button class="nav" id="prev" aria-label="Previous">‹</button>
  <div id="stage"></div>
  <button class="nav" id="next" aria-label="Next">›</button>
  <div class="bar"><span id="lbname"></span><a id="lbopen" target="_blank" rel="noopener">open original</a></div>
</div>

<script>
const DATA = __DATA__;
const CFG = { batch: 96, maxParallel: 6, retries: 3, margin: 800 };
const $ = s => document.querySelector(s);
const grid = $('#grid'), sentinel = $('#sentinel');
let view = [], shown = 0, tab = 'all', query = '';

/* ---------- throttled thumbnail loader ---------- */
const queue = [];
let active = 0;
const picObs = new IntersectionObserver(entries => {
  for (const e of entries) if (e.isIntersecting) { picObs.unobserve(e.target); queue.push(e.target); }
  pump();
}, { rootMargin: CFG.margin + 'px 0px' });

function pump() {
  while (active < CFG.maxParallel && queue.length) {
    const el = queue.shift();
    if (!el.isConnected) continue;                         // filtered away
    const r = el.getBoundingClientRect();
    if (r.bottom < -1500 || r.top > innerHeight + 1500) { // scrolled far past: re-arm instead of fetching
      picObs.observe(el); continue;
    }
    load(el);
  }
}
function load(el) {
  active++;
  let tries = 0;
  const done = () => { active--; pump(); };
  const attempt = () => {
    const p = new Image();
    p.decoding = 'async';
    p.onload = () => {
      el.style.backgroundImage = 'url("' + p.src + '")';
      el.classList.add('ok'); el.parentElement.classList.add('done'); done();
    };
    p.onerror = () => {
      if (++tries <= CFG.retries) setTimeout(attempt, 500 * 2 ** tries);
      else { el.parentElement.classList.add('err', 'done'); done(); }
    };
    p.src = tries ? el.dataset.src + '?r=' + tries : el.dataset.src;
  };
  attempt();
}

/* ---------- cards, rendered in batches ---------- */
function card(d, i) {
  const a = document.createElement('a');
  a.className = 'card'; a.href = d.u; a.dataset.i = i; a.title = d.n;
  a.setAttribute('aria-label', d.n);
  const pic = document.createElement('div');
  pic.className = 'pic'; pic.dataset.src = d.t;
  a.append(pic); picObs.observe(pic);
  const cap = document.createElement('span'); cap.className = 'cap'; cap.textContent = d.n; a.append(cap);
  if (d.v) { const b = document.createElement('span'); b.className = 'badge'; b.textContent = 'video'; a.append(b); }
  return a;
}
function renderMore() {
  const end = Math.min(shown + CFG.batch, view.length), frag = document.createDocumentFragment();
  for (let i = shown; i < end; i++) frag.append(card(view[i], i));
  grid.append(frag); shown = end;
  sentinelObs.unobserve(sentinel); sentinelObs.observe(sentinel); // re-check if still visible
}
const sentinelObs = new IntersectionObserver(es => {
  if (es[0].isIntersecting && shown < view.length) renderMore();
}, { rootMargin: '1200px 0px' });
sentinelObs.observe(sentinel);

function refresh() {
  const q = query.trim().toLowerCase();
  view = DATA.filter(d => (tab === 'all' || d.s === tab) && (!q || d.n.toLowerCase().includes(q)));
  queue.length = 0; grid.replaceChildren(); shown = 0;
  $('#count').textContent = view.length + ' wallpapers';
  scrollTo(0, 0); renderMore();
}
document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  tab = b.dataset.tab;
  document.querySelectorAll('.tab').forEach(x => x.setAttribute('aria-pressed', x === b));
  refresh();
});
let t; $('#q').oninput = e => { clearTimeout(t); t = setTimeout(() => { query = e.target.value; refresh(); }, 200); };

/* ---------- lightbox (always the original from media.githubusercontent.com) ---------- */
const lb = $('#lb'), stage = $('#stage'); let cur = -1;
function show(i) {
  if (i < 0 || i >= view.length) return;
  cur = i; const d = view[i]; stage.replaceChildren();
  let el;
  if (d.v) { el = document.createElement('video'); el.controls = el.autoplay = el.loop = true; el.src = d.u; }
  else { el = document.createElement('img'); el.alt = d.n; el.src = d.u; }
  stage.append(el);
  $('#lbname').textContent = d.n + (d.w ? `  ·  ${d.w}×${d.h}` : '') + `  ·  ${i + 1}/${view.length}`;
  $('#lbopen').href = d.u; lb.classList.add('open');
}
function hide() { lb.classList.remove('open'); stage.replaceChildren(); cur = -1; }
grid.addEventListener('click', e => {
  const a = e.target.closest('.card'); if (!a) return;
  e.preventDefault(); show(+a.dataset.i);
});
$('#close').onclick = hide; $('#prev').onclick = () => show(cur - 1); $('#next').onclick = () => show(cur + 1);
lb.onclick = e => { if (e.target === lb || e.target === stage) hide(); };
addEventListener('keydown', e => {
  if (cur < 0) return;
  if (e.key === 'Escape') hide();
  else if (e.key === 'ArrowLeft') show(cur - 1);
  else if (e.key === 'ArrowRight') show(cur + 1);
});
refresh();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()
