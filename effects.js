// FFglitch 0.10.2: ffedit -i work.avi -s effects.js -o glitched.avi
// Embed a version 1 recipe here, or pass it as a JSON-encoded string via -sp.
const embeddedRecipe = { version: 1, fps: 24, pads: [] };

let effects, fps, index, remaining, history, random;

function seededRandom(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6D2B79F5) >>> 0;
    let n = Math.imul(state ^ (state >>> 15), state | 1);
    n ^= n + Math.imul(n ^ (n >>> 7), n | 61);
    return ((n ^ (n >>> 14)) >>> 0) / 4294967296;
  };
}

export function setup(args) {
  // FFglitch maps some numeric arrays from -sp to native vector objects.
  // Decode recipe text ourselves to preserve pairs of timeline boundaries.
  const recipe = args.params === undefined ? embeddedRecipe : JSON.parse(args.params);
  if (recipe.version !== 1 || !Number.isInteger(recipe.fps) || recipe.fps <= 0) {
    throw new Error('Expected a version 1 recipe with an integer frame rate');
  }
  effects = Object.fromEntries(recipe.pads.map(pad => [pad.effect, pad]));
  fps = recipe.fps;
  index = remaining = 0;
  history = [];
  random = seededRandom(effects['wave-bursts']?.params.seed ?? 0);
  args.features = effects.crumble ? ['mv', 'q_dct'] : ['mv'];
}

function roundEven(value) {
  const floor = Math.floor(value);
  return value - floor === 0.5 ? floor + (floor & 1) : Math.round(value);
}

function trimDetails(frame, cutoff) {
  for (const row of frame.q_dct?.data?.[0] ?? []) {
    for (const block of row) {
      // Keep coded blocks that would otherwise become entirely empty.
      if (block == null || !block.slice(0, cutoff).some(Boolean)) continue;
      for (let k = cutoff; k < block.length; k++) block[k] = 0;
    }
  }
}

export function glitch_frame(frame) {
  const i = index++;
  const range = effect => effects[effect]?.active_ranges_ms.find(
    ([a, b]) => a * fps <= i * 1000 && i * 1000 < b * fps
  );
  if (range('crumble')) trimDetails(frame, effects.crumble.params.cutoff);

  const roll = random() * 100;
  if (!range('wave-bursts')) remaining = 0;
  else if (!remaining && roll < effects['wave-bursts'].params.chance) {
    remaining = effects['wave-bursts'].params.duration;
  }
  const bursting = remaining > 0;
  remaining = Math.max(0, remaining - 1);

  const mv = frame.mv?.forward;
  const echoParams = effects.echo?.params;
  const delayFrames = effects.delay?.params.delay_frames ?? 0;
  const original = mv ? Array.from({ length: mv.height }, (_, y) =>
    Array.from({ length: mv.width }, (_, x) => {
      const v = mv[y][x];
      return v == null ? null : [v[0], v[1]];
    })) : null;
  const historySize = Math.max(echoParams?.history_frames ?? 0, delayFrames ? delayFrames + 1 : 0);
  if (historySize) {
    history.push(original);
    if (history.length > historySize) history.shift();
  }
  const echo = range('echo') ? echoParams.blend_percent / 100 : 0;
  const shift = range('pan') ? [effects.pan.params.x, effects.pan.params.y] : [0, 0];
  const swap = Boolean(range('swap'));
  const circular = Boolean(range('circular'));
  const reverse = Boolean(range('reverse'));
  const zoom = range('zoom') ? effects.zoom.params.amount : 0;
  const delayed = range('delay') ? history[history.length - 1 - delayFrames] : null;
  const phaseRange = range('split-phase');
  const phase = phaseRange ? Math.floor(
    (i - Math.ceil(phaseRange[0] * fps / 1000)) / effects['split-phase'].params.phase_frames
  ) % 2 : -1;
  if (!mv || !(echo || shift.some(Boolean) || bursting || swap || circular || phase >= 0 || reverse || zoom || delayed)) return;

  frame.mv.overflow = 'truncate';
  const echoHistory = echo ? history.slice(-echoParams.history_frames) : [];
  for (let y = 0; y < mv.height; y++) {
    for (let x = 0; x < mv.width; x++) {
      const clean = original[y][x];
      if (clean == null) continue;
      const values = echoHistory.flatMap(old => old?.[y][x] == null ? [] : [old[y][x]]);
      const delayedVector = delayed?.[y]?.[x];
      for (let axis = 0; axis < 2; axis++) {
        const v = clean[axis];
        const avg = values.length ? values.reduce((sum, n) => sum + n[axis], 0) / values.length : v;
        let delta = echo * (avg - v) + shift[axis];
        if (bursting) {
          const factor = axis === 0 ? Math.sin(2 * Math.PI * y / mv.height) :
            Math.cos(2 * Math.PI * x / mv.width);
          delta += v * (factor - 1);
        }
        if (swap) delta += clean[1 - axis] - v;
        if (circular) delta += v * ((axis === 0 ? Math.sin(x) : Math.cos(y)) - 1);
        if (phase === axis) delta -= v;
        if (reverse) delta -= 2 * v;
        if (zoom) delta += ((axis === 0 ? (mv.width - 1) / 2 - x : (mv.height - 1) / 2 - y) / 100) * zoom;
        if (delayedVector) delta += delayedVector[axis] - v;
        const weight = echo + Number(shift[axis] !== 0) + Number(bursting) +
          Number(swap) + Number(circular) + Number(phase === axis) +
          Number(reverse) + Number(zoom !== 0) + Number(Boolean(delayedVector));
        mv[y][x][axis] = roundEven(v + delta / Math.max(1, weight));
      }
    }
  }
}
