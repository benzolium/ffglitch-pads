use std::{
    collections::hash_map::DefaultHasher,
    env,
    hash::{Hash, Hasher},
    io,
    path::{Path, PathBuf},
    time::Duration,
};
use tempfile::TempDir;
use tokio::{fs, process::Command, sync::Mutex, time::timeout};

pub struct Tools {
    ffedit: PathBuf,
    ffmpeg: PathBuf,
    ffprobe: PathBuf,
}

impl Tools {
    pub async fn discover() -> io::Result<Self> {
        let tools = Self {
            ffedit: env::var_os("FFEDIT")
                .map(PathBuf::from)
                .unwrap_or_else(|| "ffedit".into()),
            ffmpeg: env::var_os("FFMPEG")
                .map(PathBuf::from)
                .unwrap_or_else(|| "ffmpeg".into()),
            ffprobe: env::var_os("FFPROBE")
                .map(PathBuf::from)
                .unwrap_or_else(|| "ffprobe".into()),
        };
        for (path, var) in [
            (&tools.ffedit, "FFEDIT"),
            (&tools.ffmpeg, "FFMPEG"),
            (&tools.ffprobe, "FFPROBE"),
        ] {
            run(tool(path).arg("-version"), 5).await.map_err(|err| {
                io::Error::other(format!("Cannot run {} ({var}): {err}", path.display()))
            })?;
        }
        let encoders = run(tool(&tools.ffmpeg).args(["-hide_banner", "-encoders"]), 5).await?;
        if !String::from_utf8_lossy(&encoders)
            .lines()
            .any(|line| line.split_whitespace().nth(1) == Some("libx264"))
        {
            return Err(io::Error::other(format!(
                "{} does not provide the libx264 encoder (FFMPEG)",
                tools.ffmpeg.display()
            )));
        }
        Ok(tools)
    }
}

pub struct Library {
    root: TempDir,
    tools: Tools,
    render_lock: Mutex<()>,
}

impl Library {
    pub fn new(tools: Tools) -> io::Result<Self> {
        let root = tempfile::Builder::new()
            .prefix("ffglitch-pads-")
            .tempdir()?;
        Ok(Self {
            root,
            tools,
            render_lock: Mutex::new(()),
        })
    }

    pub fn folder(&self, clip: &str) -> Option<PathBuf> {
        let suffix = clip.strip_prefix("clip-")?;
        if suffix.len() != 6 || !suffix.bytes().all(|b| b.is_ascii_alphanumeric()) {
            return None;
        }
        let path = self.root.path().join(clip);
        path.join("1.mp4").is_file().then_some(path)
    }

    pub fn incoming(&self) -> io::Result<TempDir> {
        tempfile::Builder::new()
            .prefix("clip-")
            .rand_bytes(6)
            .tempdir_in(self.root.path())
    }

    pub async fn prepare(
        &self,
        folder: TempDir,
        start: u64,
    ) -> io::Result<(String, &'static str, u32)> {
        let destination = folder.path();
        let source = destination.join("input");
        let work = destination.join("work.avi");
        let start = format!("{}.{:03}", start / 1000, start % 1000);
        run(tool(&self.tools.ffmpeg).args(["-nostdin", "-v", "error", "-ss", &start,
            "-protocol_whitelist", "file", "-format_whitelist", "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpeg,mpegts",
            "-i"]).arg(&source).args(["-map", "0:v:0", "-an", "-vf",
            "scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=24",
            "-frames:v", "96", "-c:v", "mpeg2video", "-bf", "0", "-g", "48", "-q:v", "3"])
            .arg(&work), 60).await?;
        let bytes = run(
            tool(&self.tools.ffprobe)
                .args([
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,nb_frames",
                    "-of",
                    "json",
                ])
                .arg(&work),
            10,
        )
        .await?;
        let info: serde_json::Value = serde_json::from_slice(&bytes)?;
        let stream = &info["streams"][0];
        let frames = stream["nb_frames"]
            .as_str()
            .and_then(|s| s.parse::<u32>().ok())
            .filter(|n| (1..=96).contains(n))
            .ok_or_else(|| io::Error::other("No usable video frames"))?;
        let duration_ms = (frames * 1000 + 12) / 24;
        let orientation = if stream["width"].as_u64() >= stream["height"].as_u64() {
            "landscape"
        } else {
            "portrait"
        };
        self.mp4(&work, &destination.join("1.mp4")).await?;
        fs::write(destination.join("duration-ms"), duration_ms.to_string()).await?;
        fs::remove_file(source).await?;
        let id = destination
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| io::Error::other("Invalid clip directory"))?
            .to_owned();
        let _ = folder.keep();
        Ok((id, orientation, duration_ms))
    }

    pub async fn duration(&self, clip: &str) -> Option<u32> {
        let folder = self.folder(clip)?;
        fs::read_to_string(folder.join("duration-ms"))
            .await
            .ok()?
            .parse()
            .ok()
    }

    pub async fn render(&self, clip: &str, script: &str) -> io::Result<String> {
        let folder = self
            .folder(clip)
            .ok_or_else(|| io::Error::other("Unknown clip"))?;
        let mut hash = DefaultHasher::new();
        script.hash(&mut hash);
        let name = format!("js-{}.mp4", hash.finish());
        let target = folder.join(&name);
        let _lock = self.render_lock.lock().await;
        if !target.is_file() {
            let tmp = tempfile::tempdir_in(&folder)?;
            let entry = tmp.path().join("recipe.js");
            let avi = tmp.path().join("result.avi");
            let mp4 = tmp.path().join("preview.mp4");
            fs::write(&entry, script).await?;
            run(
                tool(&self.tools.ffedit)
                    .arg("-i")
                    .arg(folder.join("work.avi"))
                    .arg("-s")
                    .arg(entry)
                    .arg("-o")
                    .arg(&avi),
                60,
            )
            .await?;
            self.mp4(&avi, &mp4).await?;
            fs::rename(mp4, target).await?;
        }
        Ok(name)
    }

    async fn mp4(&self, input: &Path, output: &Path) -> io::Result<()> {
        run(
            tool(&self.tools.ffmpeg)
                .args(["-nostdin", "-v", "error", "-xerror", "-i"])
                .arg(input)
                .args([
                    "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-g", "6",
                ])
                .arg(output),
            60,
        )
        .await
        .map(|_| ())
    }
}

fn tool(path: &Path) -> Command {
    let mut command = Command::new(path);
    command
        .kill_on_drop(true)
        .stdin(std::process::Stdio::null());
    command
}

async fn run(command: &mut Command, seconds: u64) -> io::Result<Vec<u8>> {
    let output = timeout(Duration::from_secs(seconds), command.output())
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "Media command timed out"))??;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "{}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    Ok(output.stdout)
}
