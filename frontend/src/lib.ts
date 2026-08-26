/**
 * Pure presentation helpers.
 *
 * These format values for display and derive small pieces of view state. They
 * hold no React state and touch no Tauri commands, which makes them the part
 * of the frontend worth unit testing -- and keeps them out of a component file
 * that had grown past 2,900 lines.
 */
import type {
  EpisodeMapping,
  JobLog,
  JobSnapshot,
  JobStatus,
  RipTitle,
  SelectedMedia,
  SelectedMovieSlot,
  SplitPlan,
  StartJobRequest,
  TmdbCandidate,
} from "./types";

export const TV_PIPELINE_STAGES: JobStatus[] = ["queued", "identifying", "ripping", "mapping", "splitting", "renaming", "copying", "done"];
export const MOVIE_PIPELINE_STAGES: JobStatus[] = ["queued", "ripping", "identifying", "renaming", "copying", "done"];

export type GuidedReviewRowDraft = {
  mappingId: string;
  ripTitleId: string;
  sourceFile: string;
  status: "map" | "ignore";
  episodeStart: string;
  episodeEnd: string;
  durationMinutes: string;
  chapterCount: string;
  confidence: string;
  reason: string;
};

export type GuidedSplitDraft = {
  splitPlanId: string;
  sourceFile: string;
  segmentIndex: number;
  startSeconds: string;
  endSeconds: string;
};

export function formatBytes(bytes?: number | null) {
  if (bytes === null || bytes === undefined || bytes <= 0) return null;
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
  return `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`;
}

export function parseTitles(value?: string) {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [value];
  }
}

export function episodeLabel(mapping: EpisodeMapping) {
  if (mapping.episode_start == null) return "Excluded";
  if (mapping.episode_end == null || mapping.episode_end === mapping.episode_start) {
    return `E${String(mapping.episode_start).padStart(2, "0")}`;
  }
  return `E${String(mapping.episode_start).padStart(2, "0")}-${String(mapping.episode_end).padStart(2, "0")}`;
}

export function fileNameFromPath(path?: string | null) {
  return path ? path.split(/[\\/]/).pop() ?? path : "—";
}

export function mappingDisplay(mapping: EpisodeMapping) {
  const titles = parseTitles(mapping.episode_titles_json).join(" / ") || "No titles";
  return `${mapping.id} • ${fileNameFromPath(mapping.source_file)} • ${episodeLabel(mapping)} • ${titles}`;
}

export function ripTitleDisplay(ripTitle: RipTitle) {
  const mins = ripTitle.duration_seconds ? `${(ripTitle.duration_seconds / 60).toFixed(1)}m` : "?m";
  return `${ripTitle.id} • ${fileNameFromPath(ripTitle.source_file)} • ${mins}`;
}

export function buildGuidedReviewRows(ripTitles: RipTitle[], mappings: EpisodeMapping[]): GuidedReviewRowDraft[] {
  return ripTitles.map((ripTitle) => {
    const mapping = mappings.find((candidate) => candidate.rip_title_id === ripTitle.id) ?? null;
    return {
      mappingId: mapping ? String(mapping.id) : "",
      ripTitleId: String(ripTitle.id),
      sourceFile: ripTitle.source_file,
      status: mapping?.episode_start == null ? "ignore" : "map",
      episodeStart: mapping?.episode_start != null ? String(mapping.episode_start) : "",
      episodeEnd: mapping?.episode_end != null ? String(mapping.episode_end) : "",
      durationMinutes: ripTitle.duration_seconds ? (ripTitle.duration_seconds / 60).toFixed(1) : "—",
      chapterCount: ripTitle.chapter_count != null ? String(ripTitle.chapter_count) : "—",
      confidence: mapping?.confidence != null ? mapping.confidence.toFixed(2) : "—",
      reason: mapping?.reason ?? "",
    };
  });
}

export function buildGuidedSplitDrafts(plans: SplitPlan[]): GuidedSplitDraft[] {
  return plans.map((plan) => ({
    splitPlanId: String(plan.id),
    sourceFile: plan.source_file ?? "",
    segmentIndex: plan.segment_index,
    startSeconds: plan.start_seconds != null ? String(plan.start_seconds) : "",
    endSeconds: plan.end_seconds != null ? String(plan.end_seconds) : "",
  }));
}

export function parseScoreBreakdown(value?: string) {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>;
    return parsed;
  } catch {
    return null;
  }
}

export function tmdbCandidateDisplay(candidate: TmdbCandidate) {
  const year = candidate.year ?? "—";
  return `${candidate.title} (${year}) • ${candidate.media_type.toUpperCase()} • ${(candidate.score * 100).toFixed(1)}%`;
}

export function movieSlotCount(movieMode?: "single" | "double_feature" | "trilogy" | null) {
  if (movieMode === "double_feature") return 2;
  if (movieMode === "trilogy") return 3;
  return 1;
}

