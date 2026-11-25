import json
import hashlib
import math
import random
import re
import subprocess
from uuid import uuid4
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from render_grid import crumble, run

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "ffglitch-0.10.2-macos-aarch64" / "ffedit"
EFFECTS = ROOT / "effects.js"
RENDER_LOCK = Lock()
SESSION = TemporaryDirectory(prefix="ffglitch-pads-")
UPLOADS = Path(SESSION.name)

PAD_PARAMS = {
    "echo": {"history_frames": (1, 24), "blend_percent": (0, 100)},
    "pan": {"x": (-16, 16), "y": (-16, 16)},
    "wave-bursts": {"chance": (0, 100), "duration": (1, 24), "seed": (0, 999999)},
    "crumble": {"cutoff": (1, 64)},
    "swap": {},
    "split-phase": {"phase_frames": (1, 24)},
    "circular": {},
}


def parse_recipe(recipe):
    if not isinstance(recipe, dict) or type(recipe.get("version")) is not int or recipe["version"] != 1:
        raise ValueError("Unsupported recipe version")
    pads = recipe.get("pads")
    if not isinstance(pads, list) or len(pads) > 4:
        raise ValueError("Expected up to four pads")
    effects, slots = {}, set()
    for pad in pads:
        if not isinstance(pad, dict) or set(pad) != {"slot", "effect", "params", "active_ranges_ms"}:
            raise ValueError("Invalid pad")
        slot, effect, params = pad["slot"], pad["effect"], pad["params"]
        if type(slot) is not int or not 1 <= slot <= 4 or slot in slots:
            raise ValueError("Invalid or repeated slot")
        if not isinstance(effect, str) or effect not in PAD_PARAMS or effect in effects:
            raise ValueError("Unknown or repeated effect")
        rules = PAD_PARAMS[effect]
        if not isinstance(params, dict) or set(params) != set(rules):
            raise ValueError("Invalid parameters")
        values = []
        for name, (low, high) in rules.items():
            value = params[name]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid parameter: {name}")
            values.append(value)
        ranges = pad["active_ranges_ms"]
        if not isinstance(ranges, list) or len(ranges) > 8 or any(
            not isinstance(span, list) or len(span) != 2 or
            any(type(value) is not int for value in span) or not 0 <= span[0] < span[1] <= 4000
            for span in ranges
        ):
            raise ValueError("Invalid ranges")
        ranges = sorted(ranges)
        if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
            raise ValueError("Overlapping ranges")
        slots.add(slot)
        effects[effect] = [*values, ranges]
    # Slots address controls, not the order of processing.
    h, p, spans = effects.get("echo", [1, 0, []])
    return (h, p, spans, effects.get("pan", [0, 0, []]),
            effects.get("crumble", [64, []]), effects.get("wave-bursts", [0, 5, 1, []]),
            {name: effects[name] for name in ("swap", "split-phase", "circular")
             if name in effects and effects[name][-1]})


