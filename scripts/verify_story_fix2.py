"""Sandbox verification for the 2026-11-05 story fix #2.

Checks (user's error report):
  1. story:cat ALWAYS shows the Iranian/foreign chooser — even when the
     Iranian archive is empty (no fall-through to Pixabay categories).
  2. The chooser's foreign branch goes to story:foreign (Pixabay, honestly
     labelled), never back to story:cat.
  3. Iranian subtopics with an empty archive answer exactly
     «هنوز کلیپ مناسبی برای این موضوع موجود نیست» and send NO clip
     (love/longing, sad/breakup, happy/hope).
  4. Pixabay _process_clip burns the Waveframe badge (apply_waveframe_badge)
     into the real output file; the old v2 pill (apply_watermark) never runs.
  5. Archive forward-mode delivery is a RAW forward_message (source header
     preserved); no download, no watermark, no re-send.
  6. Archive resend-mode (watermark_ok=1) burns the Waveframe badge and
     sends the processed file.
  7. Original audio + resolution survive the Waveframe pass.

Fake aiogram mirrors the REAL aiogram 3 layout only (aiogram.F,
aiogram.types, aiogram.filters.StateFilter, aiogram.fsm.context) — never
invented submodules (lesson 2026-11-05).

Run: python3 scripts/verify_story_fix2.py ; exit 0 = all PASS.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# No Turso in the sandbox: the archive must behave as EMPTY.
os.environ.pop("TURSO_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)
os.environ.pop("TURSO_DATABASE_URL", None)

# ---------------------------------------------------------------------------
# Fake aiogram (real aiogram 3 layout only)
# ---------------------------------------------------------------------------
class _Marker(str):
    pass


class _Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return _Marker(f"eq:{self.name}={other}")

    def __ne__(self, other):
        return _Marker(f"ne:{self.name}={other}")

    def startswith(self, prefix):
        return _Marker(f"startswith:{self.name}:{prefix}")

    def __or__(self, other):
        return _Marker(f"or({self!r}|{other!r})")

    def __repr__(self):
        return f"F.{self.name}"


class _F:
    def __getattr__(self, name):
        return _Field(name)


class _Button:
    def __init__(self, text, callback_data=None, **kw):
        self.text = text
        self.callback_data = callback_data


class _Markup:
    def __init__(self, inline_keyboard=None):
        self.inline_keyboard = inline_keyboard or []


class _FSInputFile:
    def __init__(self, path, filename=None):
        self.path = str(path)
        self.filename = filename


class _StateFilter:
    def __init__(self, *states):
        self.states = states


class _FSMContext:
    pass


class _State:
    def __init__(self):
        pass


class _StatesGroup:
    pass


def _install_fake_aiogram():
    aiogram = types.ModuleType("aiogram")
    aiogram.F = _F()
    types_mod = types.ModuleType("aiogram.types")
    types_mod.InlineKeyboardButton = _Button
    types_mod.InlineKeyboardMarkup = _Markup
    types_mod.CallbackQuery = type("CallbackQuery", (), {})
    types_mod.Message = type("Message", (), {})
    types_mod.FSInputFile = _FSInputFile
    filters_mod = types.ModuleType("aiogram.filters")
    filters_mod.StateFilter = _StateFilter
    fsm_mod = types.ModuleType("aiogram.fsm")
    fsm_ctx = types.ModuleType("aiogram.fsm.context")
    fsm_ctx.FSMContext = _FSMContext
    fsm_state = types.ModuleType("aiogram.fsm.state")
    fsm_state.State = _State
    fsm_state.StatesGroup = _StatesGroup
    sys.modules["aiogram"] = aiogram
    sys.modules["aiogram.types"] = types_mod
    sys.modules["aiogram.filters"] = filters_mod
    sys.modules["aiogram.fsm"] = fsm_mod
    sys.modules["aiogram.fsm.context"] = fsm_ctx
    sys.modules["aiogram.fsm.state"] = fsm_state


_install_fake_aiogram()

# ---------------------------------------------------------------------------
# Fakes: router / callback / bot
# ---------------------------------------------------------------------------
class FakeRouter:
    def __init__(self):
        self.handlers = []  # (filters tuple, fn)

    def _deco(self, *filters):
        def wrap(fn):
            self.handlers.append((filters, fn))
            return fn
        return wrap

    def callback_query(self, *filters):
        return self._deco(*filters)

    def message(self, *filters):
        return self._deco(*filters)

    def channel_post(self, *filters):
        return self._deco(*filters)


def find_handler(router, marker):
    for filters, fn in router.handlers:
        if any(str(f) == marker for f in filters):
            return fn
    return None


class FakeChat:
    id = 111


class FakeMessage:
    def __init__(self, bot):
        self.bot = bot
        self.chat = FakeChat()
        self.edits = []
        self.answers = []
        self.sent_videos = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text=None, reply_markup=None, parse_mode=None, **kw):
        self.answers.append((text, reply_markup))
        return FakeMessage(self.bot)

    async def answer_video(self, video, caption=None, reply_markup=None):
        self.sent_videos.append((video, caption))
        return None

    async def delete(self):
        return None


class FakeUser:
    id = 111


class FakeCallback:
    def __init__(self, data, bot):
        self.data = data
        self.bot = bot
        self.from_user = FakeUser()
        self.message = FakeMessage(bot)
        self.answered = []

    async def answer(self, text=None, show_alert=False):
        self.answered.append(text)


class MockBot:
    """Records calls; get_file/download_file raise unless overridden."""

    def __init__(self):
        self.calls = []

    async def forward_message(self, chat_id, from_chat_id, message_id):
        self.calls.append(("forward", chat_id, from_chat_id, message_id))

    async def send_video(self, chat_id, video, caption=None, **kw):
        self.calls.append(("send_video", chat_id,
                            getattr(video, "path", video), caption))

    async def get_file(self, file_id):
        raise AssertionError("get_file must not be called here")

    async def download_file(self, file_path, destination=None):
        raise AssertionError("download_file must not be called here")


class ResendBot(MockBot):
    def __init__(self, fixture, keepdir):
        super().__init__()
        self.fixture = fixture
        self.keepdir = keepdir

    async def send_video(self, chat_id, video, caption=None, **kw):
        # deliver_waveframed deletes its tempdir right after sending,
        # so keep a copy of the sent file for frame inspection.
        kept = os.path.join(self.keepdir, "resend_sent.mp4")
        shutil.copyfile(getattr(video, "path", video), kept)
        self.calls.append(("send_video", chat_id, kept, caption))

    async def get_file(self, file_id):
        class F:
            file_path = "fixture.mp4"
        return F()

    async def download_file(self, file_path, destination=None):
        shutil.copyfile(self.fixture, destination)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))


def badge_diff_count(plain_png, wm_png):
    """Pixels changed inside the badge zone vs the unbadged frame.

    The Waveframe badge is ~28% opacity, so absolute-colour thresholds
    fail on it (brightest green in the zone is only G~93 after blending).
    The fixture is a uniform colour, so ANY change in the badge zone is
    the badge itself. Zone covers the badge rect (x 0.35W..0.94W,
    y 0.76H..0.90H) with margin.
    """
    from PIL import Image
    a = Image.open(plain_png).convert("RGB")
    b = Image.open(wm_png).convert("RGB")
    W, H = b.size
    diff = 0
    for y in range(int(H * 0.74), int(H * 0.92)):
        for x in range(int(W * 0.30), W):
            pa = a.getpixel((x, y))
            pb = b.getpixel((x, y))
            if sum(abs(u - v) for u, v in zip(pa, pb)) > 18:
                diff += 1
    return diff


def extract_frame(video, at, out_png):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at),
                    "-i", video, "-frames:v", "1", out_png],
                   check=True, timeout=60)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height",
                          "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True)
    streams = [l.split(",") for l in out.stdout.strip().splitlines() if l.strip()]
    return streams


async def main():
    from app.bot import story_archive as sarch
    from app.bot import story_clips as sclips
    from app.bot import motion_watermark as mw

    # --- registration -------------------------------------------------
    r1 = FakeRouter()
    sclips.register_story_handlers(r1)
    r2 = FakeRouter()
    sarch.register_story_archive_handlers(r2)
    check("story:foreign handler registered",
          find_handler(r1, "eq:data=story:foreign") is not None)
    check("story:cat handler registered",
          find_handler(r1, "eq:data=story:cat") is not None)
    check("sarch:sub handler registered",
          find_handler(r2, "startswith:data:sarch:sub:") is not None)
    check("sarch:play handler registered",
          find_handler(r2, "startswith:data:sarch:play:") is not None)

    # --- T1: story:cat always shows the chooser (archive empty) --------
    bot = MockBot()
    cb = FakeCallback("story:cat", bot)
    await find_handler(r1, "eq:data=story:cat")(cb)
    shown = (cb.message.edits + cb.message.answers)
    check("story:cat shows chooser even with empty archive",
          any("کدام بخش" in (t or "") for t, _ in shown))
    chooser_kb = next((kb for _, kb in shown if kb is not None), None)
    btns = []
    if chooser_kb:
        for row in chooser_kb.inline_keyboard:
            btns += [b.callback_data for b in row]
    check("chooser offers Iranian + foreign branches",
          "sarch:home" in btns and "story:foreign" in btns, str(btns))
    check("chooser foreign branch does NOT loop to story:cat",
          "story:cat" not in btns, str(btns))

    # --- T2: foreign branch shows Pixabay categories ------------------
    cb2 = FakeCallback("story:foreign", bot)
    await find_handler(r1, "eq:data=story:foreign")(cb2)
    shown2 = (cb2.message.edits + cb2.message.answers)
    cat_btns = []
    for _, kb in shown2:
        if kb:
            for row in kb.inline_keyboard:
                cat_btns += [b.text for b in row]
    check("story:foreign shows Pixabay categories",
          any("عاشقانه" in t for t in cat_btns), str(cat_btns))

    # --- T3: Iranian subtopics with empty archive -> exact message ----
    for cat, sub in [("love", "longing"), ("sad", "breakup"), ("happy", "hope")]:
        b = MockBot()
        c = FakeCallback(f"sarch:sub:{cat}:{sub}:0", b)
        await find_handler(r2, "startswith:data:sarch:sub:")(c, None, None)
        texts = [t or "" for t, _ in (c.message.edits + c.message.answers)]
        check(f"empty archive {cat}/{sub} -> not-available message",
              any("هنوز کلیپ مناسبی برای این موضوع موجود نیست" in t for t in texts),
              str(texts))
        check(f"empty archive {cat}/{sub} sends no clip",
              not [cl for cl in b.calls if cl[0] in ("send_video", "forward")])

    # --- fixture video (dark, with real audio) ------------------------
    td = tempfile.mkdtemp(prefix="storyfix2_")
    fixture = os.path.join(td, "fixture.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=0x0a1420:s=720x1280:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-shortest", fixture],
                   check=True, timeout=120)
    plain_png = os.path.join(td, "plain.png")
    extract_frame(fixture, 1.5, plain_png)
    plain_png2 = os.path.join(td, "plain2.png")
    extract_frame(fixture, 1.5, plain_png2)
    check("fixture badge zone is uniform (diff baseline is zero)",
          badge_diff_count(plain_png, plain_png2) == 0)

    # --- T4: Pixabay _process_clip burns Waveframe, never v2 pill -----
    calls = {"old": 0}

    def _old_raises(*a, **k):
        calls["old"] += 1
        raise AssertionError("apply_watermark (v2 pill) must never run")

    mw.apply_watermark = _old_raises
    orig_dl = sclips._download_file

    def _fake_dl(url, dest, **kw):
        shutil.copyfile(fixture, dest)
        return dest

    sclips._download_file = _fake_dl
    clip = {"file_url": "https://example.invalid/clip.mp4"}
    out = sclips._process_clip(clip, td)
    check("_process_clip output exists and is non-empty",
          os.path.exists(out) and os.path.getsize(out) > 0)
    streams = probe(out)
    check("Waveframe output keeps 720x1280 resolution",
          any(s[1] == "video" and s[2] == "720" and s[3] == "1280" for s in streams),
          str(streams))
    check("Waveframe output preserves original audio (aac)",
          any(s[1] == "audio" for s in streams), str(streams))
    wm_png = os.path.join(td, "wm.png")
    extract_frame(out, 1.5, wm_png)
    n_diff = badge_diff_count(plain_png, wm_png)
    check("Waveframe badge REALLY rendered in output frame",
          n_diff > 500, f"changed px in badge zone={n_diff}")
    check("old v2 pill never called during _process_clip", calls["old"] == 0)
    sclips._download_file = orig_dl

    # --- T5: forward-mode = raw forward, source preserved --------------
    fwd_clip = {"id": 1, "file_id": "AAA", "file_unique_id": "uniq1",
                "source_chat_id": -100123, "source_message_id": 456,
                "source_username": "chan", "mode": "forward",
                "category": "love", "subcategory": "longing",
                "status": "active", "watermark_ok": 0}
    sarch.get_clip = lambda clip_id: fwd_clip
    b5 = MockBot()
    c5 = FakeCallback("sarch:play:1:love:longing:0", b5)
    await find_handler(r2, "startswith:data:sarch:play:")(c5, None, None)
    check("forward-mode calls forward_message once with source ids",
          ("forward", 111, -100123, 456) in b5.calls, str(b5.calls))
    check("forward-mode never re-sends / downloads",
          not [cl for cl in b5.calls if cl[0] == "send_video"])

    # --- T6: resend wm=1 = Waveframe file actually sent ----------------
    res_clip = dict(fwd_clip, mode="resend", watermark_ok=1)
    sarch.get_clip = lambda clip_id: res_clip
    b6 = ResendBot(fixture, td)
    c6 = FakeCallback("sarch:play:1:love:longing:0", b6)
    await find_handler(r2, "startswith:data:sarch:play:")(c6, None, None)
    sent = [cl for cl in b6.calls if cl[0] == "send_video"]
    check("resend wm=1 sends a processed video file", len(sent) == 1, str(b6.calls))
    if sent:
        sent_path = sent[0][2]
        check("resend caption carries @AvanoMediaBot",
              "@AvanoMediaBot" in (sent[0][3] or ""))
        rp = os.path.join(td, "resend.png")
        extract_frame(sent_path, 1.5, rp)
        n2 = badge_diff_count(plain_png, rp)
        check("resend file really has the Waveframe badge",
              n2 > 500, f"changed px in badge zone={n2}")

    # --- T8: every archive keyboard builder runs (no _btn TypeError) --
    # Lesson 2026-11-04: a self-test must build EVERY keyboard at least
    # once. The old def _btn(text, cb) rejected callback_data= keywords,
    # which ~40 archive call sites use — every archive keyboard crashed.
    import types as _t
    owner_cfg = _t.SimpleNamespace(owner_id=111)
    for data, marker in [("sarch:home", "eq:data=sarch:home"),
                         ("sarch:cat:love", "startswith:data:sarch:cat:"),
                         ("sarch:panel", "eq:data=sarch:panel"),
                         ("sarch:queue", "eq:data=sarch:queue")]:
        c = FakeCallback(data, MockBot())
        try:
            await find_handler(r2, marker)(c, owner_cfg, None)
            built = bool(c.message.edits or c.message.answers)
            check(f"keyboard builds: {data}", built)
        except TypeError as e:
            check(f"keyboard builds: {data}", False, f"TypeError: {e}")

    # --- T7: old watermark unreachable from the Pixabay path -----------
    import inspect
    src = inspect.getsource(sclips._process_clip)
    check("_process_clip calls apply_waveframe_badge",
          "apply_waveframe_badge" in src)
    check("_process_clip never calls apply_watermark",
          "apply_watermark(" not in src)
    check("no apply_watermark( call left in story_clips.py",
          "apply_watermark(" not in Path(sclips.__file__).read_text())

    # --- existing self-test still green --------------------------------
    check("story_clips.run_self_test() still passes",
          sclips.run_self_test() is True)

    shutil.rmtree(td, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
