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
  if (form.discScope === "compilation") {
    // A compilation holds episodes from across the show, so there is no season
    // and no range to send -- only whether the specials are in scope.
    return {
      discLabel: form.discLabel.trim(),
      opticalDrive: form.opticalDrive ?? null,
      mediaType: form.mediaType,
      discScope: form.discScope,
      includeSpecials: form.includeSpecials ?? false,
      tmdbShowId: form.tmdbShowId ?? null,
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
      tmdbShowId: form.tmdbShowId ?? null,
    };
  }
  return {
    discLabel: form.discLabel.trim(),
    opticalDrive: form.opticalDrive ?? null,
    mediaType: form.mediaType,
    discScope: form.discScope,
    tmdbShowId: form.tmdbShowId ?? null,
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

export type DriveCardState = {
  id: string;
  form: StartJobRequest;
  continuousMode: boolean;
  continuousStatus: string | null;
};

export const DRIVE_CARDS_STORAGE_KEY = "autorippr-drive-cards";

export function createCardId() {
  return globalThis.crypto?.randomUUID?.() ?? `drive-card-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function buildDefaultStartJobRequest(): StartJobRequest {
  return {
    discLabel: "",
    opticalDrive: null,
    mediaType: "movie",
    movieMode: "single",
    discScope: "full_season",
    seasonNumber: 1,
    episodeRangeStart: 1,
    episodeRangeEnd: 10,
  };
}

export function buildDriveCardState(): DriveCardState {
  return {
    id: createCardId(),
    form: buildDefaultStartJobRequest(),
    continuousMode: false,
    continuousStatus: null,
  };
}

/**
 * Reduce a drive card to the part worth remembering between sessions.
 *
 * Deliberately excludes the disc label, which belongs to whatever disc is in
 * the drive right now, and continuous mode, which stays opt-in per session --
 * restoring it would mean an app launched weeks later starts ripping whatever
 * disc happens to be sitting in the tray.
 */
export function serializeDriveCards(cards: DriveCardState[]): string {
  return JSON.stringify(
    cards.map((card) => ({
      id: card.id,
      form: {
        opticalDrive: card.form.opticalDrive ?? null,
        mediaType: card.form.mediaType,
        movieMode: card.form.movieMode,
        discScope: card.form.discScope,
        seasonNumber: card.form.seasonNumber ?? null,
        episodeRangeStart: card.form.episodeRangeStart ?? null,
        episodeRangeEnd: card.form.episodeRangeEnd ?? null,
      },
    })),
  );
}

/**
 * Rebuild drive cards from storage, falling back to a single default card.
 *
 * Anything unrecognised is discarded rather than trusted: this is parsing
 * data written by an older version of the app, and a malformed entry must not
 * leave the user with a blank sidebar.
 */
export function deserializeDriveCards(raw: string | null): DriveCardState[] {
  if (!raw) return [buildDriveCardState()];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [buildDriveCardState()];
  }
  if (!Array.isArray(parsed) || parsed.length === 0) return [buildDriveCardState()];

  const defaults = buildDefaultStartJobRequest();
  const cards: DriveCardState[] = [];
  for (const entry of parsed) {
    if (!entry || typeof entry !== "object") continue;
    const form = (entry as { form?: Partial<StartJobRequest> }).form ?? {};
    const mediaType = form.mediaType === "tv" ? "tv" : "movie";
    cards.push({
      id: typeof (entry as { id?: unknown }).id === "string" ? (entry as { id: string }).id : createCardId(),
      form: {
        ...defaults,
        // The label always comes from the disc currently in the drive.
        discLabel: "",
        opticalDrive: typeof form.opticalDrive === "string" ? form.opticalDrive : null,
        mediaType,
        movieMode: form.movieMode ?? defaults.movieMode,
        discScope: form.discScope ?? defaults.discScope,
        seasonNumber: typeof form.seasonNumber === "number" ? form.seasonNumber : defaults.seasonNumber,
        episodeRangeStart:
          typeof form.episodeRangeStart === "number" ? form.episodeRangeStart : defaults.episodeRangeStart,
        episodeRangeEnd:
          typeof form.episodeRangeEnd === "number" ? form.episodeRangeEnd : defaults.episodeRangeEnd,
      },
      continuousMode: false,
      continuousStatus: null,
    });
  }
  return cards.length ? cards : [buildDriveCardState()];
}

/**
 * Turn a disc's volume label into something worth searching TMDB for.
 *
 * Mirrors the backend's cleaning so the lookup box is prefilled with the same
 * query the pipeline would use. A label carries the pressing's concerns --
 * aspect ratio, edition, which disc of the set -- and none of those belong in
 * a search for the show's name.
 */
export function showQueryFromLabel(label: string | null | undefined): string {
  if (!label) return "";
  return label
    .toLowerCase()
    .replace(/[_\-.]+/g, " ")
    .replace(/\b(disc|disk|dvd|vol|volume|season|ep|episode)\b/g, " ")
    .replace(/\bs\d{1,2}\s?e\d{1,3}\b/g, " ")
    .replace(/\b[sd]\s?\d{1,2}\b/g, " ")
    .replace(/\b(4\s?x\s?3|16\s?x\s?9|ws|fs|ntsc|pal|se|ce|dts|ac3|thx)\b/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Season number named by the label, if it names one. */
export function seasonFromLabel(label: string | null | undefined): number | null {
  if (!label) return null;
  const flat = label.replace(/[_\-.]+/g, " ");
  const match =
    /\bseason\s*(\d{1,2})\b/i.exec(flat) ??
    /\bs(\d{1,2})\s?e\d{1,3}\b/i.exec(flat) ??
    /\bs\s?(\d{1,2})\b/i.exec(flat);
  return match ? Number(match[1]) : null;
}

/** Which disc of a boxed set the label says this is. */
export function discNumberFromLabel(label: string | null | undefined): number | null {
  if (!label) return null;
  const flat = label.replace(/[_\-.]+/g, " ");
  const match = /\bdis[ck]\s*(\d{1,2})\b/i.exec(flat) ?? /\bd\s?(\d{1,2})\b/i.exec(flat);
  return match ? Number(match[1]) : null;
}

/**
 * Which episodes disc N of a boxed set probably holds.
 *
 * Mirrors `suggest_episode_range` in tmdb.py so the card can prefill without a
 * round-trip. Studios cut a season evenly across its discs, with the remainder
 * falling to the earlier ones. Returns null rather than a guess when the
 * inputs cannot support one -- a wrong prefill is worse than an empty box.
 */
export function suggestEpisodeRange(
  episodeCount: number,
  discNumber: number | null | undefined,
  discsInSet: number | null | undefined,
): { start: number; end: number } | null {
  if (!episodeCount || !discNumber || !discsInSet) return null;
  if (discNumber < 1 || discsInSet < 1 || discNumber > discsInSet) return null;

  const perDisc = Math.floor(episodeCount / discsInSet);
  const remainder = episodeCount % discsInSet;
  if (perDisc < 1) return null;

  let start = 1;
  for (let index = 1; index < discNumber; index += 1) {
    start += perDisc + (index <= remainder ? 1 : 0);
  }
  const end = start + perDisc + (discNumber <= remainder ? 1 : 0) - 1;
  return { start, end: Math.min(end, episodeCount) };
}
