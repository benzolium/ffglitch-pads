import json
import subprocess
from pathlib import Path


def drift(frame, offset):
    mv = frame.get("mv", {})
    mv["overflow"] = "truncate"
    for row in mv.get("forward", []):
        for vector in row:
            if vector is not None:
                vector[0] += offset


def crumble(frame, cutoff):
    planes = frame.get("q_dct", {}).get("data", [])
    if not planes:
        return
    for row in planes[0]:
        for block in row:
            if block is not None:
                block[cutoff:] = [0] * max(0, len(block) - cutoff)


def run(*args):
    subprocess.run(args, check=True)


def main():
    engine = "./ffglitch-0.10.2-macos-aarch64/ffedit"
    source = Path("sample.json").read_text()
    output = Path("loops")
    output.mkdir(exist_ok=True)
    scratch = Path("recipe-data.json")
    for row, cutoff in enumerate((64, 12, 2)):
        for column, offset in enumerate((0, 6, 18)):
            slot = row * 3 + column + 1
            recipe = {"drift_offset": offset, "crumble_cutoff": cutoff}
            (output / f"{slot}.json").write_text(json.dumps(recipe, indent=2) + "\n")
            media = "work.avi"
            if slot != 1:
                data = json.loads(source)
                for frame in data["streams"][0]["frames"]:
                    if offset:
                        drift(frame, offset)
                    if cutoff < 64:
                        crumble(frame, cutoff)
                scratch.write_text(json.dumps(data, separators=(",", ":")))
                media = "render.avi"
                run(engine, "-i", "work.avi", "-f", "mv", "-f", "q_dct",
                    "-a", str(scratch), "-y", "-o", media)
            run("ffmpeg", "-v", "error", "-y", "-i", media, "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-g", "12", str(output / f"{slot}.mp4"))


if __name__ == "__main__":
    main()
