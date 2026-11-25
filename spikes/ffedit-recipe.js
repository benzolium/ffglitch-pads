// Standalone parity fixture for FFglitch 0.10.2, MPEG-2, 24 fps, 96 frames.
const recipe = {
  echo: { history: 16, percent: 100, ranges: [[1000, 3000]] },
  pan: { x: 12, y: 0, ranges: [[2000, 3500]] },
  crumble: { cutoff: 2, ranges: [[500, 1600]] },
  wave: { chance: 22, duration: 6, ranges: [[2400, 3900]] },
};

// Python Random(42).randrange(100), frozen only for the parity comparison.
const rolls = [
  81,14,3,94,35,31,28,17,94,13,86,94,69,11,75,54,4,3,11,27,29,64,77,3,
  71,25,91,83,89,69,53,28,57,75,35,0,97,20,89,54,43,35,19,27,97,43,13,11,
  48,12,45,44,77,33,5,93,58,68,15,48,10,70,37,80,79,46,73,24,90,8,5,84,
  29,98,37,10,29,12,48,35,58,81,46,20,47,45,26,85,34,89,87,82,9,77,81,21,
];

let index;
let remaining;
let history;
export function setup(args) {
  args.features = recipe.crumble.cutoff < 64 ? ['mv', 'q_dct'] : ['mv'];
  index = remaining = 0;
  history = [];
}

function roundEven(value) {
  const floor = Math.floor(value);
  return value - floor === 0.5 ? floor + (floor & 1) : Math.round(value);
}

export function glitch_frame(frame) {
  const i = index++;
  const applies = ranges => ranges.some(([a, b]) => a * 24 <= i * 1000 && i * 1000 < b * 24);
  if (recipe.crumble.cutoff < 64 && applies(recipe.crumble.ranges)) {
    for (const row of frame.q_dct?.data?.[0] ?? []) {
      for (const block of row) {
        // A coded block cannot be left with no coefficients by this edit.
        if (block == null || !block.slice(0, recipe.crumble.cutoff).some(Boolean)) continue;
        for (let k = recipe.crumble.cutoff; k < block.length; k++) block[k] = 0;
      }
    }
  }

  const mv = frame.mv?.forward;
  const original = mv ? Array.from({ length: mv.height }, (_, y) =>
    Array.from({ length: mv.width }, (_, x) => {
      const v = mv[y][x];
      return v == null ? null : [v[0], v[1]];
    })) : null;
  history.push(original);
  if (history.length > recipe.echo.history) history.shift();

  const echo = applies(recipe.echo.ranges) ? recipe.echo.percent / 100 : 0;
  const shift = applies(recipe.pan.ranges) ? [recipe.pan.x, recipe.pan.y] : [0, 0];
  if (!applies(recipe.wave.ranges)) remaining = 0;
  else if (!remaining && rolls[i] < recipe.wave.chance) remaining = recipe.wave.duration;
  const bursting = remaining > 0;
  remaining = Math.max(0, remaining - 1);
  if (!mv || !(echo || shift.some(Boolean) || bursting)) return;

  frame.mv.overflow = 'truncate';
  for (let y = 0; y < mv.height; y++) {
    for (let x = 0; x < mv.width; x++) {
      const v = original[y][x];
      if (v == null) continue;
      const values = history.flatMap(old => old?.[y][x] == null ? [] : [old[y][x]]);
      for (let axis = 0; axis < 2; axis++) {
        const avg = values.reduce((sum, n) => sum + n[axis], 0) / values.length;
        let delta = echo * (avg - v[axis]) + shift[axis];
        if (bursting) {
          const factor = axis === 0 ? Math.sin(2 * Math.PI * y / mv.height) :
            Math.cos(2 * Math.PI * x / mv.width);
          delta += v[axis] * (factor - 1);
        }
        const weight = echo + Number(shift[axis] !== 0) + Number(bursting);
        mv[y][x][axis] = roundEven(v[axis] + delta / Math.max(1, weight));
      }
    }
  }
}
