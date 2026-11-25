"""Run from the repo root: python3 spikes/check_effects.py."""
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from check_ffedit_parity import decoded, encoded_packets, guarded_crumble, legacy


def pad(slot, effect, params, start, end):
    return dict(slot=slot, effect=effect, params=params, active_ranges_ms=[[start, end]])


def keep_avi(*args):
    if args[0] == "ffmpeg":
        shutil.copyfile(args[args.index("-i") + 1], args[-1])
    else:
        subprocess.run(args, check=True, capture_output=True)


def main():
    script = Path(__file__).resolve().parents[1] / "effects.js"
    base = [
        pad(1, "echo", dict(history_frames=16, blend_percent=100), 1000, 3000),
        pad(2, "pan", dict(x=12, y=0), 2000, 3500),
        pad(3, "crumble", dict(cutoff=2), 500, 1600),
    ]
    with TemporaryDirectory() as directory:
        folder = Path(directory)
        for name in ("work.avi", "source.json", "texture-source.json"):
            (folder / name).symlink_to(legacy.MEDIA / name)

        def render_js(name, recipe, standalone=False):
            encoded = json.dumps(recipe)
            output = folder / f"{name}.avi"
            entry, params = script, ["-sp", json.dumps(encoded)]
            if standalone:
                entry, params = folder / "standalone.js", []
                entry.write_text(script.read_text().replace(
                    "{ version: 1, fps: 24, pads: [] }", encoded, 1))
            subprocess.run(
                [str(legacy.ENGINE), "-i", str(folder / "work.avi"), "-s",
                 str(entry), *params, "-o", str(output)], check=True, capture_output=True,
            )
            hashes, errors = decoded(output)
            assert len(hashes) == 96 and not errors, (name, len(hashes), errors)
            packets = encoded_packets(output)
            assert len(packets) == 96
            return packets

        for effect, params in [
            (None, {}), ("swap", {}), ("split-phase", {"phase_frames": 5}), ("circular", {}),
        ]:
            recipe = dict(version=1, fps=24, pads=[*base, pad(4, effect, params, 1700, 3500)] if effect else base)
            with patch.object(legacy, "run", keep_avi), patch.object(legacy, "crumble", guarded_crumble):
                reference = legacy.render(folder, *legacy.parse_recipe(recipe))
            _, errors = decoded(folder / reference)
            expected = encoded_packets(folder / reference)
            actual = render_js(effect, recipe)
            assert not errors and actual == expected, (effect, [i for i,(a,b) in enumerate(zip(actual,expected)) if a != b][:15], errors)
            print(f"{effect} + echo/pan/crumble: 96/96 identical MPEG-2 packets, no decode errors")

        def with_wave(seed):
            return dict(version=1, fps=24, pads=[*base, pad(
                4, "wave-bursts", dict(chance=22, duration=6, seed=seed), 2400, 3900)])

        actual = render_js("wave-42", with_wave(42))
        assert actual == render_js("wave-42-standalone", with_wave(42), standalone=True)
        assert actual != render_js("wave-43", with_wave(43))
        print("wave: deterministic across launches, seed changes output, embedded recipe matches")


if __name__ == "__main__":
    main()
