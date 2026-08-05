#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct StartJobRequest {
    disc_label: String,
    media_type: String,
    movie_mode: Option<String>,
    disc_scope: Option<String>,
    season_number: Option<i64>,
    episode_range_start: Option<i64>,
    episode_range_end: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize)]
struct JobSummary {
    id: String,
    disc_label: String,
    media_type: String,
    movie_mode: Option<String>,
    has_local_artifacts: Option<bool>,
    disc_scope: Option<String>,
    season_number: Option<i64>,
    episode_range_start: Option<i64>,
    episode_range_end: Option<i64>,
    status: String,
    current_stage: Option<String>,
    updated_at: String,
    error_message: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct DiscDrive {
    drive: String,
    root: String,
    has_media: bool,
    volume_label: String,
}

#[derive(Debug)]
struct RuntimePaths {
    repo_root: Option<PathBuf>,
    app_main: Option<PathBuf>,
    backend_exe: Option<PathBuf>,
    config_path: PathBuf,
    working_dir: PathBuf,
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            list_jobs,
            job_snapshot,
            detect_disc,
            start_pipeline,
            resume_pipeline,
            analyze_menu,
            rerun_mapping,
            rerun_identify,
            search_tmdb_candidates,
            select_tmdb_candidate,
            override_mapping,
            override_mapping_source,
            ignore_mapping,
            override_split,
            plan_splits,
            update_job_profile,
            cancel_job,
            clear_local_artifacts,
            rebuild_output,
            remap_remote_output,
            delete_job_cmd,
            open_path
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[tauri::command]
fn list_jobs() -> Result<Vec<JobSummary>, String> {
    let value = run_python_json(&["job", "list"])?;
    let jobs = value
        .get("jobs")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "Invalid jobs payload".to_string())?;
    jobs.iter()
        .map(|job| serde_json::from_value::<JobSummary>(job.clone()).map_err(|e| e.to_string()))
        .collect()
}

#[tauri::command]
fn job_snapshot(job_id: String) -> Result<Value, String> {
    run_python_json(&["job", "snapshot", &job_id])
}

#[tauri::command]
fn detect_disc() -> Result<Option<DiscDrive>, String> {
    let value = run_python_json(&["rip", "drives"])?;
    let drives = value
        .get("drives")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "Invalid drives payload".to_string())?;
    for drive in drives {
        let parsed: DiscDrive = serde_json::from_value(drive.clone()).map_err(|e| e.to_string())?;
        if parsed.has_media {
            return Ok(Some(parsed));
        }
    }
    Ok(None)
}

#[tauri::command]
fn start_pipeline(request: StartJobRequest) -> Result<String, String> {
    let mut create_args: Vec<String> = vec![
        "job".into(),
        "create".into(),
        "--disc-label".into(),
        request.disc_label,
        "--media-type".into(),
        request.media_type,
    ];
    if let Some(scope) = request.disc_scope {
        create_args.push("--disc-scope".into());
        create_args.push(scope);
    }
    if let Some(movie_mode) = request.movie_mode {
        create_args.push("--movie-mode".into());
        create_args.push(movie_mode);
    }
    if let Some(season) = request.season_number {
        create_args.push("--season-number".into());
        create_args.push(season.to_string());
    }
    if let Some(start) = request.episode_range_start {
        create_args.push("--episode-range-start".into());
        create_args.push(start.to_string());
    }
    if let Some(end) = request.episode_range_end {
        create_args.push("--episode-range-end".into());
        create_args.push(end.to_string());
    }
    let output = run_python_text(&create_args.iter().map(String::as_str).collect::<Vec<_>>())?;
    let job_id = output.lines().last().unwrap_or("").trim().to_string();
    if job_id.is_empty() {
        return Err("Failed to create job".to_string());
    }
    spawn_python_background(&["pipeline", "run", &job_id])?;
    Ok(job_id)
}

#[tauri::command]
fn resume_pipeline(job_id: String) -> Result<(), String> {
    spawn_python_background(&["pipeline", "run", &job_id])
}

#[tauri::command]
fn analyze_menu(job_id: String) -> Result<(), String> {
    spawn_python_background(&["mapping", "analyze-menu", &job_id])
}

#[tauri::command]
fn rerun_mapping(job_id: String) -> Result<(), String> {
    spawn_python_background(&["mapping", "run", &job_id])
}

#[tauri::command]
fn rerun_identify(job_id: String) -> Result<(), String> {
    spawn_python_background(&["tmdb", "identify", &job_id])
}

