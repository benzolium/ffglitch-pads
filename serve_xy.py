import json
import hashlib
import math
import random
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from render_grid import crumble, run

ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / "portrait"
ENGINE = ROOT / "ffglitch-0.10.2-macos-aarch64" / "ffedit"
SOURCE = (MEDIA / "source.json").read_text()
TEXTURE_SOURCE = MEDIA / "texture-source.json"
RENDER_LOCK = Lock()


def render(history, percent, spans, pan, texture, wave):
    key = hashlib.sha256(json.dumps(["mix-v3", spans, pan, texture, wave]).encode()).hexdigest()[:16]
    name = f"echo-{history}-{percent}-{key}.mp4"

    target = MEDIA / name
    with RENDER_LOCK:
        if not target.exists():
            with TemporaryDirectory(dir=MEDIA) as folder:
                tmp = Path(folder)
                use_texture = texture[0] < 64 and bool(texture[1])
                features = ["-f", "mv"] + (["-f", "q_dct"] if use_texture else [])
                data = json.loads(TEXTURE_SOURCE.read_text() if use_texture else SOURCE)
                frames = data["streams"][0]["frames"]
                originals = [f.get("mv", {}).get("forward") for f in
                             json.loads(SOURCE)["streams"][0]["frames"]]
                rng = random.Random(wave[2])
                remaining = 0
                for i in range(len(frames)):
                    def applies(ranges):
                        return any(a * 24 <= i * 1000 < b * 24 for a, b in ranges)
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
                    if not originals[i] or not (echo or any(shift) or bursting):
                        continue
                    frames[i]["mv"]["overflow"] = "truncate"
                    window = [m for m in originals[max(0, i-history+1):i+1] if m]
                    for y, row in enumerate(frames[i]["mv"]["forward"]):
                        for x, vector in enumerate(row):
                            if vector is None:
                                continue
                            values = [m[y][x] for m in window if m[y][x] is not None]
                            for axis in (0, 1):
                                avg = sum(v[axis] for v in values) / len(values)
                                pan_weight = int(shift[axis] != 0)
                                delta = echo * (avg - vector[axis]) + shift[axis]
                                if bursting:
                                    factor = (math.sin(math.tau * y / len(originals[i])) if axis == 0
                                              else math.cos(math.tau * x / len(row)))
                                    delta += vector[axis] * (factor - 1)
                                vector[axis] = round(vector[axis] + delta / max(1, echo + pan_weight + bursting))
                (tmp / "recipe.json").write_text(json.dumps(data, separators=(",", ":")))
                run(str(ENGINE), "-i", str(MEDIA / "work.avi"), *features,
                    "-a", str(tmp / "recipe.json"), "-o", str(tmp / "result.avi"))
                run("ffmpeg", "-v", "error", "-i", str(tmp / "result.avi"), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-g", "6", str(tmp / name))
                (tmp / name).replace(target)
    return f"portrait/{name}"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args):
        super().__init__(*args, directory=ROOT)

    def do_GET(self):
        if not re.fullmatch(r"/(?:portrait\.html|portrait/(?:[1-9]|echo-\d+-\d+(?:-[a-f0-9]{16})?)\.(?:mp4|json))", self.path):
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):
        self.send_error(405)

    def do_POST(self):
        if self.path != "/render" or self.headers.get_content_type() != "application/json":
            self.send_error(404)
            return
        if self.headers.get("Host") != "127.0.0.1:8765" or self.headers.get("Origin") != "http://127.0.0.1:8765":
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 512:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            h, p = data["history"], data["percent"]
            spans = data["spans"]
            pan = data.get("pan", [0, 0, []])
            texture = data.get("texture", [64, []])
            wave = data.get("wave", [0, 5, 1, []])
            if type(h) is not int or type(p) is not int or not (1 <= h <= 24 and 0 <= p <= 100):
                raise ValueError()
            if (not isinstance(texture, list) or len(texture) != 2 or
                    type(texture[0]) is not int or not 1 <= texture[0] <= 64):
                raise ValueError()
            if (not isinstance(wave, list) or len(wave) != 4 or
                    any(type(v) is not int or not a <= v <= b
                        for v, (a,b) in zip(wave[:3], [(0,100),(1,24),(0,999999)]))):
                raise ValueError()
            if (not isinstance(pan, list) or len(pan) != 3 or
                any(type(v) is not int or not -16 <= v <= 16 for v in pan[:2])):
                raise ValueError()
            def validate(ranges):
                if not isinstance(ranges, list) or len(ranges) > 8 or any(
                    not isinstance(span, list) or len(span) != 2 or
                    any(type(value) is not int for value in span) or not 0 <= span[0] < span[1] <= 4000
                    for span in ranges
                ):
                    raise ValueError()
                ranges = sorted(ranges)
                if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
                    raise ValueError()
                return ranges
            spans = validate(spans)
            pan[2] = validate(pan[2])
            texture[1] = validate(texture[1])
            wave[3] = validate(wave[3])
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        try:
            active = (p and spans) or (any(pan[:2]) and pan[2]) or (texture[0] < 64 and texture[1]) or (wave[0] and wave[3])
            path = render(h, p, spans, pan, texture, wave) if active else "portrait/1.mp4"
        except Exception:
            self.send_error(500, "Render failed")
            raise
        body = json.dumps({"path": path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
