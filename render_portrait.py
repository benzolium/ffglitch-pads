import json
from pathlib import Path

from render_grid import run

engine = "./ffglitch-0.10.2-macos-aarch64/ffedit"
out = Path("portrait")
out.mkdir(exist_ok=True)
source = str(out / "work.avi")
data_path = out / "source.json"
scratch = out / "recipe-data.json"
render = str(out / "render.avi")
if not data_path.exists():
    run("ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", "street.mp4", "-t", "4",
        "-an", "-vf", "scale=360:640,fps=24", "-c:v", "mpeg2video", "-bf", "0",
        "-g", "48", "-q:v", "3", source)
    run(engine, "-i", source, "-f", "mv", "-e", str(data_path))
clean = data_path.read_text()
for row, blend in enumerate((0, .55, 1)):
    for col, size in enumerate((3, 8, 16)):
        slot = row * 3 + col + 1
        recipe = {"effect": "echo", "history_frames": size, "blend": blend,
                  "start_frame": 24, "end_frame_exclusive": 72, "fps": 24}
        (out / f"{slot}.json").write_text(json.dumps(recipe, indent=2) + "\n")
        media = source
        if blend:
            data = json.loads(clean)
            original = json.loads(clean)["streams"][0]["frames"]
            for i, frame in enumerate(data["streams"][0]["frames"]):
                if not 24 <= i < 72:
                    continue
                old = [f["mv"]["forward"] for f in original[max(0, i-size+1):i+1]
                       if f.get("mv", {}).get("forward")]
                mv = frame.get("mv", {})
                for y, strip in enumerate(mv.get("forward", [])):
                    for x, vector in enumerate(strip):
                        if vector is None:
                            continue
                        values = [m[y][x] for m in old if m[y][x] is not None]
                        if values:
                            for axis in range(2):
                                avg = sum(v[axis] for v in values) / len(values)
                                vector[axis] = round(vector[axis] + blend * (avg - vector[axis]))
            scratch.write_text(json.dumps(data, separators=(",", ":")))
            run(engine, "-i", source, "-f", "mv", "-a", str(scratch), "-y", "-o", render)
            media = render
        run("ffmpeg", "-v", "error", "-y", "-i", media, "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-g", "6", str(out / f"{slot}.mp4"))