#[tauri::command]
fn search_tmdb_candidates(job_id: String, query: String) -> Result<(), String> {
    let _ = run_python_text(&["tmdb", "search", &job_id, &query])?;
    Ok(())
}

#[tauri::command]
fn select_tmdb_candidate(job_id: String, media_type: String, tmdb_id: i64, slot_index: Option<i64>) -> Result<(), String> {
    let resume_job_id = job_id.clone();
    let mut args = vec![
        "tmdb".to_string(),
        "select".to_string(),
        job_id,
        media_type,
        tmdb_id.to_string(),
    ];
    if let Some(slot_index) = slot_index {
        args.push("--slot-index".to_string());
        args.push(slot_index.to_string());
    }
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let _ = run_python_text(&refs)?;
    spawn_python_background(&["pipeline", "run", &resume_job_id])?;
    Ok(())
}

#[tauri::command]
fn override_mapping(mapping_id: i64, episode_start: i64, episode_end: i64) -> Result<(), String> {
    let _ = run_python_text(&[
        "mapping",
        "override",
        &mapping_id.to_string(),
        &episode_start.to_string(),
        &episode_end.to_string(),
    ])?;
    Ok(())
}

#[tauri::command]
fn override_mapping_source(mapping_id: i64, rip_title_id: i64) -> Result<(), String> {
    let _ = run_python_text(&[
        "mapping",
        "source-override",
        &mapping_id.to_string(),
        &rip_title_id.to_string(),
    ])?;
    Ok(())
}

#[tauri::command]
fn ignore_mapping(mapping_id: i64) -> Result<(), String> {
    let _ = run_python_text(&[
        "mapping",
        "ignore",
        &mapping_id.to_string(),
    ])?;
    Ok(())
}

#[tauri::command]
fn override_split(split_plan_id: i64, start: Option<f64>, end: Option<f64>) -> Result<(), String> {
    let mut args = vec![
        "split".to_string(),
        "override".to_string(),
        split_plan_id.to_string(),
    ];
    if let Some(value) = start {
        args.push("--start".to_string());
        args.push(value.to_string());
    }
    if let Some(value) = end {
        args.push("--end".to_string());
        args.push(value.to_string());
    }
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let _ = run_python_text(&refs)?;
    Ok(())
}

#[tauri::command]
fn plan_splits(job_id: String) -> Result<(), String> {
    let _ = run_python_text(&["split", "plan", &job_id])?;
    Ok(())
}

#[tauri::command]
fn update_job_profile(
    job_id: String,
    disc_scope: String,
    season_number: Option<i64>,
    episode_range_start: Option<i64>,
    episode_range_end: Option<i64>,
) -> Result<(), String> {
    let mut args = vec![
        "job".to_string(),
        "set-profile".to_string(),
        job_id,
        "--disc-scope".to_string(),
        disc_scope,
    ];
    if let Some(value) = season_number {
        args.push("--season-number".to_string());
        args.push(value.to_string());
    }
    if let Some(value) = episode_range_start {
        args.push("--episode-range-start".to_string());
        args.push(value.to_string());
    }
    if let Some(value) = episode_range_end {
        args.push("--episode-range-end".to_string());
        args.push(value.to_string());
    }
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let _ = run_python_text(&refs)?;
    Ok(())
}

#[tauri::command]
fn cancel_job(job_id: String) -> Result<(), String> {
    let _ = run_python_text(&["job", "cancel", &job_id])?;
    Ok(())
}

#[tauri::command]
fn clear_local_artifacts(job_id: String) -> Result<(), String> {
    spawn_python_background(&["job", "clear-local", &job_id])
}

#[tauri::command]
fn rebuild_output(job_id: String) -> Result<(), String> {
    spawn_python_background(&["job", "rebuild-output", &job_id])
}

#[tauri::command]
fn remap_remote_output(job_id: String) -> Result<(), String> {
    let _ = run_python_text(&["job", "remap-remote", &job_id])?;
    Ok(())
}

