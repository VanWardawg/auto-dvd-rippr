export type JobStatus =
  | "queued"
  | "ripping"
  | "identifying"
  | "mapping"
  | "splitting"
  | "renaming"
  | "copying"
  | "done"
  | "error";

export interface JobSummary {
  id: string;
  disc_label: string;
  optical_drive?: string | null;
  media_type: "tv" | "movie";
  movie_mode?: "single" | "double_feature" | "trilogy";
  has_local_artifacts?: boolean;
  disc_scope?: "full_season" | "partial_season" | "special" | "custom" | null;
  season_number?: number | null;
  episode_range_start?: number | null;
  episode_range_end?: number | null;
  status: JobStatus;
  current_stage?: string | null;
  updated_at: string;
  error_message?: string | null;
}

export interface DiscDrive {
  drive: string;
  root: string;
  has_media: boolean;
  volume_label: string;
}

export interface EpisodeMapping {
  id: number;
  rip_title_id?: number | null;
  title_id?: number | null;
  source_file?: string | null;
  episode_start?: number | null;
  episode_end?: number | null;
  tmdb_episode_ids_json?: string;
  episode_titles_json?: string;
  confidence?: number | null;
  reason?: string | null;
  manual_override?: number;
  needs_split?: number;
}

export interface SplitPlan {
  id: number;
  mapping_id: number;
  source_file?: string | null;
  segment_index: number;
  start_seconds?: number | null;
  end_seconds?: number | null;
  chapter_start?: number | null;
  chapter_end?: number | null;
  output_file?: string | null;
  status: string;
  error_message?: string | null;
}

export interface OutputFile {
  id: number;
  local_path: string;
  nas_path?: string | null;
  transfer_status: string;
  transfer_attempts?: number;
  last_error?: string | null;
}

export interface RipTitle {
  id: number;
  title_id?: number | null;
  duration_seconds?: number | null;
  chapter_count?: number | null;
  source_file: string;
  raw_metadata_json?: string | null;
}

export interface SelectedMedia {
  media_type: string;
  tmdb_id: number;
  title: string;
  year?: number | null;
  season_number?: number | null;
  order_mode?: string | null;
}

export interface SelectedMovieSlot {
  slot_index: number;
  tmdb_id: number;
  title: string;
  year?: number | null;
  rip_title_id?: number | null;
  created_at?: string;
  updated_at?: string;
}

export interface TmdbCandidate {
  tmdb_id: number;
  media_type: "tv" | "movie";
  title: string;
  year?: number | null;
  score: number;
  score_breakdown_json?: string;
  selected: number;
  manual_override: number;
}

export interface ReviewLane {
  needed: boolean;
  reason?: string | null;
  threshold: number;
  details?: string[];
  candidate_count?: number;
  top_candidate?: TmdbCandidate | null;
  required_slots?: number;
  selected_slots?: number;
  low_confidence_count?: number;
  low_confidence_mappings?: EpisodeMapping[];
  bundle_confidence_gate?: Record<string, unknown> | null;
}

export interface ReviewState {
  rip: ReviewLane;
  tmdb: ReviewLane;
  mapping: ReviewLane;
}

export interface ProgressState {
  overall_fraction: number;
  stage_fraction: number;
  detail?: string | null;
  eta_seconds?: number | null;
  rate_mb_s?: number | null;
  current_mb?: number | null;
  total_mb?: number | null;
  kind?: string | null;
}

export interface JobLog {
  id?: number;
  timestamp: string;
  level: string;
  message: string;
  from_status?: string | null;
  to_status?: string | null;
}

export interface SeasonEpisodeOption {
  id: number;
  episode_number: number;
  name: string;
  runtime?: number | null;
}

export interface JobSnapshot {
  job: JobSummary;
  logs: JobLog[];
  selected_media?: SelectedMedia | null;
  selected_movies?: SelectedMovieSlot[]; 
  tmdb_candidates: TmdbCandidate[];
  episode_mappings: EpisodeMapping[];
  split_plans: SplitPlan[];
  outputs: OutputFile[];
  rip_titles: RipTitle[];
  season_episodes: SeasonEpisodeOption[];
  all_season_episodes?: SeasonEpisodeOption[];
  menu_analysis?: Record<string, unknown> | null;
  bundle_association?: Record<string, unknown> | null;
  dvdnav_menu?: Record<string, unknown> | null;
  review_state?: ReviewState | null;
  progress_state?: ProgressState | null;
}

export interface StartJobRequest {
  discLabel: string;
  opticalDrive?: string | null;
  mediaType: "tv" | "movie";
  movieMode?: "single" | "double_feature" | "trilogy";
  discScope?: "full_season" | "partial_season" | "special" | "custom";
  seasonNumber?: number | null;
  episodeRangeStart?: number | null;
  episodeRangeEnd?: number | null;
}

export interface RuntimeDependencyStatus {
  path: string;
  exists: boolean;
}

export interface RuntimeConfigValidation {
  ok: boolean;
  message: string;
}

export interface RuntimeMakeMkvStatus {
  level: "ok" | "warning" | "error";
  message: string;
  details: string[];
  buildVersion?: string | null;
  canRip?: boolean | null;
  betaKeyExpiresAt?: string | null;
  daysUntilExpiry?: number | null;
  checkedAt?: string | null;
  sourceUrl?: string | null;
}

export interface RuntimeConfigState {
  configPath: string;
  config: Record<string, unknown>;
  validation: RuntimeConfigValidation;
  dependencies: {
    makemkv: RuntimeDependencyStatus;
    ffmpeg: RuntimeDependencyStatus;
    ffprobe: RuntimeDependencyStatus;
  };
  makemkvStatus: RuntimeMakeMkvStatus;
}