def render(media, history, percent, spans, pan, texture, wave, extra):
    cache = ["mix-v3", spans, pan, texture, wave]
    if extra:
        cache.append(extra)
    key = hashlib.sha256(json.dumps(cache).encode()).hexdigest()[:16]
    name = f"echo-{history}-{percent}-{key}.mp4"

    target = media / name
    with RENDER_LOCK:
        if not target.exists():
            with TemporaryDirectory(dir=media) as folder:
                tmp = Path(folder)
                use_texture = texture[0] < 64 and bool(texture[1])
                features = ["-f", "mv"] + (["-f", "q_dct"] if use_texture else [])
                source = (media / "source.json").read_text()
                data = json.loads((media / "texture-source.json").read_text() if use_texture else source)
                frames = data["streams"][0]["frames"]
                originals = [f.get("mv", {}).get("forward") for f in
                             json.loads(source)["streams"][0]["frames"]]
                rng = random.Random(wave[2])
                remaining = 0
                for i in range(len(frames)):
                    def applies(ranges):
                        return any(a * 24 <= i * 1000 < b * 24 for a, b in ranges)
                    swap = "swap" in extra and applies(extra["swap"][-1])
                    circular = "circular" in extra and applies(extra["circular"][-1])
                    phase_axis = None
                    if "split-phase" in extra:
                        count, ranges = extra["split-phase"]
                        for a, b in ranges:
                            if a * 24 <= i * 1000 < b * 24:
                                phase_axis = ((i - (a * 24 + 999) // 1000) // count) % 2
                                break
                    if use_texture and applies(texture[1]):
                        crumble(frames[i], texture[0])
                    echo = percent / 100 if applies(spans) else 0
                    shift = pan[:2] if applies(pan[2]) else [0, 0]
                    roll = rng.randrange(100)
                    if not applies(wave[3]):
                        remaining = 0
                    elif remaining == 0 and roll < wave[0]:
                        remaining = wave[1]
                    bursting = remaining > 0
                    remaining = max(0, remaining - 1)
                    if not originals[i] or not (echo or any(shift) or bursting or swap or circular or phase_axis is not None):
                        continue
                    frames[i]["mv"]["overflow"] = "truncate"
                    window = [m for m in originals[max(0, i-history+1):i+1] if m]
                    for y, row in enumerate(frames[i]["mv"]["forward"]):
                        for x, vector in enumerate(row):
                            if vector is None:
                                continue
                            clean = tuple(vector)
                            values = [m[y][x] for m in window if m[y][x] is not None]
                            for axis in (0, 1):
                                avg = sum(v[axis] for v in values) / len(values)
                                pan_weight = int(shift[axis] != 0)
                                delta = echo * (avg - vector[axis]) + shift[axis]
                                if bursting:
                                    factor = (math.sin(math.tau * y / len(originals[i])) if axis == 0
                                              else math.cos(math.tau * x / len(row)))
                                    delta += vector[axis] * (factor - 1)
                                if swap:
                                    delta += clean[1-axis] - clean[axis]
                                if circular:
                                    delta += clean[axis] * ((math.sin(x) if axis == 0 else math.cos(y)) - 1)
                                if phase_axis == axis:
                                    delta -= clean[axis]
                                weight = echo + pan_weight + bursting + swap + circular + (phase_axis == axis)
                                vector[axis] = round(clean[axis] + delta / max(1, weight))
                (tmp / "recipe.json").write_text(json.dumps(data, separators=(",", ":")))
                run(str(ENGINE), "-i", str(media / "work.avi"), *features,
                    "-a", str(tmp / "recipe.json"), "-o", str(tmp / "result.avi"))
                run("ffmpeg", "-v", "error", "-i", str(tmp / "result.avi"), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-g", "6", str(tmp / name))
                (tmp / name).replace(target)
    return name


def standalone_script(recipe):
    template = EFFECTS.read_text()
    placeholder = "const embeddedRecipe = { version: 1, fps: 24, pads: [] };"
    if template.count(placeholder) != 1:
        raise RuntimeError("Missing embedded recipe placeholder")
    encoded = json.dumps(recipe, ensure_ascii=True, indent=2)
    return template.replace(placeholder, f"const embeddedRecipe = {encoded};", 1)


def render_script(media, script):
    key = hashlib.sha256(script.encode()).hexdigest()[:16]
    name = f"js-{key}.mp4"
    target = media / name
    with RENDER_LOCK:
        if not target.exists():
            with TemporaryDirectory(dir=media) as directory:
                tmp = Path(directory)
                entry = tmp / "recipe.js"
                entry.write_text(script)
                run(str(ENGINE), "-i", str(media / "work.avi"), "-s", str(entry),
                    "-o", str(tmp / "result.avi"))
                run("ffmpeg", "-v", "error", "-xerror", "-i", str(tmp / "result.avi"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "6", str(tmp / name))
                (tmp / name).replace(target)
    return name


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args):
        super().__init__(*args, directory=ROOT)

    def do_GET(self):
        if self.headers.get("Host") != "127.0.0.1:8765":
            self.send_error(403)
            return
        match = re.fullmatch(r"/clips/([a-f0-9]{32})/((?:1|js-[a-f0-9]{16})\.mp4|work\.avi)", self.path)
        if match:
            self.directory = UPLOADS
            self.path = f"/{match[1]}/{match[2]}"
            super().do_GET()
            return
        if self.path != "/portrait.html":
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):
        self.send_error(405)

    def do_POST(self):
        if self.headers.get("Host") != "127.0.0.1:8765" or self.headers.get("Origin") != "http://127.0.0.1:8765":
            self.send_error(403)
            return
        if self.path == "/open-video" and self.headers.get_content_type() == "application/octet-stream":
            self.open_video()
            return
        if self.path != "/render" or self.headers.get_content_type() != "application/json":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
            clip = data.get("clip")
            if isinstance(clip, str) and re.fullmatch(r"[a-f0-9]{32}", clip) and (UPLOADS / clip / "1.mp4").is_file():
                media, prefix = UPLOADS / clip, f"clips/{clip}"
            else:
                raise ValueError()
            recipe = data["recipe"]
            if not isinstance(recipe, dict) or type(recipe.get("fps")) is not int or recipe["fps"] != 24:
                raise ValueError("Expected a 24 fps recipe")
            parsed = parse_recipe(recipe)
            duration_ms = int((media / "duration-ms").read_text())
            _, _, echo, pan, texture, wave, extra = parsed
            tracks = [echo, pan[-1], texture[-1], wave[-1], *(v[-1] for v in extra.values())]
            if any(end > duration_ms for track in tracks for _, end in track):
                raise ValueError("Range ends after video")
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        try:
            script = standalone_script(recipe)
            name = render_script(media, script)
        except Exception:
            self.send_error(500, "Render failed")
            raise
        self.send_json({"path": f"{prefix}/{name}", "script": script, "input": f"{prefix}/work.avi"})

    def open_video(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            start = int(self.headers.get("X-Start-Ms", "0"))
            if not 0 <= start <= 86_400_000 or not 0 < size <= 256 * 1024**2:
                raise ValueError()
        except ValueError:
            self.send_error(400)
            return
        clip = uuid4().hex
        folder = UPLOADS / clip
        folder.mkdir()
        with TemporaryDirectory(dir=UPLOADS) as incoming:
            source = Path(incoming) / "input"
            with source.open("wb") as file:
                while size:
                    chunk = self.rfile.read(min(size, 1024**2))
                    if not chunk:
                        self.send_error(400)
                        return
                    file.write(chunk)
                    size -= len(chunk)
            try:
                work = str(folder / "work.avi")
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(start / 1000),
                    "-protocol_whitelist", "file", "-format_whitelist", "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpeg,mpegts",
                    "-i", str(source), "-map", "0:v:0", "-an",
                    "-vf", "scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=24",
                    "-frames:v", "96", "-c:v", "mpeg2video", "-bf", "0", "-g", "48", "-q:v", "3", work],
                    check=True, timeout=60)
                info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,nb_frames", "-of", "json", work], timeout=10))["streams"][0]
                frames = int(info["nb_frames"])
                if not 1 <= frames <= 96:
                    raise ValueError("Start is outside the video")
                duration_ms = (frames * 1000 + 12) // 24
                run("ffmpeg", "-v", "error", "-i", work, "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-g", "6", str(folder / "1.mp4"))
                (folder / "duration-ms").write_text(str(duration_ms))
            except (ValueError, IndexError, KeyError, subprocess.SubprocessError):
                self.send_error(422, "Could not prepare video")
                return
        self.send_json({"clip": clip, "path": f"clips/{clip}/1.mp4", "duration_ms": duration_ms,
                        "orientation": "landscape" if info["width"] >= info["height"] else "portrait"})

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
