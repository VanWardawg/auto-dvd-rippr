import { invoke } from "@tauri-apps/api/core";
import type { DiscDrive, JobSnapshot, JobSummary, StartJobRequest } from "./types";

export async function listJobs(): Promise<JobSummary[]> {
  return invoke<JobSummary[]>("list_jobs");
}

export async function getJobSnapshot(jobId: string): Promise<JobSnapshot> {
  return invoke<JobSnapshot>("job_snapshot", { jobId });
}

export async function detectDisc(): Promise<DiscDrive | null> {
  return invoke<DiscDrive | null>("detect_disc");
}

export async function startPipeline(request: StartJobRequest): Promise<string> {
  return invoke<string>("start_pipeline", { request });
}

export async function resumePipeline(jobId: string): Promise<void> {
  return invoke("resume_pipeline", { jobId });
}

export async function analyzeMenu(jobId: string): Promise<void> {
  return invoke("analyze_menu", { jobId });
}

export async function rerunMapping(jobId: string): Promise<void> {
  return invoke("rerun_mapping", { jobId });
}

export async function rerunIdentify(jobId: string): Promise<void> {
  return invoke("rerun_identify", { jobId });
}

export async function searchTmdbCandidates(jobId: string, query: string): Promise<void> {
  return invoke("search_tmdb_candidates", { jobId, query });
}

export async function selectTmdbCandidate(jobId: string, mediaType: "tv" | "movie", tmdbId: number): Promise<void> {
  return invoke("select_tmdb_candidate", { jobId, mediaType, tmdbId });
}

export async function selectMovieSlotCandidate(
  jobId: string,
  slotIndex: number,
  tmdbId: number,
): Promise<void> {
  return invoke("select_tmdb_candidate", { jobId, mediaType: "movie", tmdbId, slotIndex });
}

export async function overrideMapping(mappingId: number, episodeStart: number, episodeEnd: number): Promise<void> {
  return invoke("override_mapping", { mappingId, episodeStart, episodeEnd });
}

export async function overrideMappingSource(mappingId: number, ripTitleId: number): Promise<void> {
  return invoke("override_mapping_source", { mappingId, ripTitleId });
}

export async function ignoreMapping(mappingId: number): Promise<void> {
  return invoke("ignore_mapping", { mappingId });
}

export async function overrideSplit(splitPlanId: number, start?: number | null, end?: number | null): Promise<void> {
  return invoke("override_split", { splitPlanId, start, end });
}

export async function planSplits(jobId: string): Promise<void> {
  return invoke("plan_splits", { jobId });
}

export async function updateJobProfile(
  jobId: string,
  discScope: "full_season" | "partial_season" | "special" | "custom",
  seasonNumber?: number | null,
  episodeRangeStart?: number | null,
  episodeRangeEnd?: number | null,
): Promise<void> {
  return invoke("update_job_profile", { jobId, discScope, seasonNumber, episodeRangeStart, episodeRangeEnd });
}

export async function deleteJob(jobId: string): Promise<void> {
  return invoke("delete_job", { jobId });
}

export async function cancelJob(jobId: string): Promise<void> {
  return invoke("cancel_job", { jobId });
}

export async function clearLocalArtifacts(jobId: string): Promise<void> {
  return invoke("clear_local_artifacts", { jobId });
}

export async function rebuildOutput(jobId: string): Promise<void> {
  return invoke("rebuild_output", { jobId });
}

export async function remapRemoteOutput(jobId: string): Promise<void> {
  return invoke("remap_remote_output", { jobId });
}

export async function openPath(path: string): Promise<void> {
  return invoke("open_path", { path });
}