export function formatRelativeTime(value?: string | null) {
  if (!value) return "—";
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  const deltaSeconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
  if (deltaSeconds < 10) return "just now";
  if (deltaSeconds < 60) return `${deltaSeconds}s ago`;
  const minutes = Math.floor(deltaSeconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function formatEta(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  const total = Math.max(0, Math.round(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins <= 0) return `${secs}s left`;
  if (mins < 60) return `${mins}m ${secs}s left`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins % 60}m left`;
}

export function formatDurationSince(value?: string | null) {
  if (!value) return null;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return null;
  const deltaSeconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
  const mins = Math.floor(deltaSeconds / 60);
  const secs = deltaSeconds % 60;
  if (mins <= 0) return `${secs}s in stage`;
  if (mins < 60) return `${mins}m ${secs}s in stage`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins % 60}m in stage`;
}

export function formatCompletionSince(value?: string | null) {
  if (!value) return null;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return null;
  const deltaSeconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
  const mins = Math.floor(deltaSeconds / 60);
  const secs = deltaSeconds % 60;
  if (mins <= 0) return `Completed ${secs}s ago`;
  if (mins < 60) return `Completed ${mins}m ${secs}s ago`;
  const hours = Math.floor(mins / 60);
  return `Completed ${hours}h ${mins % 60}m ago`;
}

export function formatCalendarDate(value?: string | null) {
  if (!value) return null;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Date(parsed).toLocaleDateString();
}

export function getPipelineStages(mediaType?: "tv" | "movie" | null) {
  return mediaType === "movie" ? MOVIE_PIPELINE_STAGES : TV_PIPELINE_STAGES;
}

export function getProgressPercent(status: JobStatus, stages: JobStatus[]) {
  if (status === "error") return 100;
  const index = stages.indexOf(status);
  if (index < 0) return 0;
  return Math.round((index / (stages.length - 1)) * 100);
}

export function getPipelineStageIndex(status: JobStatus, stages: JobStatus[], currentStage?: string | null) {
  const current = stages.indexOf((currentStage as JobStatus) ?? status);
  if (current >= 0) return current;
  const direct = stages.indexOf(status);
  return direct >= 0 ? direct : 0;
}

export function getActivityState(status: JobStatus, reviewNeeded: boolean, lastActivityAt?: string | null) {
  if (reviewNeeded) return "Waiting for review";
  if (status === "done") return "Completed";
  if (status === "error") return "Needs attention";
  const parsed = lastActivityAt ? Date.parse(lastActivityAt) : Number.NaN;
  if (Number.isNaN(parsed)) return "Monitoring";
  const ageSeconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
  if (ageSeconds <= 20) return "Active now";
  if (ageSeconds <= 120) return "Working";
  return `Quiet for ${formatRelativeTime(lastActivityAt)}`;
}

export function isLogWarning(log: JobLog) {
  return log.level === "WARNING" || log.level === "ERROR";
}

export function getHeroSubtitle(
  snapshot: JobSnapshot,
  isMultiMovie: boolean,
  selectedMovies: SelectedMovieSlot[],
): string | null {
  if (isMultiMovie) {
    if (selectedMovies.length > 0) {
      return selectedMovies.map((slot) => `${slot.slot_index}. ${slot.title}`).join(" • ");
    }
    return ["queued", "ripping"].includes(snapshot.job.status) ? null : "No selected movie slots yet";
  }
  if (snapshot.selected_media?.title) {
    return snapshot.selected_media.title;
  }
  return ["queued", "ripping"].includes(snapshot.job.status) ? null : "No selected TMDB media yet";
}

export function normalizeStartRequest(form: StartJobRequest): StartJobRequest {
  if (form.mediaType === "movie") {
    return {
      discLabel: form.discLabel.trim(),
      opticalDrive: form.opticalDrive ?? null,
      mediaType: form.mediaType,
      movieMode: form.movieMode,
    };
  }
  if (form.discScope === "partial_season") {
    return {
      discLabel: form.discLabel.trim(),
      opticalDrive: form.opticalDrive ?? null,
      mediaType: form.mediaType,
      discScope: form.discScope,
      seasonNumber: form.seasonNumber ?? null,
      episodeRangeStart: form.episodeRangeStart ?? null,
      episodeRangeEnd: form.episodeRangeEnd ?? null,
    };
  }
  return {
    discLabel: form.discLabel.trim(),
    opticalDrive: form.opticalDrive ?? null,
    mediaType: form.mediaType,
    discScope: form.discScope,
    seasonNumber: form.seasonNumber ?? null,
    episodeRangeStart: null,
    episodeRangeEnd: null,
  };
}

export function buildConfigDraft(config?: Record<string, unknown> | null) {
  const source = config ?? {};
  return {
    tmdb_api_key: String(source.tmdb_api_key ?? ""),
    makemkv_path: String(source.makemkv_path ?? ""),
    ffmpeg_path: String(source.ffmpeg_path ?? ""),
    ffprobe_path: String(source.ffprobe_path ?? ""),
    staging_root: String(source.staging_root ?? ""),
    nas_root: String(source.nas_root ?? ""),
    default_order_mode: String(source.default_order_mode ?? "aired"),
    collision_policy: String(source.collision_policy ?? "skip"),
    rip_title_selection: String(source.rip_title_selection ?? "auto"),
    eject_after_rip: String(source.eject_after_rip ?? false),
    verify_transfers: String(source.verify_transfers ?? false),
    clear_local_after_transfer: String(source.clear_local_after_transfer ?? false),
  };
}

/**
 * Config keys the backend expects as real booleans.
 *
 * The settings form keeps every field as a string, so these must be converted
 * back before saving -- otherwise the JSON stores "false", which is truthy on
 * the Python side.
 */
export const BOOLEAN_CONFIG_KEYS = ["eject_after_rip", "verify_transfers", "clear_local_after_transfer"];

export function coerceConfigDraft(draft: Record<string, string>): Record<string, unknown> {
  const coerced: Record<string, unknown> = { ...draft };
  for (const key of BOOLEAN_CONFIG_KEYS) {
    if (key in coerced) {
      coerced[key] = String(coerced[key]) === "true";
    }
  }
  return coerced;
}
