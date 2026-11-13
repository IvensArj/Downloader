#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use serde::Serialize;
use studio_core::{Job, Metadata, Preset, Source, Studio, Tools};
use tauri::{Manager, State};

struct AppState {
    studio: Studio,
    default_folder: String,
    sample: PathBuf,
    closing: AtomicBool,
    tools: Tools,
}

#[derive(Serialize)]
struct Settings {
    folder: String,
    tools_ready: bool,
}

fn human(error: impl std::fmt::Display) -> String {
    format!("{error:#}")
}

#[tauri::command]
fn settings(state: State<'_, AppState>) -> Settings {
    Settings {
        folder: state.default_folder.clone(),
        tools_ready: state.tools.available(),
    }
}

#[tauri::command]
fn list_jobs(state: State<'_, AppState>) -> Result<Vec<Job>, String> {
    state.studio.list().map_err(human)
}

#[tauri::command]
async fn inspect_source(state: State<'_, AppState>, source: Source) -> Result<Metadata, String> {
    let studio = state.studio.clone();
    tauri::async_runtime::spawn_blocking(move || studio.inspect(&source).map_err(human))
        .await
        .map_err(human)?
}

#[tauri::command]
fn add_jobs(
    state: State<'_, AppState>,
    sources: Vec<Source>,
    preset: Preset,
    folder: String,
) -> Result<(), String> {
    state
        .studio
        .add(&sources, preset, Path::new(&folder))
        .map_err(human)
}

#[tauri::command]
fn cancel_job(state: State<'_, AppState>, id: i64) -> Result<(), String> {
    state.studio.cancel(id).map_err(human)
}

#[tauri::command]
fn retry_job(state: State<'_, AppState>, id: i64) -> Result<(), String> {
    state.studio.retry(id).map_err(human)
}

#[tauri::command]
fn repeat_batch(state: State<'_, AppState>, id: i64) -> Result<(), String> {
    state.studio.repeat(id).map_err(human)
}

#[tauri::command]
fn open_output(state: State<'_, AppState>, id: i64) -> Result<(), String> {
    open::that_detached(state.studio.output(id).map_err(human)?).map_err(human)
}

#[tauri::command]
fn sample_path(state: State<'_, AppState>) -> Result<String, String> {
    if state.sample.is_file() {
        Ok(state.sample.to_string_lossy().into_owned())
    } else {
        Err("O vídeo de exemplo não está disponível neste pacote.".into())
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let resources = app.path().resource_dir()?;
            let tools = Tools::new(Some(resources.join("bin")));
            let data = std::env::var_os("MEDIA_STUDIO_DATA_DIR")
                .map(PathBuf::from)
                .map(Ok)
                .unwrap_or_else(|| app.path().app_local_data_dir())?;
            let studio = Studio::open(&data.join("fila.sqlite3"), tools.clone())?;
            studio.start_workers();
            let sample = resources.join("resources/exemplo.mp4");
            let sample = if sample.is_file() {
                sample
            } else {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources/exemplo.mp4")
            };
            let folder = std::env::var_os("MEDIA_STUDIO_OUTPUT_DIR")
                .map(PathBuf::from)
                .map(Ok)
                .unwrap_or_else(|| {
                    app.path()
                        .download_dir()
                        .or_else(|_| app.path().document_dir())
                })?;
            app.manage(AppState {
                studio,
                sample,
                default_folder: folder.to_string_lossy().into_owned(),
                closing: AtomicBool::new(false),
                tools,
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            settings,
            list_jobs,
            inspect_source,
            add_jobs,
            cancel_job,
            retry_job,
            repeat_batch,
            open_output,
            sample_path
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<AppState>();
                api.prevent_close();
                if !state.closing.swap(true, Ordering::SeqCst) {
                    state.studio.shutdown();
                    let app = window.app_handle().clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(Duration::from_millis(350));
                        app.exit(0);
                    });
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("Não foi possível iniciar o Estúdio de Mídia");
}
