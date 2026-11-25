use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

const SCRIPT: &str = include_str!("../effects.js");
const PLACEHOLDER: &str = "const embeddedRecipe = { version: 1, fps: 24, pads: [] };";

#[derive(Deserialize, Serialize)]
pub struct Recipe {
    version: u32,
    fps: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    source: Option<Source>,
    pads: Vec<Pad>,
}

#[derive(Deserialize, Serialize)]
struct Source {
    name: String,
    start_ms: u64,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Pad {
    slot: u8,
    effect: String,
    params: BTreeMap<String, Value>,
    active_ranges_ms: Vec<[u32; 2]>,
}

impl Recipe {
    pub fn validate(&mut self) -> Result<(), &'static str> {
        if self.version != 1 || self.fps != 24 || self.pads.len() > 4 {
            return Err("Unsupported recipe");
        }
        let mut slots = [false; 4];
        let mut effects = std::collections::HashSet::new();
        for pad in &mut self.pads {
            if !(1..=4).contains(&pad.slot) || slots[usize::from(pad.slot - 1)] {
                return Err("Invalid or repeated slot");
            }
            slots[usize::from(pad.slot - 1)] = true;
            if !effects.insert(pad.effect.as_str()) {
                return Err("Repeated effect");
            }
            let limits: &[(&str, i64, i64)] = match pad.effect.as_str() {
                "echo" => &[("history_frames", 1, 24), ("blend_percent", 0, 100)],
                "pan" => &[("x", -16, 16), ("y", -16, 16)],
                "wave-bursts" => &[("chance", 0, 100), ("duration", 1, 24), ("seed", 0, 999999)],
                "crumble" => &[("cutoff", 1, 64)],
                "split-phase" => &[("phase_frames", 1, 24)],
                "swap" | "circular" => &[],
                _ => return Err("Unknown effect"),
            };
            if pad.params.len() != limits.len()
                || !limits.iter().all(|&(key, low, high)| {
                    pad.params
                        .get(key)
                        .and_then(Value::as_i64)
                        .is_some_and(|v| (low..=high).contains(&v))
                })
            {
                return Err("Invalid parameters");
            }
            pad.active_ranges_ms.sort_unstable();
            if pad.active_ranges_ms.len() > 8
                || pad
                    .active_ranges_ms
                    .iter()
                    .any(|[a, b]| a >= b || *b > 4000)
                || pad.active_ranges_ms.windows(2).any(|p| p[0][1] > p[1][0])
            {
                return Err("Invalid or overlapping ranges");
            }
        }
        Ok(())
    }

    pub fn script(&self) -> String {
        debug_assert_eq!(SCRIPT.matches(PLACEHOLDER).count(), 1);
        let data = serde_json::to_string_pretty(self).expect("serializable recipe");
        SCRIPT.replacen(PLACEHOLDER, &format!("const embeddedRecipe = {data};"), 1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn validates_ranges_per_pad_and_embeds_recipe() {
        let mut recipe: Recipe = serde_json::from_value(json!({
            "version":1,"fps":24,"pads":[
                {"slot":1,"effect":"swap","params":{},"active_ranges_ms":[[300,500],[100,300]]},
                {"slot":2,"effect":"circular","params":{},"active_ranges_ms":[[200,400]]}
            ]
        }))
        .unwrap();
        assert!(recipe.validate().is_ok());
        assert_eq!(recipe.pads[0].active_ranges_ms, [[100, 300], [300, 500]]);
        assert!(recipe.script().contains("\"effect\": \"circular\""));
        assert!(!recipe.script().contains(PLACEHOLDER));
        recipe.pads[0].active_ranges_ms[1][0] = 299;
        assert!(recipe.validate().is_err());
    }
}
