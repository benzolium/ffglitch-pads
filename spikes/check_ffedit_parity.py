"""Run from the repo root: python3 spikes/check_ffedit_parity.py."""
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve_xy as legacy


def guarded_crumble(frame, cutoff):
    planes = frame.get("q_dct", {}).get("data", [])
    if planes:
        for row in planes[0]:
            for block in row:
                if block is not None and any(block[:cutoff]):
                    block[cutoff:] = [0] * max(0, len(block) - cutoff)


def decoded(path):
    output = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "framemd5", "-"],
        check=True, capture_output=True,
    )
    hashes = [line.rsplit(b",", 1)[-1] for line in output.stdout.splitlines()
              if not line.startswith(b"#")]
    return hashes, len(output.stderr.splitlines())


def encoded_packets(path):
    """Compare the edited MPEG-2 stream, not decoder-dependent pixels."""
    return subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
        "-show_entries", "packet=data_hash", "-show_data_hash", "sha256",
        "-of", "csv=p=0", str(path),
    ]).splitlines()


def main():
    script = Path(__file__).with_name("ffedit-recipe.js").read_text()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for config, cutoff, guard in [
            ("motion", 64, False), ("original mix", 2, False), ("guarded mix", 2, True),
        ]:
            folder = root / config
            folder.mkdir()
            for name in ("work.avi", "source.json", "texture-source.json"):
                (folder / name).symlink_to(legacy.MEDIA / name)
            js = script.replace("cutoff: 2", f"cutoff: {cutoff}")
            if not guard:
                js = js.replace(" || !block.slice(0, recipe.crumble.cutoff).some(Boolean)", "")
            (folder / "recipe.js").write_text(js)
            js_output = folder / "js.avi"
            subprocess.run(
                [str(legacy.ENGINE), "-i", str(folder / "work.avi"), "-s",
                 str(folder / "recipe.js"), "-o", str(js_output)],
                check=True, capture_output=True,
            )

            def keep_avi(*args):
                if args[0] == "ffmpeg":
                    shutil.copyfile(args[args.index("-i") + 1], args[-1])
                else:
                    subprocess.run(args, check=True, capture_output=True)

            with patch.object(legacy, "run", keep_avi), patch.object(
                legacy, "crumble", guarded_crumble if guard else legacy.crumble
            ):
                reference = legacy.render(
                    folder, 16, 100, [[1000, 3000]], [12, 0, [[2000, 3500]]],
                    [cutoff, [[500, 1600]]], [22, 6, 42, [[2400, 3900]]], {},
                )
            a, js_errors = decoded(js_output)
            b, py_errors = decoded(folder / reference)
            equal = sum(x == y for x, y in zip(a, b))
            print(f"{config}: {equal}/{len(a)} equal; decode log lines JS/Python: {js_errors}/{py_errors}")
            assert len(a) == len(b) == 96
            if config != "original mix":
                assert equal == 96 and js_errors == py_errors == 0


if __name__ == "__main__":
    main()
