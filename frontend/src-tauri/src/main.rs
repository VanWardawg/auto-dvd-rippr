#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct StartJobRequest {
    disc_label: String,
    optical_drive: Option<String>,
    media_type: String,
    movie_mode: Option<String>,
    disc_scope: Option<String>,
    tmdb_show_id: Option<i64>,
    include_specials: Option<bool>,
    season_number: Option<i64>,
    episode_range_start: Option<i64>,
    episode_range_end: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize)]
struct JobSummary {
    id: String,
    disc_label: String,
    optical_drive: Option<String>,
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
    awaiting_review: Option<bool>,
}

#[derive(Debug, Serialize, Deserialize)]
struct DiscDrive {
    drive: String,
    root: String,
    has_media: bool,
    volume_label: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeConfigValidation {
    ok: bool,
    message: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeDependencyStatus {
    path: String,
    exists: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeDependencies {
    makemkv: RuntimeDependencyStatus,
    ffmpeg: RuntimeDependencyStatus,
    ffprobe: RuntimeDependencyStatus,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeMakeMkvStatus {
    level: String,
    message: String,
    details: Vec<String>,
    build_version: Option<String>,
    can_rip: Option<bool>,
    beta_key_expires_at: Option<String>,
    days_until_expiry: Option<i64>,
    checked_at: Option<String>,
    source_url: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeConfigState {
    config_path: String,
    config: Value,
    validation: RuntimeConfigValidation,
    dependencies: RuntimeDependencies,
    makemkv_status: RuntimeMakeMkvStatus,
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
            get_runtime_config_state,
            save_runtime_config,
            autodetect_runtime_config,
            browse_file_path,
            browse_directory_path,
            job_snapshot,
            list_disc_drives,
            detect_disc,
            start_pipeline,
            resume_pipeline,
            analyze_menu,
            rerun_mapping,
            rerun_identify,
            search_tmdb_candidates,
            search_tv_shows,
            get_tv_show_seasons,
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
            delete_job,
            set_window_theme,
            reclaimable_space,
            reclaim_completed,
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
fn get_runtime_config_state() -> Result<RuntimeConfigState, String> {
    let runtime = resolve_runtime_paths()?;
    let config = read_runtime_config_json(&runtime.config_path)?;
    Ok(build_runtime_config_state(&runtime.config_path, &config))
}

#[tauri::command]
fn save_runtime_config(config: Value) -> Result<RuntimeConfigState, String> {
    let runtime = resolve_runtime_paths()?;
    let object = config
        .as_object()
        .ok_or_else(|| "Config payload must be a JSON object".to_string())?;
    let pretty = serde_json::to_string_pretty(object).map_err(|e| e.to_string())?;
    fs::write(&runtime.config_path, pretty).map_err(|e| e.to_string())?;
    let refreshed = read_runtime_config_json(&runtime.config_path)?;
    Ok(build_runtime_config_state(&runtime.config_path, &refreshed))
}

#[tauri::command]
fn autodetect_runtime_config() -> Result<RuntimeConfigState, String> {
    let runtime = resolve_runtime_paths()?;
    let mut config = read_runtime_config_json(&runtime.config_path)?;
    let object = config
        .as_object_mut()
        .ok_or_else(|| "Runtime config must be a JSON object".to_string())?;

    apply_detected_path(object, "makemkv_path", detect_makemkv_path());
    apply_detected_path(object, "ffmpeg_path", detect_ffmpeg_path());
    apply_detected_path(object, "ffprobe_path", detect_ffprobe_path());

    let pretty = serde_json::to_string_pretty(object).map_err(|e| e.to_string())?;
    fs::write(&runtime.config_path, pretty).map_err(|e| e.to_string())?;
    let refreshed = read_runtime_config_json(&runtime.config_path)?;
    Ok(build_runtime_config_state(&runtime.config_path, &refreshed))
}

#[tauri::command]
fn browse_file_path(title: String, initial_path: Option<String>) -> Result<Option<String>, String> {
    browse_windows_path(title, initial_path, false)
}

#[tauri::command]
fn browse_directory_path(title: String, initial_path: Option<String>) -> Result<Option<String>, String> {
    browse_windows_path(title, initial_path, true)
}

#[tauri::command]
fn job_snapshot(job_id: String) -> Result<Value, String> {
    run_python_json(&["job", "snapshot", &job_id])
}

#[tauri::command]
fn list_disc_drives() -> Result<Vec<DiscDrive>, String> {
    let value = run_python_json(&["rip", "drives"])?;
    let drives = value
        .get("drives")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "Invalid drives payload".to_string())?;
    drives
        .iter()
        .map(|drive| serde_json::from_value::<DiscDrive>(drive.clone()).map_err(|e| e.to_string()))
        .collect()
}

#[tauri::command]
fn detect_disc(preferred_drive: Option<String>) -> Result<Option<DiscDrive>, String> {
    detect_disc_for_drive(preferred_drive)
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
    if let Some(optical_drive) = request.optical_drive {
        create_args.push("--optical-drive".into());
        create_args.push(optical_drive);
    }
    if let Some(movie_mode) = request.movie_mode {
        create_args.push("--movie-mode".into());
        create_args.push(movie_mode);
    }
    // Only meaningful for a compilation disc, and the CLI treats its absence
    // as "no", so there is nothing to push when it is false.
    if let Some(show_id) = request.tmdb_show_id {
        create_args.push("--tmdb-show-id".into());
        create_args.push(show_id.to_string());
    }
    if request.include_specials.unwrap_or(false) {
        create_args.push("--include-specials".into());
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

fn detect_disc_for_drive(preferred_drive: Option<String>) -> Result<Option<DiscDrive>, String> {
    let drives = list_disc_drives()?;
    if let Some(preferred) = preferred_drive {
        let normalized = preferred.trim().to_ascii_uppercase();
        if let Some(match_drive) = drives
            .iter()
            .find(|drive| drive.has_media && drive.drive.trim().to_ascii_uppercase() == normalized)
        {
            return Ok(Some(DiscDrive {
                drive: match_drive.drive.clone(),
                root: match_drive.root.clone(),
                has_media: match_drive.has_media,
                volume_label: match_drive.volume_label.clone(),
            }));
        }
    }
    Ok(drives.into_iter().find(|drive| drive.has_media))
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

/// Search TMDB for a show without a job existing yet.
///
/// A disc label is frequently not the show's name -- MINNIES_PET_SALON is a
/// themed compilation of Mickey Mouse Clubhouse episodes, and TMDB has no such
/// series -- so the user has to be able to look the real show up before the
/// rip starts, not after it has already gone wrong.
#[tauri::command]
fn search_tv_shows(query: String) -> Result<Value, String> {
    run_python_json(&["tmdb", "show-search", &query])
}

/// A show's seasons with episode counts, and a suggested range for this disc.
#[tauri::command]
fn get_tv_show_seasons(
    tmdb_id: i64,
    disc_number: Option<i64>,
    discs_in_set: Option<i64>,
) -> Result<Value, String> {
    let id = tmdb_id.to_string();
    let mut args = vec!["tmdb".to_string(), "show-seasons".to_string(), id];
    if let Some(disc) = disc_number {
        args.push("--disc-number".to_string());
        args.push(disc.to_string());
    }
    if let Some(total) = discs_in_set {
        args.push("--discs-in-set".to_string());
        args.push(total.to_string());
    }
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    run_python_json(&refs)
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
fn override_mapping(
    mapping_id: i64,
    episode_start: i64,
    episode_end: i64,
    season_number: Option<i64>,
) -> Result<(), String> {
    // A compilation's episodes come from all over the show, so a correction
    // there has to name its season; omitting it keeps the row where it is.
    if let Some(season) = season_number {
        let _ = run_python_text(&[
            "mapping",
            "override",
            &mapping_id.to_string(),
            &episode_start.to_string(),
            &episode_end.to_string(),
            "--season",
            &season.to_string(),
        ])?;
        return Ok(());
    }
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

#[tauri::command]
fn delete_job(job_id: String) -> Result<(), String> {
    let _ = run_python_text(&["job", "delete", &job_id])?;
    Ok(())
}

/// Match the OS-drawn window chrome to the theme the app is using.
///
/// The title bar belongs to Windows, not to the webview, so a dark UI in a
/// light title bar is the default unless the window is told otherwise.
#[tauri::command]
fn set_window_theme(window: tauri::Window, theme: String) -> Result<(), String> {
    let resolved = match theme.as_str() {
        "dark" => Some(tauri::Theme::Dark),
        "light" => Some(tauri::Theme::Light),
        // Anything else hands the decision back to the system setting.
        _ => None,
    };
    window.set_theme(resolved).map_err(|e| e.to_string())
}

#[tauri::command]
fn reclaimable_space() -> Result<Value, String> {
    run_python_json(&["job", "reclaimable"])
}

#[tauri::command]
fn reclaim_completed() -> Result<Value, String> {
    run_python_json(&["job", "reclaim-all"])
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
    // The pipeline runs detached, so this file is the only place its crash can
    // ever be seen. Discarding stderr cost a real job: the backend died
    // mid-rip, the traceback went nowhere, and the job sat in `ripping`
    // forever with an orphaned MakeMKV still spinning the disc.
    let errors = open_backend_error_log(&runtime);
    command
        .current_dir(&runtime.working_dir)
        .stdout(Stdio::null())
        .stderr(errors)
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}

/// Append-only log for detached backend crashes, next to the user's config.
///
/// Falls back to discarding output rather than failing the spawn: not being
/// able to write a log is never a good reason to refuse to start a rip.
fn open_backend_error_log(runtime: &RuntimePaths) -> Stdio {
    let Some(dir) = runtime.config_path.parent() else {
        return Stdio::null();
    };
    let path = dir.join("backend-errors.log");
    // Truncate rather than grow without bound: a backend crashing in a loop
    // should not be able to fill the user's disk.
    if let Ok(meta) = fs::metadata(&path) {
        if meta.len() > 1_000_000 {
            let _ = fs::remove_file(&path);
        }
    }
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map(Stdio::from)
        .unwrap_or(Stdio::null())
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
        #[cfg(target_os = "windows")]
        cmd.creation_flags(CREATE_NO_WINDOW);
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
        #[cfg(target_os = "windows")]
        cmd.creation_flags(CREATE_NO_WINDOW);
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
    let app_main = repo_root
        .as_ref()
        .map(|root| root.join("app").join("main.py"))
        .filter(|path| path.exists());
    let backend_exe = if cfg!(debug_assertions) && app_main.is_some() {
        None
    } else {
        resources_dir
            .as_ref()
            .and_then(|dir| resolve_bundled_backend(dir))
    };

    let config_path = resolve_user_config_path()?;
    if !config_path.exists() {
        seed_user_config(&config_path, resources_dir.as_deref(), repo_root.as_deref())?;
    }

    let working_dir = repo_root
        .clone()
        .or_else(|| resources_dir.clone())
        .or_else(|| config_path.parent().map(Path::to_path_buf))
        .ok_or_else(|| "Could not determine a working directory for backend commands".to_string())?;

    Ok(RuntimePaths {
        repo_root,
        app_main,
        backend_exe,
        config_path,
        working_dir,
    })
}

fn resolve_bundled_backend(resources_dir: &Path) -> Option<PathBuf> {
    let exe_name = if cfg!(target_os = "windows") {
        "autorippr-backend.exe"
    } else {
        "autorippr-backend"
    };
    let candidates = [
        resources_dir.join("backend").join(exe_name),
        resources_dir.join("backend").join("autorippr-backend").join(exe_name),
    ];
    candidates.into_iter().find(|path| path.exists())
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

fn read_runtime_config_json(config_path: &Path) -> Result<Value, String> {
    let raw = fs::read_to_string(config_path).map_err(|e| e.to_string())?;
    serde_json::from_str::<Value>(&raw).map_err(|e| e.to_string())
}

fn build_runtime_config_state(config_path: &Path, config: &Value) -> RuntimeConfigState {
    let validation = validate_runtime_config_value(config);
    let dependencies = RuntimeDependencies {
        makemkv: dependency_status(config, "makemkv_path"),
        ffmpeg: dependency_status(config, "ffmpeg_path"),
        ffprobe: dependency_status(config, "ffprobe_path"),
    };
    let makemkv_status = probe_runtime_makemkv_status(config);
    RuntimeConfigState {
        config_path: config_path.to_string_lossy().to_string(),
        config: config.clone(),
        validation,
        dependencies,
        makemkv_status,
    }
}

fn apply_detected_path(config: &mut serde_json::Map<String, Value>, key: &str, detected: Option<String>) {
    if let Some(path) = detected {
        let current = config.get(key).and_then(|value| value.as_str()).unwrap_or_default();
        if is_blank_or_placeholder(current) || !Path::new(current).exists() {
            config.insert(key.to_string(), Value::String(path));
        }
    }
}

fn dependency_status(config: &Value, key: &str) -> RuntimeDependencyStatus {
    let path = config
        .get(key)
        .and_then(|value| value.as_str())
        .unwrap_or_default()
        .to_string();
    let exists = !path.trim().is_empty() && Path::new(&path).exists();
    RuntimeDependencyStatus { path, exists }
}

fn probe_runtime_makemkv_status(config: &Value) -> RuntimeMakeMkvStatus {
    let path = config
        .get("makemkv_path")
        .and_then(|value| value.as_str())
        .unwrap_or_default()
        .trim()
        .to_string();
    if path.is_empty() || !Path::new(&path).exists() {
        return RuntimeMakeMkvStatus {
            level: "ok".to_string(),
            message: String::new(),
            details: Vec::new(),
            build_version: None,
            can_rip: None,
            beta_key_expires_at: None,
            days_until_expiry: None,
            checked_at: None,
            source_url: Some("https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053".to_string()),
        };
    }

    match run_python_json(&["makemkv-status"]) {
        Ok(value) => runtime_makemkv_status_from_value(&value),
        Err(err) => RuntimeMakeMkvStatus {
            level: "warning".to_string(),
            message: "Could not verify MakeMKV beta-key status.".to_string(),
            details: vec![err],
            build_version: None,
            can_rip: None,
            beta_key_expires_at: None,
            days_until_expiry: None,
            checked_at: None,
            source_url: Some("https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053".to_string()),
        },
    }
}

fn runtime_makemkv_status_from_value(value: &Value) -> RuntimeMakeMkvStatus {
    let details = value
        .get("details")
        .and_then(|entry| entry.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(ToString::to_string))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    RuntimeMakeMkvStatus {
        level: value
            .get("level")
            .and_then(|entry| entry.as_str())
            .unwrap_or("ok")
            .to_string(),
        message: value
            .get("message")
            .and_then(|entry| entry.as_str())
            .unwrap_or_default()
            .to_string(),
        details,
        build_version: value
            .get("build_version")
            .and_then(|entry| entry.as_str())
            .map(ToString::to_string),
        can_rip: value.get("can_rip").and_then(|entry| entry.as_bool()),
        beta_key_expires_at: value
            .get("beta_key_expires_at")
            .and_then(|entry| entry.as_str())
            .map(ToString::to_string),
        days_until_expiry: value.get("days_until_expiry").and_then(|entry| entry.as_i64()),
        checked_at: value
            .get("checked_at")
            .and_then(|entry| entry.as_str())
            .map(ToString::to_string),
        source_url: value
            .get("source_url")
            .and_then(|entry| entry.as_str())
            .map(ToString::to_string),
    }
}

fn detect_makemkv_path() -> Option<String> {
    let candidates = [
        r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
        r"C:\Program Files\MakeMKV\makemkvcon64.exe",
    ];
    candidates
        .iter()
        .map(PathBuf::from)
        .find(|path| path.exists())
        .map(|path| path.to_string_lossy().to_string())
        .or_else(|| detect_from_where("makemkvcon64.exe"))
}

fn detect_ffmpeg_path() -> Option<String> {
    detect_tool_path(
        "ffmpeg.exe",
        &[
            r"C:\tools\ffmpeg\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ],
    )
}

fn detect_ffprobe_path() -> Option<String> {
    detect_tool_path(
        "ffprobe.exe",
        &[
            r"C:\tools\ffmpeg\bin\ffprobe.exe",
            r"C:\ffmpeg\bin\ffprobe.exe",
            r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        ],
    )
}

fn detect_tool_path(exe_name: &str, candidates: &[&str]) -> Option<String> {
    candidates
        .iter()
        .map(PathBuf::from)
        .find(|path| path.exists())
        .map(|path| path.to_string_lossy().to_string())
        .or_else(|| detect_from_where(exe_name))
}

fn detect_from_where(exe_name: &str) -> Option<String> {
    let output = Command::new("where")
        .arg(exe_name)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    stdout
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(|line| line.to_string())
}

fn validate_runtime_config_value(config: &Value) -> RuntimeConfigValidation {
    let required = [
        "tmdb_api_key",
        "makemkv_path",
        "ffmpeg_path",
        "ffprobe_path",
        "staging_root",
        "nas_root",
    ];
    let mut missing: Vec<&str> = Vec::new();
    for key in required {
        let value = config.get(key).and_then(|candidate| candidate.as_str()).unwrap_or_default();
        if is_blank_or_placeholder(value) {
            missing.push(key);
        }
    }
    if missing.is_empty() {
        RuntimeConfigValidation {
            ok: true,
            message: "Configuration looks ready.".to_string(),
        }
    } else {
        RuntimeConfigValidation {
            ok: false,
            message: format!("Missing or placeholder config values: {}", missing.join(", ")),
        }
    }
}

fn is_blank_or_placeholder(value: &str) -> bool {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return true;
    }
    let upper = trimmed.to_ascii_uppercase();
    upper.starts_with("REPLACE_WITH_") || upper.starts_with("YOUR_")
}

fn browse_windows_path(title: String, initial_path: Option<String>, directory: bool) -> Result<Option<String>, String> {
    #[cfg(target_os = "windows")]
    {
        let safe_title = title.replace('\'', "''");
        let safe_initial = initial_path.unwrap_or_default().replace('\'', "''");
        let script = if directory {
            format!(
                "Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description = '{safe_title}'; if ('{safe_initial}' -ne '') {{ $dialog.SelectedPath = '{safe_initial}' }}; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{ $dialog.SelectedPath }}"
            )
        } else {
            format!(
                "Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.OpenFileDialog; $dialog.Title = '{safe_title}'; if ('{safe_initial}' -ne '') {{ if (Test-Path '{safe_initial}') {{ $item = Get-Item '{safe_initial}'; if ($item.PSIsContainer) {{ $dialog.InitialDirectory = '{safe_initial}' }} else {{ $dialog.InitialDirectory = Split-Path '{safe_initial}' -Parent; $dialog.FileName = Split-Path '{safe_initial}' -Leaf }} }} }}; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{ $dialog.FileName }}"
            )
        };
        let output = Command::new("powershell")
            .args(["-NoProfile", "-Command", &script])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
            .map_err(|e| e.to_string())?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            return Err(if stderr.is_empty() { "Path browse cancelled or failed".to_string() } else { stderr });
        }
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if stdout.is_empty() {
            return Ok(None);
        }
        return Ok(Some(stdout));
    }

    #[cfg(not(target_os = "windows"))]
    {
        let _ = title;
        let _ = initial_path;
        let _ = directory;
        Err("Browse dialogs are currently implemented for Windows only.".to_string())
    }
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
