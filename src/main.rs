mod media;
mod recipe;

use axum::{
    Json, Router,
    body::{HttpBody, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use media::{Library, Tools};
use serde::Deserialize;
use serde_json::json;
use std::{env, future::poll_fn, pin::Pin, sync::Arc, time::Duration};
use tokio::{fs, io::AsyncWriteExt, net::TcpListener, process::Command, time::timeout};

const PAGE: &str = include_str!("../portrait.html");
const MAX_UPLOAD: usize = 256 * 1024 * 1024;

#[derive(Clone)]
struct App {
    library: Arc<Library>,
    origin: String,
    host: String,
}

type Error = (StatusCode, &'static str);

impl App {
    fn check(&self, headers: &HeaderMap, write: bool) -> Result<(), Error> {
        let value = |name| headers.get(name).and_then(|v| v.to_str().ok());
        if value("host") != Some(self.host.as_str())
            || (write && value("origin") != Some(self.origin.as_str()))
        {
            return Err((StatusCode::FORBIDDEN, "Local browser only"));
        }
        Ok(())
    }
}

async fn page(State(app): State<App>, headers: HeaderMap) -> Result<Html<&'static str>, Error> {
    app.check(&headers, false)?;
    Ok(Html(PAGE))
}

async fn clip(
    State(app): State<App>,
    Path((clip, file)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<Response, Error> {
    app.check(&headers, false)?;
    serve_file(&app.library, &clip, &file).await
}

async fn serve_file(library: &Library, clip: &str, file: &str) -> Result<Response, Error> {
    let mime = match file {
        "work.avi" => "video/x-msvideo",
        "1.mp4" => "video/mp4",
        _ if file
            .strip_prefix("js-")
            .and_then(|s| s.strip_suffix(".mp4"))
            .is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())) =>
        {
            "video/mp4"
        }
        _ => return Err((StatusCode::NOT_FOUND, "File not found")),
    };
    let folder = library
        .folder(clip)
        .ok_or((StatusCode::NOT_FOUND, "Clip not found"))?;
    let bytes = fs::read(folder.join(file))
        .await
        .map_err(|_| (StatusCode::NOT_FOUND, "File not found"))?;
    Ok(([(header::CONTENT_TYPE, mime)], bytes).into_response())
}

#[derive(Deserialize)]
struct RenderRequest {
    clip: String,
    recipe: recipe::Recipe,
}

async fn render(State(app): State<App>, req: Request) -> Result<Json<serde_json::Value>, Error> {
    app.check(req.headers(), true)?;
    if req
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        != Some("application/json")
    {
        return Err((StatusCode::UNSUPPORTED_MEDIA_TYPE, "Expected JSON"));
    }
    let body = to_bytes(req.into_body(), 2048)
        .await
        .map_err(|_| (StatusCode::BAD_REQUEST, "Invalid recipe size"))?;
    let mut input: RenderRequest =
        serde_json::from_slice(&body).map_err(|_| (StatusCode::BAD_REQUEST, "Invalid recipe"))?;
    let id = &input.clip;
    let duration = app
        .library
        .duration(id)
        .await
        .ok_or((StatusCode::BAD_REQUEST, "Unknown clip"))?;
    input
        .recipe
        .validate(duration)
        .map_err(|e| (StatusCode::BAD_REQUEST, e))?;
    let script = input.recipe.script();
    let name = app.library.render(id, &script).await.map_err(|error| {
        eprintln!("Render failed: {error}");
        (StatusCode::INTERNAL_SERVER_ERROR, "Render failed")
    })?;
    let prefix = format!("clips/{id}");
    Ok(Json(
        json!({"path": format!("{prefix}/{name}"), "script": script, "input": format!("{prefix}/work.avi")}),
    ))
}

async fn open_video(
    State(app): State<App>,
    req: Request,
) -> Result<Json<serde_json::Value>, Error> {
    app.check(req.headers(), true)?;
    let (parts, mut body) = req.into_parts();
    let field = |name| parts.headers.get(name).and_then(|v| v.to_str().ok());
    if field("content-type") != Some("application/octet-stream") {
        return Err((StatusCode::UNSUPPORTED_MEDIA_TYPE, "Expected a video file"));
    }
    let start: u64 = field("x-start-ms")
        .unwrap_or("0")
        .parse()
        .ok()
        .filter(|&value| value <= 86_400_000)
        .ok_or((StatusCode::BAD_REQUEST, "Invalid start"))?;
    let size: usize = field("content-length")
        .and_then(|v| v.parse().ok())
        .filter(|&size| size > 0 && size <= MAX_UPLOAD)
        .ok_or((StatusCode::BAD_REQUEST, "Invalid file size"))?;
    let directory = app.library.incoming().map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Cannot create work folder",
        )
    })?;
    let path = directory.path().join("input");
    let mut file = fs::File::create(&path)
        .await
        .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "Cannot create input"))?;
    let mut received = 0;
    timeout(Duration::from_secs(60), async {
        while let Some(frame) = poll_fn(|cx| Pin::new(&mut body).poll_frame(cx)).await {
            if let Ok(bytes) = frame
                .map_err(|_| (StatusCode::BAD_REQUEST, "Invalid file body"))?
                .into_data()
            {
                received += bytes.len();
                if received > size {
                    return Err((StatusCode::BAD_REQUEST, "Unexpected file length"));
                }
                file.write_all(&bytes)
                    .await
                    .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "Cannot store input"))?;
            }
        }
        file.flush()
            .await
            .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "Cannot store input"))?;
        Ok(())
    })
    .await
    .map_err(|_| (StatusCode::REQUEST_TIMEOUT, "File upload timed out"))??;
    drop(file);
    if received != size {
        return Err((StatusCode::BAD_REQUEST, "Unexpected file length"));
    }
    let (clip, orientation, duration_ms) =
        app.library
            .prepare(directory, start)
            .await
            .map_err(|error| {
                eprintln!("Video preparation failed: {error}");
                (StatusCode::UNPROCESSABLE_ENTITY, "Could not prepare video")
            })?;
    Ok(Json(
        json!({"clip":clip,"path":format!("clips/{clip}/1.mp4"),"orientation":orientation,"duration_ms":duration_ms}),
    ))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut port = 8765;
    let mut browser = true;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--port" => port = args.next().ok_or("Missing --port value")?.parse()?,
            "--no-browser" => browser = false,
            "--help" | "-h" => {
                println!("Usage: ffglitch-pads [--port PORT] [--no-browser]");
                return Ok(());
            }
            _ => return Err(format!("Unknown argument: {arg}").into()),
        }
    }
    let tools = Tools::discover().await?;
    let app_library = Arc::new(Library::new(tools)?);
    let listener = TcpListener::bind(("127.0.0.1", port)).await?;
    let host = listener.local_addr()?.to_string();
    let origin = format!("http://{host}");
    let address = format!("{origin}/portrait.html");
    let app = App {
        library: app_library,
        host,
        origin,
    };
    let router = Router::new()
        .route("/", get(page))
        .route("/portrait.html", get(page))
        .route("/clips/{clip}/{file}", get(clip))
        .route("/render", post(render))
        .route("/open-video", post(open_video))
        .with_state(app);
    println!("{address}  (Ctrl+C to stop)");
    if browser
        && !Command::new("open")
            .arg(&address)
            .status()
            .await
            .is_ok_and(|s| s.success())
    {
        eprintln!("Open the address above in your browser.");
    }
    let (stop, stopped) = tokio::sync::oneshot::channel();
    let mut server = tokio::spawn(async move {
        axum::serve(listener, router)
            .with_graceful_shutdown(async {
                let _ = stopped.await;
            })
            .await
    });
    tokio::select! {
        result = &mut server => result??,
        signal = tokio::signal::ctrl_c() => {
            signal?;
            let _ = stop.send(());
            if timeout(Duration::from_secs(3), &mut server).await.is_err() {
                // Dropping the runtime also drops in-flight children (kill_on_drop).
                server.abort();
            }
        }
    }
    Ok(())
}