#[tauri::command(name = "delete_job")]
fn delete_job_cmd(job_id: String) -> Result<(), String> {
    let _ = run_python_text(&["job", "delete", &job_id])?;
    Ok(())
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        Command::new("cmd")
            .args(["/C", "start", "", &path])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(&path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        Command::new("xdg-open")
            .arg(&path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
}

fn spawn_python_background(args: &[&str]) -> Result<(), String> {
    let runtime = resolve_runtime_paths()?;
    let mut command = build_python_command(&runtime, args)?;
    command
        .current_dir(&runtime.working_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn run_python_json(args: &[&str]) -> Result<Value, String> {
    let raw = run_python_text(args)?;
    serde_json::from_str::<Value>(&raw).map_err(|e| format!("Invalid JSON: {e}\nOutput: {raw}"))
}

fn run_python_text(args: &[&str]) -> Result<String, String> {
    let runtime = resolve_runtime_paths()?;

    let output = build_python_command(&runtime, args)?
        .current_dir(&runtime.working_dir)
        .output()
        .map_err(|e| e.to_string())?;

    if output.status.success() {
        String::from_utf8(output.stdout).map_err(|e| e.to_string())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();
        Err(if stderr.is_empty() {
            "Python command failed".to_string()
        } else {
            stderr
        })
    }
}

fn build_python_command(runtime: &RuntimePaths, args: &[&str]) -> Result<Command, String> {
    if let Some(backend_exe) = &runtime.backend_exe {
        let mut cmd = Command::new(backend_exe);
        cmd.arg("--config");
        cmd.arg(&runtime.config_path);
        cmd.args(args);
        return Ok(cmd);
    }

    let app_main = runtime
        .app_main
        .as_ref()
        .ok_or_else(|| "No backend executable or app/main.py could be resolved".to_string())?;

    if cfg!(target_os = "windows") {
        let mut cmd = Command::new("py");
        cmd.arg("-3.11");
        cmd.arg(app_main);
        cmd.arg("--config");
        cmd.arg(&runtime.config_path);
        cmd.args(args);
        return Ok(cmd);
    }

    let mut cmd = Command::new("python3");
    cmd.arg(app_main);
    cmd.arg("--config");
    cmd.arg(&runtime.config_path);
    cmd.args(args);
    Ok(cmd)
}

fn resolve_runtime_paths() -> Result<RuntimePaths, String> {
    let repo_root = find_repo_root().ok();
    let resources_dir = find_resources_dir();
    let backend_exe = resources_dir
        .as_ref()
        .map(|dir| dir.join("backend").join(if cfg!(target_os = "windows") { "autorippr-backend.exe" } else { "autorippr-backend" }))
        .filter(|path| path.exists());

    let config_path = resolve_user_config_path()?;
    if !config_path.exists() {
        seed_user_config(&config_path, resources_dir.as_deref(), repo_root.as_deref())?;
    }

    let working_dir = repo_root
        .clone()
        .or_else(|| resources_dir.clone())
        .or_else(|| config_path.parent().map(Path::to_path_buf))
        .ok_or_else(|| "Could not determine a working directory for backend commands".to_string())?;

    let app_main = repo_root
        .as_ref()
        .map(|root| root.join("app").join("main.py"))
        .filter(|path| path.exists());

    Ok(RuntimePaths {
        repo_root,
        app_main,
        backend_exe,
        config_path,
        working_dir,
    })
}

fn resolve_user_config_path() -> Result<PathBuf, String> {
    let base = std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("LOCALAPPDATA").map(PathBuf::from))
        .ok_or_else(|| "Could not locate APPDATA or LOCALAPPDATA for user config".to_string())?;
    let dir = base.join("Auto-Ripper");
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir.join("config.json"))
}

fn seed_user_config(config_path: &Path, resources_dir: Option<&Path>, repo_root: Option<&Path>) -> Result<(), String> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(dir) = resources_dir {
        candidates.push(dir.join("config").join("config.example.json"));
    }
    if let Some(root) = repo_root {
        candidates.push(root.join("app").join("config.local.json"));
        candidates.push(root.join("app").join("config.json"));
        candidates.push(root.join("app").join("config.example.json"));
    }
    let source = candidates
        .into_iter()
        .find(|path| path.exists())
        .ok_or_else(|| "Could not find a config template or local config to seed user config".to_string())?;
    fs::copy(source, config_path).map_err(|e| e.to_string())?;
    Ok(())
}

fn find_resources_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let exe_dir = exe.parent()?;
    let candidates = [
        exe_dir.join("resources"),
        exe_dir.join("..").join("Resources"),
        exe_dir.join("..").join("resources"),
    ];
    candidates.into_iter().find(|path| path.exists())
}

fn find_repo_root() -> Result<PathBuf, String> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        candidates.push(PathBuf::from(manifest));
    }
    if let Ok(current) = std::env::current_dir() {
        candidates.push(current);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            candidates.push(parent.to_path_buf());
        }
    }

    for candidate in candidates {
        for ancestor in candidate.ancestors() {
            if ancestor.join("app").join("main.py").exists() {
                return Ok(ancestor.to_path_buf());
            }
        }
    }
    Err("Could not locate repository root containing app/main.py".to_string())
}
