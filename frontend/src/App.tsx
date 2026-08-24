import { useEffect, useMemo, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import {
  detectDisc,
  analyzeMenu,
  autodetectRuntimeConfig,
  browseDirectoryPath,
  browseFilePath,
  cancelJob,
  clearLocalArtifacts,
  listDiscDrives,
  deleteJob,
  getRuntimeConfigState,
  getJobSnapshot,
  listJobs,
  openPath,
  planSplits,
  remapRemoteOutput,
  rebuildOutput,
  searchTmdbCandidates,
  selectMovieSlotCandidate,
  selectTmdbCandidate,
  updateJobProfile,
  ignoreMapping,
  overrideMapping,
  overrideMappingSource,
  overrideSplit,
  rerunIdentify,
  rerunMapping,
  resumePipeline,
  saveRuntimeConfig,
  startPipeline,
} from "./api";
import type { DiscDrive, EpisodeMapping, JobLog, JobSnapshot, JobStatus, JobSummary, RipTitle, RuntimeConfigState, SelectedMovieSlot, SplitPlan, StartJobRequest, TmdbCandidate } from "./types";

const POLL_MS = 3000;
const TV_PIPELINE_STAGES: JobStatus[] = ["queued", "identifying", "ripping", "mapping", "splitting", "renaming", "copying", "done"];
const MOVIE_PIPELINE_STAGES: JobStatus[] = ["queued", "ripping", "identifying", "renaming", "copying", "done"];

type QuickAction = {
  key: string;
  label: string;
  onClick: () => void;
  disabled: boolean;
  tone?: "default" | "danger";
};

type GuidedReviewRowDraft = {
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

type GuidedSplitDraft = {
  splitPlanId: string;
  sourceFile: string;
  segmentIndex: number;
  startSeconds: string;
  endSeconds: string;
};

type DriveCardState = {
  id: string;
  form: StartJobRequest;
  continuousMode: boolean;
  continuousStatus: string | null;
};

function parseTitles(value?: string) {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [value];
  }
}

function episodeLabel(mapping: EpisodeMapping) {
  if (mapping.episode_start == null) return "Excluded";
  if (mapping.episode_end == null || mapping.episode_end === mapping.episode_start) {
    return `E${String(mapping.episode_start).padStart(2, "0")}`;
  }
  return `E${String(mapping.episode_start).padStart(2, "0")}-${String(mapping.episode_end).padStart(2, "0")}`;
}

function fileNameFromPath(path?: string | null) {
  return path ? path.split(/[\\/]/).pop() ?? path : "—";
}

function mappingDisplay(mapping: EpisodeMapping) {
  const titles = parseTitles(mapping.episode_titles_json).join(" / ") || "No titles";
  return `${mapping.id} • ${fileNameFromPath(mapping.source_file)} • ${episodeLabel(mapping)} • ${titles}`;
}

function ripTitleDisplay(ripTitle: RipTitle) {
  const mins = ripTitle.duration_seconds ? `${(ripTitle.duration_seconds / 60).toFixed(1)}m` : "?m";
  return `${ripTitle.id} • ${fileNameFromPath(ripTitle.source_file)} • ${mins}`;
}

function buildGuidedReviewRows(ripTitles: RipTitle[], mappings: EpisodeMapping[]): GuidedReviewRowDraft[] {
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

function buildGuidedSplitDrafts(plans: SplitPlan[]): GuidedSplitDraft[] {
  return plans.map((plan) => ({
    splitPlanId: String(plan.id),
    sourceFile: plan.source_file ?? "",
    segmentIndex: plan.segment_index,
    startSeconds: plan.start_seconds != null ? String(plan.start_seconds) : "",
    endSeconds: plan.end_seconds != null ? String(plan.end_seconds) : "",
  }));
}

function parseScoreBreakdown(value?: string) {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>;
    return parsed;
  } catch {
    return null;
  }
}

function tmdbCandidateDisplay(candidate: TmdbCandidate) {
  const year = candidate.year ?? "—";
  return `${candidate.title} (${year}) • ${candidate.media_type.toUpperCase()} • ${(candidate.score * 100).toFixed(1)}%`;
}

function movieSlotCount(movieMode?: "single" | "double_feature" | "trilogy" | null) {
  if (movieMode === "double_feature") return 2;
  if (movieMode === "trilogy") return 3;
  return 1;
}

function formatRelativeTime(value?: string | null) {
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

function formatEta(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  const total = Math.max(0, Math.round(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins <= 0) return `${secs}s left`;
  if (mins < 60) return `${mins}m ${secs}s left`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins % 60}m left`;
}

function formatDurationSince(value?: string | null) {
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

function formatCompletionSince(value?: string | null) {
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

function formatCalendarDate(value?: string | null) {
  if (!value) return null;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Date(parsed).toLocaleDateString();
}

function getPipelineStages(mediaType?: "tv" | "movie" | null) {
  return mediaType === "movie" ? MOVIE_PIPELINE_STAGES : TV_PIPELINE_STAGES;
}

function getProgressPercent(status: JobStatus, stages: JobStatus[]) {
  if (status === "error") return 100;
  const index = stages.indexOf(status);
  if (index < 0) return 0;
  return Math.round((index / (stages.length - 1)) * 100);
}

function getPipelineStageIndex(status: JobStatus, stages: JobStatus[], currentStage?: string | null) {
  const current = stages.indexOf((currentStage as JobStatus) ?? status);
  if (current >= 0) return current;
  const direct = stages.indexOf(status);
  return direct >= 0 ? direct : 0;
}

function getActivityState(status: JobStatus, reviewNeeded: boolean, lastActivityAt?: string | null) {
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

function isLogWarning(log: JobLog) {
  return log.level === "WARNING" || log.level === "ERROR";
}

function getHeroSubtitle(
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

function normalizeStartRequest(form: StartJobRequest): StartJobRequest {
  if (form.mediaType === "movie") {
    return {
      discLabel: form.discLabel,
      opticalDrive: form.opticalDrive ?? null,
      mediaType: form.mediaType,
      movieMode: form.movieMode,
    };
  }
  if (form.discScope === "partial_season") {
    return {
      discLabel: form.discLabel,
      opticalDrive: form.opticalDrive ?? null,
      mediaType: form.mediaType,
      discScope: form.discScope,
      seasonNumber: form.seasonNumber ?? null,
      episodeRangeStart: form.episodeRangeStart ?? null,
      episodeRangeEnd: form.episodeRangeEnd ?? null,
    };
  }
  return {
    discLabel: form.discLabel,
    opticalDrive: form.opticalDrive ?? null,
    mediaType: form.mediaType,
    discScope: form.discScope,
    seasonNumber: form.seasonNumber ?? null,
    episodeRangeStart: null,
    episodeRangeEnd: null,
  };
}

function buildConfigDraft(config?: Record<string, unknown> | null) {
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
  };
}

/**
 * Config keys the backend expects as real booleans.
 *
 * The settings form keeps every field as a string, so these must be converted
 * back before saving -- otherwise the JSON stores "false", which is truthy on
 * the Python side.
 */
const BOOLEAN_CONFIG_KEYS = ["eject_after_rip", "verify_transfers"];

function coerceConfigDraft(draft: Record<string, string>): Record<string, unknown> {
  const coerced: Record<string, unknown> = { ...draft };
  for (const key of BOOLEAN_CONFIG_KEYS) {
    if (key in coerced) {
      coerced[key] = String(coerced[key]) === "true";
    }
  }
  return coerced;
}

function createCardId() {
  return globalThis.crypto?.randomUUID?.() ?? `drive-card-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function buildDefaultStartJobRequest(): StartJobRequest {
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

function buildDriveCardState(): DriveCardState {
  return {
    id: createCardId(),
    form: buildDefaultStartJobRequest(),
    continuousMode: false,
    continuousStatus: null,
  };
}

export default function App() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<JobSnapshot | null>(null);
  const [activeTab, setActiveTab] = useState<"overview" | "activity" | "artifacts" | "json" | "settings">("overview");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [configState, setConfigState] = useState<RuntimeConfigState | null>(null);
  const [configDraft, setConfigDraft] = useState<Record<string, string>>(buildConfigDraft());
  const [configLoading, setConfigLoading] = useState(true);
  const [configReady, setConfigReady] = useState(false);
  const [discDrives, setDiscDrives] = useState<DiscDrive[]>([]);
  const [driveCards, setDriveCards] = useState<DriveCardState[]>([buildDriveCardState()]);
  const [modal, setModal] = useState<null | "tmdb" | "map" | "file" | "split" | "review">(null);
  const [modalJobId, setModalJobId] = useState<string | null>(null);
  const [modalValues, setModalValues] = useState<Record<string, string>>({});
  const [guidedReviewRows, setGuidedReviewRows] = useState<GuidedReviewRowDraft[]>([]);
  const [guidedSplitDrafts, setGuidedSplitDrafts] = useState<GuidedSplitDraft[]>([]);
  const [actionsMenuOpen, setActionsMenuOpen] = useState(false);
  const selectedJobIdRef = useRef<string | null>(null);
  const jobsRef = useRef<JobSummary[]>([]);
  const driveCardsRef = useRef<DriveCardState[]>(driveCards);
  const busyActionRef = useRef<string | null>(null);
  const lastAutoStartedDiscRef = useRef<Record<string, string | null>>({});
  const autoStartInFlightRef = useRef<Record<string, string | null>>({});

  async function refreshJobs() {
    try {
      const nextJobs = await listJobs();
      setJobs(nextJobs);
      setError(null);
      const currentSelected = selectedJobIdRef.current;
      if (!currentSelected && nextJobs.length > 0) {
        setSelectedJobId(nextJobs[0].id);
      } else if (currentSelected && !nextJobs.some((j) => j.id === currentSelected) && nextJobs.length > 0) {
        setSelectedJobId(nextJobs[0].id);
      }
    } catch (err) {
      setError(String(err));
    }
  }

  async function refreshSnapshot(jobId: string) {
    try {
      const next = await getJobSnapshot(jobId);
      setSnapshot(next);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }

  async function loadConfigState() {
    try {
      const next = await getRuntimeConfigState();
      setConfigState(next);
      setConfigDraft(buildConfigDraft(next.config));
      setConfigReady(next.validation.ok);
      if (!next.validation.ok) {
        setActiveTab("settings");
      }
      setError(null);
    } catch (err) {
      setConfigReady(false);
      setError(String(err));
    } finally {
      setConfigLoading(false);
    }
  }

  async function saveConfigFromDraft() {
    await runAction("Saving settings", async () => {
      const next = await saveRuntimeConfig({
        ...(configState?.config ?? {}),
        ...coerceConfigDraft(configDraft),
      });
      setConfigState(next);
      setConfigReady(next.validation.ok);
      if (next.validation.ok) {
        setActiveTab("overview");
        await refreshJobs();
        await refreshAllDriveCards();
      } else {
        setActiveTab("settings");
      }
    });
  }

  async function autodetectConfig() {
    await runAction("Auto-detecting tools", async () => {
      const next = await autodetectRuntimeConfig();
      setConfigState(next);
      setConfigDraft(buildConfigDraft(next.config));
      setConfigReady(next.validation.ok);
      if (!next.validation.ok) {
        setActiveTab("settings");
      }
    });
  }

  async function pickConfigPath(
    key: "makemkv_path" | "ffmpeg_path" | "ffprobe_path" | "staging_root" | "nas_root",
    kind: "file" | "directory",
    title: string,
  ) {
    const current = configDraft[key] ?? "";
    const selected = kind === "file"
      ? await browseFilePath(title, current)
      : await browseDirectoryPath(title, current);
    if (!selected) return;
    setConfigDraft((value) => ({ ...value, [key]: selected }));
  }

  useEffect(() => {
    selectedJobIdRef.current = selectedJobId;
  }, [selectedJobId]);

  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

  useEffect(() => {
    driveCardsRef.current = driveCards;
  }, [driveCards]);

  useEffect(() => {
    busyActionRef.current = busyAction;
  }, [busyAction]);

  useEffect(() => {
    void loadConfigState();
  }, []);

  useEffect(() => {
    let disposed = false;
    const unsubscribePromise = listen<string>("app-menu", async (event) => {
      if (disposed) return;
      if (event.payload === "settings") {
        setActiveTab("settings");
      } else if (event.payload === "reload-config") {
        await loadConfigState();
      }
    });
    return () => {
      disposed = true;
      void unsubscribePromise.then((unsubscribe) => unsubscribe());
    };
  }, []);

  useEffect(() => {
    if (!configReady) return;
    void refreshJobs();
    void refreshAllDriveCards();
    const timer = window.setInterval(() => void refreshJobs(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [configReady]);

  useEffect(() => {
    if (!configReady) return;
    if (configState?.makemkvStatus?.level === "error") {
      setDriveCards((cards) =>
        cards.map((card) =>
          card.continuousMode
            ? {
                ...card,
                continuousStatus:
                  configState.makemkvStatus.message || "Continuous mode paused — MakeMKV needs attention",
              }
            : card,
        ),
      );
      return;
    }

    let cancelled = false;
    const pollForDisc = async () => {
      if (cancelled || busyActionRef.current !== null) {
        return;
      }
      const activeCards = driveCardsRef.current.filter((card) => card.continuousMode);
      if (activeCards.length === 0) {
        return;
      }
      try {
        const drives = await listDiscDrives();
        if (cancelled) return;
        setDiscDrives(drives);
        for (const card of activeCards) {
          const disc = await detectDisc(card.form.opticalDrive);
          if (cancelled) return;
          if (!disc?.has_media) {
            lastAutoStartedDiscRef.current[card.id] = null;
            setDriveCards((cards) =>
              cards.map((entry) =>
                entry.id === card.id ? { ...entry, continuousStatus: "Continuous mode on — waiting for disc" } : entry,
              ),
            );
            continue;
          }

          const signature = `${disc.drive}|${disc.volume_label}`;
          if (lastAutoStartedDiscRef.current[card.id] === signature) {
            setDriveCards((cards) =>
              cards.map((entry) =>
                entry.id === card.id ? { ...entry, continuousStatus: `Watching disc: ${disc.volume_label}` } : entry,
              ),
            );
            continue;
          }
          setDriveCards((cards) =>
            cards.map((entry) =>
              entry.id === card.id
                ? {
                    ...entry,
                    form: {
                      ...entry.form,
                      opticalDrive: disc.drive,
                      discLabel: disc.volume_label || entry.form.discLabel,
                    },
                  }
                : entry,
            ),
          );

          if (autoStartInFlightRef.current[card.id] === signature) {
            setDriveCards((cards) =>
              cards.map((entry) =>
                entry.id === card.id
                  ? { ...entry, continuousStatus: `Auto-start already in progress: ${disc.volume_label}` }
                  : entry,
              ),
            );
            continue;
          }

          const hasActiveDiscJob = jobsRef.current.some(
            (job) =>
              (job.status === "queued" || job.status === "ripping") &&
              ((job.optical_drive ?? "").toUpperCase() === disc.drive.toUpperCase()),
          );
          if (hasActiveDiscJob) {
            setDriveCards((cards) =>
              cards.map((entry) =>
                entry.id === card.id
                  ? { ...entry, continuousStatus: `Disc detected: ${disc.volume_label} — waiting for active rip to finish` }
                  : entry,
              ),
            );
            continue;
          }

          const matchingExistingJob = jobsRef.current.find(
            (job) =>
              job.disc_label === disc.volume_label &&
              (job.optical_drive ?? "").toUpperCase() === disc.drive.toUpperCase(),
          );
          if (matchingExistingJob) {
            lastAutoStartedDiscRef.current[card.id] = signature;
            setDriveCards((cards) =>
              cards.map((entry) =>
                entry.id === card.id
                  ? { ...entry, continuousStatus: `Disc already has an active job: ${disc.volume_label}` }
                  : entry,
              ),
            );
            continue;
          }

          setDriveCards((cards) =>
            cards.map((entry) =>
              entry.id === card.id ? { ...entry, continuousStatus: `Auto-starting: ${disc.volume_label}` } : entry,
            ),
          );
          lastAutoStartedDiscRef.current[card.id] = signature;
          autoStartInFlightRef.current[card.id] = signature;
          setError(null);
          const request: StartJobRequest = {
            ...normalizeStartRequest(card.form),
            opticalDrive: disc.drive,
            discLabel: disc.volume_label || card.form.discLabel,
          };
          const jobId = await startPipeline(request);
          setSelectedJobId(jobId);
          await refreshJobs();
          await refreshSnapshot(jobId);
          setDriveCards((cards) =>
            cards.map((entry) =>
              entry.id === card.id ? { ...entry, continuousStatus: `Started: ${disc.volume_label}` } : entry,
            ),
          );
          autoStartInFlightRef.current[card.id] = null;
        }
      } catch (err) {
        setError(String(err));
        setDriveCards((cards) =>
          cards.map((card) =>
            card.continuousMode ? { ...card, continuousStatus: "Continuous mode hit an error" } : card,
          ),
        );
      }
    };

    void pollForDisc();
    const timer = window.setInterval(() => void pollForDisc(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [configReady, configState?.makemkvStatus?.level, configState?.makemkvStatus?.message]);

  async function refreshDriveCard(cardId: string) {
    try {
      const drives = await listDiscDrives();
      setDiscDrives(drives);
      const card = driveCardsRef.current.find((entry) => entry.id === cardId);
      if (!card) return;
      const currentDrive = card.form.opticalDrive ?? null;
      const disc = await detectDisc(currentDrive);
      setDriveCards((cards) =>
        cards.map((entry) =>
          entry.id === cardId
            ? {
                ...entry,
                form: {
                  ...entry.form,
                  opticalDrive: disc?.drive ?? entry.form.opticalDrive ?? drives[0]?.drive ?? null,
                  discLabel: disc?.volume_label ?? entry.form.discLabel,
                },
              }
            : entry,
        ),
      );
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }

  async function refreshAllDriveCards() {
    const cards = driveCardsRef.current;
    if (!cards.length) return;
    await Promise.all(cards.map((card) => refreshDriveCard(card.id)));
  }

  function updateDriveCard(cardId: string, updater: (card: DriveCardState) => DriveCardState) {
    setDriveCards((cards) => cards.map((card) => (card.id === cardId ? updater(card) : card)));
  }

  function addDriveCard() {
    setDriveCards((cards) => [...cards, buildDriveCardState()]);
  }

  function removeDriveCard(cardId: string) {
    setDriveCards((cards) => (cards.length > 1 ? cards.filter((card) => card.id !== cardId) : cards));
    delete lastAutoStartedDiscRef.current[cardId];
    delete autoStartInFlightRef.current[cardId];
  }

  useEffect(() => {
    if (!configReady) return;
    if (!selectedJobId) return;
    void refreshSnapshot(selectedJobId);
    const timer = window.setInterval(() => void refreshSnapshot(selectedJobId), POLL_MS + 1000);
    return () => window.clearInterval(timer);
  }, [configReady, selectedJobId]);

  useEffect(() => {
    setActionsMenuOpen(false);
  }, [selectedJobId, snapshot?.job.status, busyAction]);

  async function runAction(name: string, fn: () => Promise<void>) {
    setBusyAction(name);
    setError(null);
    try {
      await fn();
      await refreshJobs();
      if (selectedJobId) {
        await refreshSnapshot(selectedJobId);
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setBusyAction(null);
    }
  }

  const bundles = snapshot?.episode_mappings ?? [];
  const artifacts = useMemo(() => {
    const rows: Array<{ kind: string; value: string }> = [];
    const menuAnalysis = snapshot?.menu_analysis ?? null;
    const bundleAssociation = snapshot?.bundle_association ?? null;
    if (menuAnalysis) {
      for (const [key, value] of Object.entries(menuAnalysis)) {
        if (Array.isArray(value)) {
          value.forEach((item) =>
            rows.push({
              kind: key,
              value: typeof item === "string" ? item : JSON.stringify(item),
            }),
          );
        } else {
          rows.push({ kind: key, value: JSON.stringify(value) });
        }
      }
    }
    if (bundleAssociation) {
      for (const [key, value] of Object.entries(bundleAssociation)) {
        rows.push({ kind: `bundle_${key}`, value: JSON.stringify(value) });
      }
    }
    return rows;
  }, [snapshot]);

  const mappingOptions = useMemo(() => snapshot?.episode_mappings ?? [], [snapshot]);
  const tmdbCandidates = useMemo(() => snapshot?.tmdb_candidates ?? [], [snapshot]);
  const ripTitleOptions = useMemo(() => snapshot?.rip_titles ?? [], [snapshot]);
  const splitPlanOptions = useMemo(() => snapshot?.split_plans ?? [], [snapshot]);
  const reviewState = snapshot?.review_state ?? null;
  const progressState = snapshot?.progress_state ?? null;
  const makemkvStatus = configState?.makemkvStatus ?? null;
  const selectedMovies = useMemo(() => snapshot?.selected_movies ?? [], [snapshot]);
  const hasLocalArtifacts = Boolean(
    snapshot &&
      (
        snapshot.outputs.length > 0 ||
        snapshot.rip_titles.length > 0 ||
        snapshot.split_plans.length > 0 ||
        snapshot.episode_mappings.length > 0 ||
        snapshot.menu_analysis ||
        snapshot.bundle_association ||
        snapshot.dvdnav_menu
      ),
  );
  const canRemapRemoteOutput = Boolean(
    snapshot
      && snapshot.job.status === "done"
      && snapshot.job.media_type === "movie"
      && snapshot.job.movie_mode === "single"
      && !hasLocalArtifacts
      && snapshot.selected_media
      && tmdbCandidates.length > 0,
  );
  const recentLogs = useMemo(() => [...(snapshot?.logs ?? [])].slice(-12).reverse(), [snapshot]);
  const latestLog = recentLogs[0] ?? null;
  const lastActivityAt = latestLog?.timestamp ?? snapshot?.job.updated_at ?? null;
  const stageStartAt = useMemo(() => {
    if (!snapshot) return null;
    const stage = snapshot.job.current_stage ?? snapshot.job.status;
    const match = [...snapshot.logs]
      .reverse()
      .find((log) => log.to_status === stage || (log.from_status === null && log.to_status === null && log.message.includes(`Transitioned`) && log.message.endsWith(`-> ${stage}`)));
    return match?.timestamp ?? snapshot.job.updated_at ?? null;
  }, [snapshot]);
  const reviewNeeded = Boolean(reviewState?.tmdb.needed || reviewState?.mapping.needed);
  const ripReviewNeeded = Boolean(reviewState?.rip?.needed);
  const pipelineStages = snapshot ? getPipelineStages(snapshot.job.media_type) : TV_PIPELINE_STAGES;
  const showBundleOverview = snapshot?.job.media_type === "tv";
  const isMultiMovie = snapshot?.job.media_type === "movie" && snapshot.job.movie_mode !== "single";
  const requiredMovieSlots = movieSlotCount(snapshot?.job.movie_mode);
  const activityState = snapshot ? getActivityState(snapshot.job.status, reviewNeeded || ripReviewNeeded, lastActivityAt) : "Monitoring";
  const progressPercent = snapshot ? Math.round((progressState?.overall_fraction ?? (getProgressPercent(snapshot.job.status, pipelineStages) / 100)) * 100) : 0;
  const currentStageIndex = snapshot ? getPipelineStageIndex(snapshot.job.status, pipelineStages, snapshot.job.current_stage) : 0;
  const progressEta = formatEta(progressState?.eta_seconds);
  const showSettingsTab = activeTab === "settings" || !configReady;
  const makemkvBlocking = makemkvStatus?.level === "error";
  const showMakemkvBanner = Boolean(configReady && makemkvStatus && makemkvStatus.level !== "ok" && makemkvStatus.message);
  const makemkvExpiryLabel = formatCalendarDate(makemkvStatus?.betaKeyExpiresAt);
  const stageDuration = snapshot?.job.status === "done"
    ? formatCompletionSince(stageStartAt)
    : formatDurationSince(stageStartAt);
  const stageProgressPercent = progressState ? Math.round((progressState.stage_fraction ?? 0) * 100) : null;
  const heroSubtitle = snapshot ? getHeroSubtitle(snapshot, isMultiMovie, selectedMovies) : null;
  const topTmdbCandidate = reviewState?.tmdb.top_candidate ?? tmdbCandidates[0] ?? null;
  const selectedTmdbResolved = useMemo(
    () => tmdbCandidates.find((candidate) => candidate.selected === 1) ?? null,
    [tmdbCandidates],
  );
  const selectedTmdbSource = parseScoreBreakdown(selectedTmdbResolved?.score_breakdown_json)?.query_source;
  const selectedTmdbCandidate = useMemo(
    () => tmdbCandidates.find((candidate) => String(candidate.tmdb_id) === modalValues.tmdbId && candidate.media_type === modalValues.mediaType) ?? null,
    [modalValues.mediaType, modalValues.tmdbId, tmdbCandidates],
  );
  const lowConfidenceMappings = useMemo(
    () => mappingOptions.filter((mapping) => mapping.episode_start != null && (mapping.confidence ?? 0) < 0.85),
    [mappingOptions],
  );
  useEffect(() => {
    if (modal !== "tmdb") return;
    if (!tmdbCandidates.length) return;
    setModalValues((current) => {
      if (current.tmdbId && current.mediaType) {
        const stillExists = tmdbCandidates.some(
          (candidate) =>
            String(candidate.tmdb_id) === current.tmdbId &&
            candidate.media_type === current.mediaType,
        );
        if (stillExists) {
          return current;
        }
      }
      return {
        ...current,
        tmdbId: String(tmdbCandidates[0].tmdb_id),
        mediaType: tmdbCandidates[0].media_type,
      };
    });
  }, [modal, tmdbCandidates]);
  const allSeasonEpisodeOptions = useMemo(() => {
    return (snapshot?.all_season_episodes ?? [])
      .map((episode) => ({
        value: String(episode.episode_number),
        label: `E${String(episode.episode_number).padStart(2, "0")} • ${episode.name}`,
      }))
      .sort((a, b) => Number(a.value) - Number(b.value));
  }, [snapshot]);
  const episodeOptions = useMemo(() => {
    const filtered = (snapshot?.season_episodes ?? [])
      .map((episode) => ({
        value: String(episode.episode_number),
        label: `E${String(episode.episode_number).padStart(2, "0")} • ${episode.name}`,
      }))
      .sort((a, b) => Number(a.value) - Number(b.value));
    return filtered.length > 0 ? filtered : allSeasonEpisodeOptions;
  }, [allSeasonEpisodeOptions, snapshot]);

  function runSelectedAction(name: string, fn: (jobId: string) => Promise<void>) {
    if (!selectedJobId) return;
    setActionsMenuOpen(false);
    void runAction(name, () => fn(selectedJobId));
  }

  const allActions = useMemo<QuickAction[]>(() => {
    const disabled = !selectedJobId || busyAction !== null;
    const actions: QuickAction[] = [
      {
        key: "resume",
        label: "Resume",
        disabled,
        onClick: () => runSelectedAction("Resume", resumePipeline),
      },
      {
        key: "analyze-menu",
        label: "Analyze DVD Menu",
        disabled,
        onClick: () => runSelectedAction("Analyze Menu", analyzeMenu),
      },
      {
        key: "rerun-mapping",
        label: "Re-run Mapping",
        disabled,
        onClick: () => runSelectedAction("Re-map", rerunMapping),
      },
      {
        key: "rerun-identify",
        label: "Re-run TMDB",
        disabled,
        onClick: () => runSelectedAction("Re-identify", rerunIdentify),
      },
      {
        key: "override-mapping",
        label: "Override Mapping",
        disabled: disabled || mappingOptions.length === 0,
        onClick: () => {
          setActionsMenuOpen(false);
          openOverrideModal("map");
        },
      },
      {
        key: "override-file",
        label: "Override File",
        disabled: disabled || mappingOptions.length === 0,
        onClick: () => {
          setActionsMenuOpen(false);
          openOverrideModal("file");
        },
      },
      {
        key: "override-split",
        label: "Override Split",
        disabled: disabled || splitPlanOptions.length === 0,
        onClick: () => {
          setActionsMenuOpen(false);
          openOverrideModal("split");
        },
      },
      {
        key: "review-tmdb",
        label: "Review TMDB Candidates",
        disabled,
        onClick: () => {
          setActionsMenuOpen(false);
          openTmdbReviewModal();
        },
      },
      {
        key: "guided-review",
        label: "Guided Manual Review",
        disabled: disabled || snapshot?.job.media_type !== "tv" || (mappingOptions.length === 0 && ripTitleOptions.length === 0),
        onClick: () => {
          setActionsMenuOpen(false);
          openGuidedReviewModal();
        },
      },
      {
        key: "cancel-job",
        label: "Cancel Job",
        disabled,
        tone: "danger",
        onClick: () => runSelectedAction("Cancel job", cancelJob),
      },
      {
        key: "clear-local",
        label: "Clear Local Artifacts",
        disabled,
        onClick: () => runSelectedAction("Clear local artifacts", clearLocalArtifacts),
      },
      {
        key: "rebuild-output",
        label: "Rebuild Outputs",
        disabled,
        onClick: () => runSelectedAction("Rebuild outputs", rebuildOutput),
      },
      {
        key: "delete",
        label: "Delete",
        disabled,
        tone: "danger",
        onClick: () => runSelectedAction("Delete", deleteJob),
      },
    ];
    if (canRemapRemoteOutput) {
      actions.splice(actions.length - 1, 0, {
        key: "remap-remote",
        label: "Remap Remote to TMDB",
        disabled,
        onClick: () => runSelectedAction("Remap remote output", remapRemoteOutput),
      });
    }
    return actions;
  }, [busyAction, canRemapRemoteOutput, mappingOptions.length, ripTitleOptions.length, selectedJobId, snapshot?.job.media_type, splitPlanOptions.length]);

  const primaryActions = useMemo<QuickAction[]>(() => {
    if (!snapshot) return [];
    const byKey = new Map(allActions.map((action) => [action.key, action]));
    const keys: string[] = [];
    if (reviewState?.tmdb.needed) {
      keys.push("review-tmdb", "rerun-identify");
    } else if (reviewState?.mapping.needed) {
      keys.push("override-mapping", "override-file", "rerun-mapping");
    } else {
      switch (snapshot.job.status) {
        case "queued":
        case "error":
          keys.push("resume");
          break;
        case "ripping":
          keys.push("resume");
          break;
        case "identifying":
          keys.push("rerun-identify");
          if (snapshot.job.media_type === "movie") {
            keys.push("analyze-menu");
          }
          break;
        case "mapping":
          if (snapshot.job.media_type === "movie") {
            keys.push("resume", "rerun-identify");
          } else {
            keys.push("rerun-mapping");
          }
          break;
        case "splitting":
          keys.push("override-split", "resume");
          break;
        case "renaming":
        case "copying":
          keys.push("resume");
          break;
        case "done":
          if (hasLocalArtifacts) {
            keys.push("clear-local");
          }
          if (snapshot.job.media_type === "movie") {
            keys.push("rebuild-output", "rerun-identify");
          } else {
            keys.push("rebuild-output", "rerun-mapping");
          }
          break;
      }
    }
    const uniqueKeys = [...new Set(keys)];
    return uniqueKeys.map((key) => byKey.get(key)).filter((action): action is QuickAction => Boolean(action));
  }, [allActions, hasLocalArtifacts, reviewState, snapshot]);

  function openOverrideModal(kind: "map" | "file" | "split") {
    const initial: Record<string, string> = {};
    if (kind === "map" && mappingOptions[0]) {
      initial.mappingId = String(mappingOptions[0].id);
      initial.episodeStart = mappingOptions[0].episode_start != null ? String(mappingOptions[0].episode_start) : (episodeOptions[0]?.value ?? "");
      initial.episodeEnd = mappingOptions[0].episode_end != null ? String(mappingOptions[0].episode_end) : (episodeOptions[0]?.value ?? "");
    }
    if (kind === "file" && mappingOptions[0]) {
      initial.mappingId = String(mappingOptions[0].id);
      initial.ripTitleId = ripTitleOptions[0] ? String(ripTitleOptions[0].id) : "";
    }
    if (kind === "split" && splitPlanOptions[0]) {
      initial.splitPlanId = String(splitPlanOptions[0].id);
      initial.startSeconds = splitPlanOptions[0].start_seconds != null ? String(splitPlanOptions[0].start_seconds) : "";
      initial.endSeconds = splitPlanOptions[0].end_seconds != null ? String(splitPlanOptions[0].end_seconds) : "";
    }
    setModal(kind);
    setModalJobId(selectedJobId);
    setModalValues(initial);
  }

  function openTmdbReviewModal() {
    const initial: Record<string, string> = {};
    initial.searchQuery = jobs.find((job) => job.id === selectedJobId)?.disc_label ?? snapshot?.job.disc_label ?? "";
    if (tmdbCandidates[0]) {
      initial.tmdbId = String(tmdbCandidates[0].tmdb_id);
      initial.mediaType = tmdbCandidates[0].media_type;
    }
    if (isMultiMovie) {
      const takenSlots = new Set(selectedMovies.map((slot) => slot.slot_index));
      const nextSlot = Array.from({ length: requiredMovieSlots }, (_, index) => index + 1).find((slot) => !takenSlots.has(slot)) ?? 1;
      initial.slotIndex = String(nextSlot);
    }
    setModal("tmdb");
    setModalJobId(selectedJobId);
    setModalValues(initial);
  }

  function openGuidedReviewModal() {
    setModal("review");
    setModalJobId(selectedJobId);
    setGuidedReviewRows(buildGuidedReviewRows(ripTitleOptions, mappingOptions));
    setGuidedSplitDrafts(buildGuidedSplitDrafts(splitPlanOptions));
    setModalValues({
      rangeStart: snapshot?.job.episode_range_start != null ? String(snapshot.job.episode_range_start) : "",
      rangeEnd: snapshot?.job.episode_range_end != null ? String(snapshot.job.episode_range_end) : "",
    });
  }

  async function runManualTmdbSearch() {
    const jobId = modalJobId ?? selectedJobId;
    if (!jobId) return;
    const query = (modalValues.searchQuery ?? "").trim();
    if (!query) return;
    await runAction("Search TMDB", () => searchTmdbCandidates(jobId, query));
  }

  async function submitModalAction() {
    if (!modal) return;
    const actionName = modal === "tmdb" ? "Select TMDB" : "Override";
    await runAction(actionName, async () => {
      if (modal === "tmdb") {
        const jobId = modalJobId ?? selectedJobId;
        if (!jobId) {
          throw new Error("No job selected for TMDB selection.");
        }
        if (isMultiMovie) {
          await selectMovieSlotCandidate(
            jobId,
            Number(modalValues.slotIndex),
            Number(modalValues.tmdbId),
          );
        } else {
          await selectTmdbCandidate(
            jobId,
            (modalValues.mediaType as "tv" | "movie"),
            Number(modalValues.tmdbId),
          );
        }
      } else if (modal === "map") {
        await overrideMapping(
          Number(modalValues.mappingId),
          Number(modalValues.episodeStart),
          Number(modalValues.episodeEnd),
        );
      } else if (modal === "file") {
        await overrideMappingSource(
          Number(modalValues.mappingId),
          Number(modalValues.ripTitleId),
        );
      } else if (modal === "split") {
        await overrideSplit(
          Number(modalValues.splitPlanId),
          modalValues.startSeconds ? Number(modalValues.startSeconds) : null,
          modalValues.endSeconds ? Number(modalValues.endSeconds) : null,
        );
      }
    });
    setModal(null);
    setModalJobId(null);
  }

  async function saveGuidedReviewAssignments() {
    const jobId = modalJobId ?? selectedJobId;
    if (!jobId) return;
    await runAction("Save guided review", async () => {
      for (const row of guidedReviewRows) {
        if (!row.mappingId) {
          continue;
        }
        if (row.status === "ignore") {
          await ignoreMapping(Number(row.mappingId));
          continue;
        }
        if (!row.episodeStart || !row.episodeEnd) {
          throw new Error(`Missing episode range for ${fileNameFromPath(row.sourceFile)}.`);
        }
        await overrideMapping(
          Number(row.mappingId),
          Number(row.episodeStart),
          Number(row.episodeEnd),
        );
      }
      await planSplits(jobId);
      const next = await getJobSnapshot(jobId);
      setSnapshot(next);
      setGuidedReviewRows(buildGuidedReviewRows(next.rip_titles, next.episode_mappings));
      setGuidedSplitDrafts(buildGuidedSplitDrafts(next.split_plans));
      setModalValues((values) => ({
        ...values,
        rangeStart: next.job.episode_range_start != null ? String(next.job.episode_range_start) : "",
        rangeEnd: next.job.episode_range_end != null ? String(next.job.episode_range_end) : "",
      }));
    });
  }

  async function saveGuidedSplitDrafts() {
    await runAction("Save split table", async () => {
      for (const draft of guidedSplitDrafts) {
        if (!draft.splitPlanId) {
          continue;
        }
        await overrideSplit(
          Number(draft.splitPlanId),
          draft.startSeconds ? Number(draft.startSeconds) : null,
          draft.endSeconds ? Number(draft.endSeconds) : null,
        );
      }
    });
    const jobId = modalJobId ?? selectedJobId;
    if (jobId) {
      const next = await getJobSnapshot(jobId);
      setSnapshot(next);
      setGuidedSplitDrafts(buildGuidedSplitDrafts(next.split_plans));
    }
  }

  async function updateGuidedEpisodeRange() {
    const jobId = modalJobId ?? selectedJobId;
    if (!jobId || !snapshot) return;
    const rangeStart = modalValues.rangeStart ? Number(modalValues.rangeStart) : null;
    const rangeEnd = modalValues.rangeEnd ? Number(modalValues.rangeEnd) : null;
    await runAction("Update episode range", async () => {
      await updateJobProfile(
        jobId,
        snapshot.job.disc_scope ?? "partial_season",
        snapshot.job.season_number ?? 1,
        rangeStart,
        rangeEnd,
      );
      await rerunMapping(jobId);
    });
    const next = await getJobSnapshot(jobId);
    setSnapshot(next);
    setGuidedReviewRows(buildGuidedReviewRows(next.rip_titles, next.episode_mappings));
    setGuidedSplitDrafts(buildGuidedSplitDrafts(next.split_plans));
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div>
            <h1>Auto-Ripper</h1>
            <p>Desktop workflow for rip, map, split, and transfer.</p>
          </div>
          <div className="toolbar-actions">
            <button className="ghost-button" onClick={() => setActiveTab("settings")}>
              Settings
            </button>
            <button className="ghost-button" disabled={!configReady} onClick={() => void refreshJobs()}>
              Refresh
            </button>
          </div>
        </div>

        <section className="app-controls-card">
          <div className="section-header">
            <h2>App</h2>
            <span className={`status-pill status-${configReady ? "done" : "error"}`}>
              {configReady ? "Ready" : "Setup needed"}
            </span>
          </div>
          <div className="app-controls-grid">
            <button className="primary-button" onClick={() => setActiveTab("settings")}>
              Settings
            </button>
            <button disabled={busyAction !== null} onClick={() => void loadConfigState()}>
              Reload Config
            </button>
            <button disabled={busyAction !== null} onClick={() => void autodetectConfig()}>
              Auto-detect Tools
            </button>
            <button disabled={!configReady || busyAction !== null} onClick={() => void refreshJobs()}>
              Refresh Jobs
            </button>
          </div>
          {!configReady ? (
            <p className="app-controls-note">
              Complete Settings before ripping. The app will store everything in your user config automatically.
            </p>
          ) : makemkvBlocking ? (
            <p className="app-controls-note">{makemkvStatus?.message}</p>
          ) : null}
        </section>

        <section className="start-card">
          <div className="section-header">
            <h2>Start new disc</h2>
            <button type="button" disabled={busyAction !== null || !configReady} onClick={addDriveCard}>
              + Drive
            </button>
          </div>
          <div className="drive-cards">
            {driveCards.map((card, index) => {
              const showDiscScope = card.form.mediaType === "tv";
              const showSeasonNumber = card.form.mediaType === "tv";
              const showEpisodeRange = card.form.mediaType === "tv" && card.form.discScope === "partial_season";
              const showMovieMode = card.form.mediaType === "movie";
              return (
                <div key={card.id} className="drive-card">
                  <div className="drive-card-header">
                    <div>
                      <h3>{`Drive ${index + 1}`}</h3>
                      <p>{card.form.opticalDrive ? `Watching ${card.form.opticalDrive}` : "Auto / first disc with media"}</p>
                    </div>
                    <div className="drive-card-actions">
                      <button
                        type="button"
                        className={card.continuousMode ? "primary-button" : undefined}
                        disabled={busyAction !== null || !configReady || (makemkvBlocking && !card.continuousMode)}
                        onClick={() =>
                          updateDriveCard(card.id, (value) => ({
                            ...value,
                            continuousMode: !value.continuousMode,
                            continuousStatus: !value.continuousMode ? "Continuous mode on — waiting for disc" : null,
                          }))
                        }
                      >
                        {card.continuousMode ? "Continuous On" : "Continuous Off"}
                      </button>
                      {driveCards.length > 1 ? (
                        <button type="button" className="ghost-button" disabled={busyAction !== null} onClick={() => removeDriveCard(card.id)}>
                          Remove
                        </button>
                      ) : null}
                    </div>
                  </div>
                  <div className="form-grid">
                    <label>
                      <span>Disc label</span>
                      <input
                        value={card.form.discLabel}
                        onChange={(e) =>
                          updateDriveCard(card.id, (value) => ({
                            ...value,
                            form: { ...value.form, discLabel: e.target.value },
                          }))
                        }
                      />
                    </label>
                    <label>
                      <span>&nbsp;</span>
                      <button type="button" disabled={!configReady} onClick={() => void refreshDriveCard(card.id)}>
                        Refresh Disc
                      </button>
                    </label>
                    <label>
                      <span>Optical drive</span>
                      <select
                        value={card.form.opticalDrive ?? ""}
                        onChange={(e) => {
                          const nextDrive = e.target.value || null;
                          const selectedDrive = discDrives.find((drive) => drive.drive === nextDrive) ?? null;
                          updateDriveCard(card.id, (value) => ({
                            ...value,
                            form: {
                              ...value.form,
                              opticalDrive: nextDrive,
                              discLabel: selectedDrive?.has_media ? (selectedDrive.volume_label || value.form.discLabel) : value.form.discLabel,
                            },
                          }));
                        }}
                      >
                        <option value="">Auto / first disc with media</option>
                        {discDrives.map((drive) => (
                          <option key={drive.drive} value={drive.drive}>
                            {drive.drive} {drive.has_media ? `• ${drive.volume_label || "Disc inserted"}` : "• Empty"}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span>Media type</span>
                      <select
                        value={card.form.mediaType}
                        onChange={(e) =>
                          updateDriveCard(card.id, (value) => ({
                            ...value,
                            form: { ...value.form, mediaType: e.target.value as "tv" | "movie" },
                          }))
                        }
                      >
                        <option value="tv">TV</option>
                        <option value="movie">Movie</option>
                      </select>
                    </label>
                    {showDiscScope ? (
                      <label>
                        <span>Disc scope</span>
                        <select
                          value={card.form.discScope}
                          onChange={(e) =>
                            updateDriveCard(card.id, (value) => ({
                              ...value,
                              form: {
                                ...value.form,
                                discScope: e.target.value as "full_season" | "partial_season" | "special" | "custom",
                              },
                            }))
                          }
                        >
                          <option value="full_season">Full season</option>
                          <option value="partial_season">Partial season</option>
                          <option value="special">Special</option>
                          <option value="custom">Custom</option>
                        </select>
                      </label>
                    ) : null}
                    {showMovieMode ? (
                      <label>
                        <span>Movie mode</span>
                        <select
                          value={card.form.movieMode ?? "single"}
                          onChange={(e) =>
                            updateDriveCard(card.id, (value) => ({
                              ...value,
                              form: {
                                ...value.form,
                                movieMode: e.target.value as "single" | "double_feature" | "trilogy",
                              },
                            }))
                          }
                        >
                          <option value="single">Single</option>
                          <option value="double_feature">Double Feature</option>
                          <option value="trilogy">Trilogy</option>
                        </select>
                      </label>
                    ) : null}
                    {showSeasonNumber ? (
                      <label>
                        <span>Season</span>
                        <input
                          type="number"
                          value={card.form.seasonNumber ?? ""}
                          onChange={(e) =>
                            updateDriveCard(card.id, (value) => ({
                              ...value,
                              form: {
                                ...value.form,
                                seasonNumber: e.target.value ? Number(e.target.value) : null,
                              },
                            }))
                          }
                        />
                      </label>
                    ) : null}
                    {showEpisodeRange ? (
                      <>
                        <label>
                          <span>Episode start</span>
                          <input
                            type="number"
                            value={card.form.episodeRangeStart ?? ""}
                            onChange={(e) =>
                              updateDriveCard(card.id, (value) => ({
                                ...value,
                                form: {
                                  ...value.form,
                                  episodeRangeStart: e.target.value ? Number(e.target.value) : null,
                                },
                              }))
                            }
                          />
                        </label>
                        <label>
                          <span>Episode end</span>
                          <input
                            type="number"
                            value={card.form.episodeRangeEnd ?? ""}
                            onChange={(e) =>
                              updateDriveCard(card.id, (value) => ({
                                ...value,
                                form: {
                                  ...value.form,
                                  episodeRangeEnd: e.target.value ? Number(e.target.value) : null,
                                },
                              }))
                            }
                          />
                        </label>
                      </>
                    ) : null}
                  </div>
                  <button
                    className="primary-button"
                    disabled={busyAction !== null || !configReady || makemkvBlocking}
                    onClick={() =>
                      void runAction(`Starting pipeline (${card.form.opticalDrive ?? `Drive ${index + 1}`})`, async () => {
                        const latestCard = driveCardsRef.current.find((entry) => entry.id === card.id) ?? card;
                        const jobId = await startPipeline(normalizeStartRequest(latestCard.form));
                        setSelectedJobId(jobId);
                      })
                    }
                  >
                    Start End-to-End
                  </button>
                  {card.continuousStatus ? <p className="continuous-status">{card.continuousStatus}</p> : null}
                </div>
              );
            })}
          </div>
        </section>

        <section className="jobs-card">
          <div className="section-header">
            <h2>Jobs</h2>
            <span>{jobs.length}</span>
          </div>
          {!configReady ? (
            <div className="empty-state">Complete setup in Settings before jobs and disc actions will run.</div>
          ) : null}
          <div className="job-list">
            {jobs.map((job) => (
              <button
                key={job.id}
                className={`job-row ${selectedJobId === job.id ? "selected" : ""} ${job.has_local_artifacts ? "has-local-artifacts" : ""}`}
                onClick={() => setSelectedJobId(job.id)}
              >
                <div className="job-row-top">
                  <strong className="job-row-title">{job.disc_label || "Untitled Disc"}</strong>
                  <span className={`status-pill status-${job.status}`}>{job.status}</span>
                </div>
                <div className="job-row-meta">
                  <span>{job.media_type.toUpperCase()}</span>
                  <span>{job.optical_drive ? `Drive ${job.optical_drive}` : "Auto drive"}</span>
                  <span>{job.current_stage ?? job.status}</span>
                  <span>{formatRelativeTime(job.updated_at)}</span>
                </div>
              </button>
            ))}
          </div>
        </section>
      </aside>

      <main className="content">
        {snapshot ? (
          <section className="panel progress-panel hero-panel">
            <div className="hero-top">
              <div className="toolbar-title hero-title-block">
                <h2 className="hero-title">{snapshot.job.disc_label}</h2>
                {heroSubtitle ? <p className="hero-subtitle">{heroSubtitle}</p> : null}
                {selectedTmdbSource ? <p>TMDB query source: {String(selectedTmdbSource)}</p> : null}
                <p>{activityState} • last activity {formatRelativeTime(lastActivityAt)}</p>
              </div>
              <div className="hero-actions">
                {primaryActions.map((action, index) => (
                  <button
                    key={action.key}
                    className={index === 0 ? "primary-button" : action.tone === "danger" ? "danger-button" : undefined}
                    disabled={action.disabled}
                    onClick={action.onClick}
                  >
                    {action.label}
                  </button>
                ))}
                <div className="actions-menu-wrap">
                  <button
                    className="ghost-button"
                    disabled={!selectedJobId || busyAction !== null}
                    onClick={() => setActionsMenuOpen((open) => !open)}
                  >
                    ...
                  </button>
                  {actionsMenuOpen ? (
                    <div className="actions-menu">
                      {allActions.map((action) => (
                        <button
                          key={action.key}
                          className={action.tone === "danger" ? "danger-button actions-menu-item" : "actions-menu-item"}
                          disabled={action.disabled}
                          onClick={action.onClick}
                        >
                          {action.label}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
            <div className="hero-progress-row">
              <div className="progress-meta">
                <strong>{progressPercent}%</strong>
                <span className={`status-pill status-${snapshot.job.status}`}>{snapshot.job.current_stage ?? snapshot.job.status}</span>
              </div>
              {progressState?.detail ? (
                <div className="progress-detail">
                  <span>{progressState.detail}</span>
                  {progressState.rate_mb_s ? <span>{progressState.rate_mb_s.toFixed(2)} MB/s</span> : null}
                  {progressEta ? <span>{progressEta}</span> : null}
                </div>
              ) : null}
              {stageDuration || (stageProgressPercent !== null && snapshot.job.status !== "done") ? (
                <div className="stage-substatus">
                  {stageDuration ? <span>{stageDuration}</span> : null}
                  {stageProgressPercent !== null && snapshot.job.status !== "done" ? <span>{stageProgressPercent}% through stage</span> : null}
                </div>
              ) : null}
              <div className="progress-bar-track">
                <div className="progress-bar-fill" style={{ width: `${progressPercent}%` }} />
              </div>
              <div className="stage-list">
                {pipelineStages.map((stage) => {
                  const stageIndex = pipelineStages.indexOf(stage);
                  const completed = currentStageIndex >= stageIndex;
                  const active = snapshot.job.status === stage;
                  return (
                    <span
                      key={stage}
                      className={`stage-pill ${completed ? "is-complete" : ""} ${active ? "is-active" : ""}`}
                    >
                      {stage}
                    </span>
                  );
                })}
              </div>
            </div>
          </section>
        ) : null}

        {error ? <div className="error-banner">{error}</div> : null}
        {showMakemkvBanner ? (
          <div className={`review-banner ${makemkvStatus?.level === "error" ? "review-banner-danger" : ""}`}>
            <div>
              <strong>{makemkvStatus?.level === "error" ? "MakeMKV needs attention" : "MakeMKV beta key warning"}</strong>
              <p>{makemkvStatus?.message}</p>
              {makemkvExpiryLabel ? <p>Published beta key expiry: {makemkvExpiryLabel}</p> : null}
              {makemkvStatus?.buildVersion ? <p>Installed MakeMKV build: {makemkvStatus.buildVersion}</p> : null}
              {makemkvStatus?.details?.map((detail, index) => (
                <p key={`makemkv-detail-${index}`}>{detail}</p>
              ))}
            </div>
            <div className="review-banner-actions">
              {makemkvStatus?.sourceUrl ? (
                <button disabled={busyAction !== null} onClick={() => void openPath(makemkvStatus.sourceUrl!)}>
                  Open Beta Key Page
                </button>
              ) : null}
              <button disabled={busyAction !== null} onClick={() => void loadConfigState()}>
                Refresh Status
              </button>
            </div>
          </div>
        ) : null}
        {reviewState?.rip?.needed ? (
          <div className="review-banner review-banner-danger">
            <div>
              <strong>Rip issue detected</strong>
              <p>{reviewState.rip.reason ?? "A serious rip issue was detected."}</p>
              {reviewState.rip.details?.map((detail, index) => (
                <p key={`rip-detail-${index}`}>{detail}</p>
              ))}
            </div>
            <div className="review-banner-actions">
              <button disabled={busyAction !== null} onClick={() => selectedJobId && void runAction("Re-identify", () => rerunIdentify(selectedJobId))}>
                Re-run TMDB
              </button>
            </div>
          </div>
        ) : null}
        {reviewState?.tmdb.needed ? (
          <div className="review-banner">
            <div>
              <strong>TMDB review needed</strong>
              <p>{reviewState.tmdb.reason ?? "This job needs a manual TMDB selection before the pipeline can continue."}</p>
              {isMultiMovie ? (
                <p>
                  Selected slots: {selectedMovies.length}/{requiredMovieSlots}
                </p>
              ) : null}
              {topTmdbCandidate ? <p>Top candidate: {tmdbCandidateDisplay(topTmdbCandidate)}</p> : null}
            </div>
            <div className="review-banner-actions">
              <button className="primary-button" disabled={busyAction !== null} onClick={() => openTmdbReviewModal()}>
                {tmdbCandidates.length === 0 ? "Search TMDB Manually" : "Review TMDB Candidates"}
              </button>
            </div>
          </div>
        ) : null}
        {reviewState?.mapping.needed ? (
          <div className="review-banner">
            <div>
              <strong>Override review recommended</strong>
              <p>{reviewState.mapping.reason ?? "Some bundle assignments are below the confidence gate and should be reviewed."}</p>
              {reviewState.mapping.low_confidence_count ? (
                <p>{reviewState.mapping.low_confidence_count} mapping(s) are below {(reviewState.mapping.threshold * 100).toFixed(0)}% confidence.</p>
              ) : null}
            </div>
            <div className="review-banner-actions">
              <button disabled={!ripTitleOptions.length || busyAction !== null} onClick={() => openGuidedReviewModal()}>
                Guided Review
              </button>
              <button disabled={!mappingOptions.length || busyAction !== null} onClick={() => openOverrideModal("map")}>
                Override Mapping
              </button>
              <button disabled={!mappingOptions.length || busyAction !== null} onClick={() => openOverrideModal("file")}>
                Override File
              </button>
              <button disabled={!splitPlanOptions.length || busyAction !== null} onClick={() => openOverrideModal("split")}>
                Override Split
              </button>
            </div>
          </div>
        ) : null}

        <div className="tabs">
          <button className={showSettingsTab ? "active" : ""} onClick={() => setActiveTab("settings")}>
            Settings
          </button>
          <button className={activeTab === "overview" ? "active" : ""} onClick={() => setActiveTab("overview")}>
            Overview
          </button>
          <button className={activeTab === "activity" ? "active" : ""} onClick={() => setActiveTab("activity")}>
            Activity
          </button>
          <button className={activeTab === "artifacts" ? "active" : ""} onClick={() => setActiveTab("artifacts")}>
            Artifacts
          </button>
          <button className={activeTab === "json" ? "active" : ""} onClick={() => setActiveTab("json")}>
            Raw JSON
          </button>
        </div>

        {showSettingsTab ? (
          <section className="panel settings-panel">
            <div className="section-header">
              <div>
                <h3>{configReady ? "Settings" : "First-run setup"}</h3>
                <p>{configState?.configPath ?? "Loading config path..."}</p>
              </div>
              <span className={`status-pill status-${configReady ? "done" : "error"}`}>{configReady ? "ready" : "setup required"}</span>
            </div>
            {configLoading ? (
              <div className="empty-state">Loading configuration…</div>
            ) : (
              <>
                <div className={`review-banner ${configReady ? "" : "review-banner-danger"}`}>
                  <div>
                    <strong>{configState?.validation.ok ? "Configuration looks ready" : "Configuration needs attention"}</strong>
                    <p>{configState?.validation.message ?? "Unable to validate configuration."}</p>
                  </div>
                  <div className="review-banner-actions">
                    <button disabled={busyAction !== null} onClick={() => void autodetectConfig()}>
                      Auto-detect
                    </button>
                  </div>
                </div>
                {showMakemkvBanner ? (
                  <div className={`review-banner ${makemkvStatus?.level === "error" ? "review-banner-danger" : ""}`}>
                    <div>
                      <strong>{makemkvStatus?.level === "error" ? "MakeMKV needs attention" : "MakeMKV beta key warning"}</strong>
                      <p>{makemkvStatus?.message}</p>
                      {makemkvExpiryLabel ? <p>Published beta key expiry: {makemkvExpiryLabel}</p> : null}
                    </div>
                    <div className="review-banner-actions">
                      {makemkvStatus?.sourceUrl ? (
                        <button disabled={busyAction !== null} onClick={() => void openPath(makemkvStatus.sourceUrl!)}>
                          Open Beta Key Page
                        </button>
                      ) : null}
                      <button disabled={busyAction !== null} onClick={() => void loadConfigState()}>
                        Refresh Status
                      </button>
                    </div>
                  </div>
                ) : null}
                <div className="panel-grid">
                  <section className="panel">
                    <div className="section-header">
                      <h3>Required settings</h3>
                    </div>
                    <div className="form-grid">
                      <label>
                        <span>TMDB API key</span>
                        <input
                          value={configDraft.tmdb_api_key ?? ""}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, tmdb_api_key: e.target.value }))}
                        />
                      </label>
                      <label>
                        <span>MakeMKV path</span>
                        <input
                          value={configDraft.makemkv_path ?? ""}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, makemkv_path: e.target.value }))}
                        />
                        <button type="button" onClick={() => void pickConfigPath("makemkv_path", "file", "Select MakeMKV executable")}>
                          Browse
                        </button>
                      </label>
                      <label>
                        <span>FFmpeg path</span>
                        <input
                          value={configDraft.ffmpeg_path ?? ""}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, ffmpeg_path: e.target.value }))}
                        />
                        <button type="button" onClick={() => void pickConfigPath("ffmpeg_path", "file", "Select FFmpeg executable")}>
                          Browse
                        </button>
                      </label>
                      <label>
                        <span>FFprobe path</span>
                        <input
                          value={configDraft.ffprobe_path ?? ""}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, ffprobe_path: e.target.value }))}
                        />
                        <button type="button" onClick={() => void pickConfigPath("ffprobe_path", "file", "Select FFprobe executable")}>
                          Browse
                        </button>
                      </label>
                      <label>
                        <span>Staging root</span>
                        <input
                          value={configDraft.staging_root ?? ""}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, staging_root: e.target.value }))}
                        />
                        <button type="button" onClick={() => void pickConfigPath("staging_root", "directory", "Select staging folder")}>
                          Browse
                        </button>
                      </label>
                      <label>
                        <span>NAS root</span>
                        <input
                          value={configDraft.nas_root ?? ""}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, nas_root: e.target.value }))}
                        />
                        <button type="button" onClick={() => void pickConfigPath("nas_root", "directory", "Select NAS/library root")}>
                          Browse
                        </button>
                      </label>
                    </div>
                  </section>
                  <section className="panel">
                    <div className="section-header">
                      <h3>Optional defaults</h3>
                    </div>
                    <div className="form-grid">
                      <label>
                        <span>Default order mode</span>
                        <select
                          value={configDraft.default_order_mode ?? "aired"}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, default_order_mode: e.target.value }))}
                        >
                          <option value="aired">Aired</option>
                          <option value="dvd">DVD</option>
                          <option value="absolute">Absolute</option>
                        </select>
                      </label>
                      <label>
                        <span>Collision policy</span>
                        <select
                          value={configDraft.collision_policy ?? "skip"}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, collision_policy: e.target.value }))}
                        >
                          <option value="skip">Skip</option>
                          <option value="overwrite">Overwrite</option>
                        </select>
                      </label>
                      <label>
                        <span>Titles to rip</span>
                        <select
                          value={configDraft.rip_title_selection ?? "auto"}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, rip_title_selection: e.target.value }))}
                        >
                          <option value="auto">Only what is needed (recommended)</option>
                          <option value="all">Every title on the disc</option>
                        </select>
                        <small>
                          Auto skips trailers, logos, and "play all" tracks, which is usually most of a disc.
                        </small>
                      </label>
                      <label>
                        <span>Eject when a rip finishes</span>
                        <select
                          value={configDraft.eject_after_rip ?? "false"}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, eject_after_rip: e.target.value }))}
                        >
                          <option value="false">Leave the disc in the drive</option>
                          <option value="true">Eject automatically</option>
                        </select>
                        <small>Useful with continuous mode when working through a stack of discs.</small>
                      </label>
                      <label>
                        <span>Verify NAS copies</span>
                        <select
                          value={configDraft.verify_transfers ?? "false"}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, verify_transfers: e.target.value }))}
                        >
                          <option value="false">Trust the copy (faster)</option>
                          <option value="true">Read back and compare checksums</option>
                        </select>
                        <small>
                          Every copy records a SHA-256 either way. Verifying reads the file back off the
                          NAS to compare it, which roughly doubles transfer time.
                        </small>
                      </label>
                    </div>
                    <div className="artifact-list">
                      <div className="artifact-row">
                        <div>
                          <strong>MakeMKV</strong>
                          <p>{configState?.dependencies.makemkv.path || "No path set"}</p>
                        </div>
                        <span className={`status-pill status-${configState?.dependencies.makemkv.exists ? "done" : "error"}`}>
                          {configState?.dependencies.makemkv.exists ? "found" : "missing"}
                        </span>
                      </div>
                      <div className="artifact-row">
                        <div>
                          <strong>FFmpeg</strong>
                          <p>{configState?.dependencies.ffmpeg.path || "No path set"}</p>
                        </div>
                        <span className={`status-pill status-${configState?.dependencies.ffmpeg.exists ? "done" : "error"}`}>
                          {configState?.dependencies.ffmpeg.exists ? "found" : "missing"}
                        </span>
                      </div>
                      <div className="artifact-row">
                        <div>
                          <strong>FFprobe</strong>
                          <p>{configState?.dependencies.ffprobe.path || "No path set"}</p>
                        </div>
                        <span className={`status-pill status-${configState?.dependencies.ffprobe.exists ? "done" : "error"}`}>
                          {configState?.dependencies.ffprobe.exists ? "found" : "missing"}
                        </span>
                      </div>
                    </div>
                  </section>
                </div>
                <div className="modal-actions">
                  <button disabled={busyAction !== null} onClick={() => void loadConfigState()}>
                    Reload
                  </button>
                  <button className="primary-button" disabled={busyAction !== null} onClick={() => void saveConfigFromDraft()}>
                    Save settings
                  </button>
                </div>
              </>
            )}
          </section>
        ) : null}

        {activeTab === "overview" && snapshot && !showSettingsTab ? (
          <div className="panel-grid">
            <section className="panel metrics">
              <div className="metric-card">
                <span>Status</span>
                <strong>{snapshot.job.status}</strong>
              </div>
              <div className="metric-card">
                <span>Scope</span>
                <strong>{snapshot.job.disc_scope ?? "unspecified"}</strong>
              </div>
              <div className="metric-card">
                <span>Season</span>
                <strong>{snapshot.job.season_number ?? "-"}</strong>
              </div>
              <div className="metric-card">
                <span>Outputs</span>
                <strong>{snapshot.outputs.length}</strong>
              </div>
            </section>

            <section className="panel">
              <div className="section-header">
                <h3>Current selection</h3>
              </div>
              {isMultiMovie ? (
                selectedMovies.length > 0 ? (
                  <div className="artifact-list">
                    {selectedMovies.map((slot) => (
                      <div className="artifact-row" key={`slot-${slot.slot_index}`}>
                        <div>
                          <strong>{`Slot ${slot.slot_index}`}</strong>
                          <p>{slot.title}{slot.year ? ` (${slot.year})` : ""}</p>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="empty-state">No movie slots selected yet.</div>
                )
              ) : snapshot.selected_media ? (
                <div className="artifact-list">
                  <div className="artifact-row">
                    <div>
                      <strong>{snapshot.selected_media.title}</strong>
                      <p>
                        {snapshot.selected_media.media_type.toUpperCase()}
                        {snapshot.selected_media.year ? ` • ${snapshot.selected_media.year}` : ""}
                      </p>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="empty-state">No selected media yet.</div>
              )}
            </section>

            <section className="panel">
              <div className="section-header">
                <h3>Rip titles</h3>
                <span>{snapshot.rip_titles.length}</span>
              </div>
              {snapshot.rip_titles.length > 0 ? (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Source</th>
                      <th>Duration</th>
                      <th>Chapters</th>
                      <th>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {snapshot.rip_titles.map((ripTitle) => (
                      <tr key={ripTitle.id}>
                        <td>{fileNameFromPath(ripTitle.source_file)}</td>
                        <td>{ripTitle.duration_seconds ? `${(ripTitle.duration_seconds / 60).toFixed(1)}m` : "—"}</td>
                        <td>{ripTitle.chapter_count ?? "—"}</td>
                        <td>
                          <button onClick={() => void openPath(ripTitle.source_file)}>
                            Play
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="empty-state">No rip titles yet.</div>
              )}
            </section>

            {showBundleOverview ? (
              <section className="panel">
                <div className="section-header">
                  <h3>Episode bundles</h3>
                </div>
                {bundles.length > 0 ? (
                  <table className="data-table">
                    <thead>
                        <tr>
                          <th>Source</th>
                          <th>Episodes</th>
                          <th>Titles</th>
                          <th>Confidence</th>
                          <th>Action</th>
                        </tr>
                      </thead>
                      <tbody>
                        {bundles.map((mapping) => (
                          <tr key={mapping.id} className={mapping.confidence != null && mapping.confidence < 0.85 ? "table-row-warning" : ""}>
                            <td>{mapping.source_file ? mapping.source_file.split(/[\\/]/).pop() : "—"}</td>
                            <td>{episodeLabel(mapping)}</td>
                            <td>{parseTitles(mapping.episode_titles_json).join(" / ") || "—"}</td>
                            <td>{mapping.confidence?.toFixed(2) ?? "—"}</td>
                            <td>
                              {mapping.source_file ? (
                                <button onClick={() => void openPath(mapping.source_file ?? "")}>
                                  Play
                                </button>
                              ) : null}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                  </table>
                ) : (
                  <div className="empty-state">No episode bundle mappings yet.</div>
                )}
              </section>
            ) : null}
          </div>
        ) : null}

        {activeTab === "activity" && snapshot && !showSettingsTab ? (
          <section className="panel">
            <div className="section-header">
              <h3>Recent activity</h3>
              <span>{snapshot.logs.length}</span>
            </div>
            {recentLogs.length > 0 ? (
              <div className="log-list">
                {recentLogs.map((logItem, index) => (
                  <div key={`${logItem.timestamp}-${index}`} className={`log-row ${isLogWarning(logItem) ? "is-warning" : ""}`}>
                    <div className="log-row-top">
                      <strong>{logItem.level}</strong>
                      <span>{formatRelativeTime(logItem.timestamp)}</span>
                    </div>
                    <p>{logItem.message}</p>
                  </div>
                ))}
              </div>
            ) : (
              <div className="empty-state">No activity logs yet.</div>
            )}
          </section>
        ) : null}

        {activeTab === "artifacts" && snapshot && !showSettingsTab ? (
          <section className="panel">
            <div className="section-header">
              <h3>Artifacts</h3>
            </div>
            <div className="artifact-list">
              <div className="artifact-row">
                <div>
                  <strong>final_outputs</strong>
                  <p>{snapshot.outputs.length > 0 ? `${snapshot.outputs.length} output file(s)` : "No finalized outputs yet."}</p>
                </div>
              </div>
              {snapshot.outputs.map((output) => (
                <div className="artifact-row" key={`output-${output.id}`}>
                  <div>
                    <strong>{fileNameFromPath(output.local_path)}</strong>
                    <p>{output.transfer_status}</p>
                  </div>
                  <button onClick={() => void openPath(output.local_path)}>Open</button>
                </div>
              ))}
            </div>
            <div className="artifact-list">
              {artifacts.length === 0 ? <div className="empty-state">No cached analysis artifacts yet.</div> : null}
              {artifacts.map((artifact, index) => {
                const looksLikePath = artifact.value.includes("\\") || artifact.value.includes("/");
                return (
                  <div className="artifact-row" key={`${artifact.kind}-${index}`}>
                    <div>
                      <strong>{artifact.kind}</strong>
                      <p>{artifact.value}</p>
                    </div>
                    {looksLikePath ? (
                      <button onClick={() => void openPath(artifact.value)}>Open</button>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </section>
        ) : null}

        {activeTab === "json" && snapshot && !showSettingsTab ? (
          <section className="panel">
            <div className="section-header">
              <h3>Raw job snapshot</h3>
            </div>
            <pre className="json-view">{JSON.stringify(snapshot, null, 2)}</pre>
          </section>
        ) : null}
      </main>

      {modal ? (
        <div className="modal-backdrop" onClick={() => setModal(null)}>
          <div className={`modal-card ${modal === "review" ? "modal-card-wide" : ""}`} onClick={(e) => e.stopPropagation()}>
            <h3>
              {modal === "tmdb"
                ? "Review TMDB Candidates"
                : modal === "review"
                  ? "Guided Manual Review"
                : modal === "map"
                  ? "Override Mapping"
                  : modal === "file"
                    ? "Override File"
                    : "Override Split"}
            </h3>
            <div className="form-grid single-column">
              {modal === "tmdb" ? (
                <>
                  <label>
                    <span>Search query</span>
                    <input
                      value={modalValues.searchQuery ?? ""}
                      onChange={(e) => setModalValues((v) => ({ ...v, searchQuery: e.target.value }))}
                    />
                  </label>
                  <button
                    type="button"
                    disabled={busyAction !== null || !(modalValues.searchQuery ?? "").trim()}
                    onClick={() => void runManualTmdbSearch()}
                  >
                    Search TMDB
                  </button>
                  {isMultiMovie ? (
                    <label>
                      <span>Movie slot</span>
                      <select
                        value={modalValues.slotIndex ?? "1"}
                        onChange={(e) => setModalValues((v) => ({ ...v, slotIndex: e.target.value }))}
                      >
                        {Array.from({ length: requiredMovieSlots }, (_, index) => index + 1).map((slot) => (
                          <option key={slot} value={slot}>
                            Slot {slot}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : null}
                  <label>
                    <span>Candidate</span>
                    <select
                      value={modalValues.tmdbId ?? ""}
                      onChange={(e) => {
                        const next = tmdbCandidates.find((candidate) => String(candidate.tmdb_id) === e.target.value);
                        setModalValues((v) => ({
                          ...v,
                          tmdbId: e.target.value,
                          mediaType: next?.media_type ?? "",
                        }));
                      }}
                      disabled={tmdbCandidates.length === 0}
                    >
                      {tmdbCandidates.length === 0 ? <option value="">No candidates yet</option> : null}
                      {tmdbCandidates.map((candidate) => (
                        <option key={`${candidate.media_type}-${candidate.tmdb_id}`} value={candidate.tmdb_id}>
                          {tmdbCandidateDisplay(candidate)}
                        </option>
                      ))}
                    </select>
                  </label>
                  {selectedTmdbCandidate ? (
                    <div className="candidate-card">
                      <strong>{selectedTmdbCandidate.title}</strong>
                      <p>{selectedTmdbCandidate.media_type.toUpperCase()} • {selectedTmdbCandidate.year ?? "Unknown year"}</p>
                      <p>Score: {(selectedTmdbCandidate.score * 100).toFixed(1)}%</p>
                      {parseScoreBreakdown(selectedTmdbCandidate.score_breakdown_json)?.query_source ? (
                        <p>Matched from: {String(parseScoreBreakdown(selectedTmdbCandidate.score_breakdown_json)?.query_source)}</p>
                      ) : null}
                    </div>
                  ) : (
                    <div className="empty-state">No TMDB candidates are available yet.</div>
                  )}
                  {isMultiMovie ? (
                    <div className="candidate-card">
                      <strong>Selected movie slots</strong>
                      {selectedMovies.length > 0 ? (
                        selectedMovies.map((slot: SelectedMovieSlot) => (
                          <p key={slot.slot_index}>
                            Slot {slot.slot_index}: {slot.title} {slot.year ? `(${slot.year})` : ""}
                          </p>
                        ))
                      ) : (
                        <p>No movie slots selected yet.</p>
                      )}
                    </div>
                  ) : null}
                </>
              ) : null}
              {modal === "review" ? (
                <>
                  <div className="candidate-card">
                    <strong>Batch file review</strong>
                    <p>Review each rip file in the table, mark it as episodes or ignore/extras, then save all assignments together.</p>
                  </div>
                  <div className="guided-review-toolbar">
                    <label>
                      <span>Range start</span>
                      <input
                        type="number"
                        value={modalValues.rangeStart ?? ""}
                        onChange={(e) => setModalValues((v) => ({ ...v, rangeStart: e.target.value }))}
                      />
                    </label>
                    <label>
                      <span>Range end</span>
                      <input
                        type="number"
                        value={modalValues.rangeEnd ?? ""}
                        onChange={(e) => setModalValues((v) => ({ ...v, rangeEnd: e.target.value }))}
                      />
                    </label>
                    <div className="guided-review-toolbar-actions">
                      <button
                        type="button"
                        disabled={busyAction !== null || !modalValues.rangeStart || !modalValues.rangeEnd}
                        onClick={() => void updateGuidedEpisodeRange()}
                      >
                        Update range & remap
                      </button>
                      <button
                        type="button"
                        className="primary-button"
                        disabled={busyAction !== null || guidedReviewRows.some((row) => row.status === "map" && (!row.episodeStart || !row.episodeEnd))}
                        onClick={() => void saveGuidedReviewAssignments()}
                      >
                        Save file assignments
                      </button>
                    </div>
                  </div>

                  {guidedReviewRows.length > 0 ? (
                    <div className="guided-review-table-wrap">
                      <table className="data-table guided-review-table">
                        <thead>
                          <tr>
                            <th>Play</th>
                            <th>File</th>
                            <th>Length</th>
                            <th>Ch</th>
                            <th>Confidence</th>
                            <th>Assignment</th>
                            <th>Episode start</th>
                            <th>Episode end</th>
                            <th>Reason</th>
                          </tr>
                        </thead>
                        <tbody>
                          {guidedReviewRows.map((row) => (
                            <tr
                              key={row.ripTitleId}
                              className={row.status === "ignore" || Number(row.confidence || 0) < 0.85 ? "table-row-warning" : ""}
                            >
                              <td>
                                <button type="button" onClick={() => void openPath(row.sourceFile)}>
                                  Play
                                </button>
                              </td>
                              <td>{fileNameFromPath(row.sourceFile)}</td>
                              <td>{row.durationMinutes}m</td>
                              <td>{row.chapterCount}</td>
                              <td>{row.confidence}</td>
                              <td>
                                <select
                                  value={row.status}
                                  onChange={(e) => {
                                    const nextStatus = e.target.value as "map" | "ignore";
                                    setGuidedReviewRows((current) => current.map((candidate) => (
                                      candidate.ripTitleId === row.ripTitleId
                                        ? {
                                            ...candidate,
                                            status: nextStatus,
                                            episodeStart: nextStatus === "ignore" ? "" : (candidate.episodeStart || modalValues.rangeStart || episodeOptions[0]?.value || ""),
                                            episodeEnd: nextStatus === "ignore" ? "" : (candidate.episodeEnd || modalValues.rangeStart || episodeOptions[0]?.value || ""),
                                          }
                                        : candidate
                                    )));
                                  }}
                                >
                                  <option value="map">Episodes</option>
                                  <option value="ignore">Ignore / Extras</option>
                                </select>
                              </td>
                              <td>
                                <select
                                  value={row.episodeStart}
                                  disabled={row.status === "ignore"}
                                  onChange={(e) => {
                                    const value = e.target.value;
                                    setGuidedReviewRows((current) => current.map((candidate) => (
                                      candidate.ripTitleId === row.ripTitleId
                                        ? { ...candidate, episodeStart: value }
                                        : candidate
                                    )));
                                  }}
                                >
                                  <option value="">—</option>
                                  {episodeOptions.map((option) => (
                                    <option key={option.value} value={option.value}>
                                      {option.label}
                                    </option>
                                  ))}
                                </select>
                              </td>
                              <td>
                                <select
                                  value={row.episodeEnd}
                                  disabled={row.status === "ignore"}
                                  onChange={(e) => {
                                    const value = e.target.value;
                                    setGuidedReviewRows((current) => current.map((candidate) => (
                                      candidate.ripTitleId === row.ripTitleId
                                        ? { ...candidate, episodeEnd: value }
                                        : candidate
                                    )));
                                  }}
                                >
                                  <option value="">—</option>
                                  {episodeOptions.map((option) => (
                                    <option key={option.value} value={option.value}>
                                      {option.label}
                                    </option>
                                  ))}
                                </select>
                              </td>
                              <td>{row.reason || "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <div className="empty-state">No rip files are available yet.</div>
                  )}

                  <div className="candidate-card">
                    <strong>Split verification</strong>
                    <p>
                      After saving file assignments, review the split plan table below and adjust any segment start/end times before saving.
                    </p>
                  </div>
                  {guidedSplitDrafts.length > 0 ? (
                    <>
                      <div className="guided-review-table-wrap">
                        <table className="data-table guided-review-table">
                          <thead>
                            <tr>
                              <th>Play</th>
                              <th>File</th>
                              <th>Part</th>
                              <th>Start seconds</th>
                              <th>End seconds</th>
                            </tr>
                          </thead>
                          <tbody>
                            {guidedSplitDrafts.map((draft) => (
                              <tr key={draft.splitPlanId}>
                                <td>
                                  <button type="button" onClick={() => void openPath(draft.sourceFile)}>
                                    Play
                                  </button>
                                </td>
                                <td>{fileNameFromPath(draft.sourceFile)}</td>
                                <td>{draft.segmentIndex}</td>
                                <td>
                                  <input
                                    value={draft.startSeconds}
                                    onChange={(e) => {
                                      const value = e.target.value;
                                      setGuidedSplitDrafts((current) => current.map((candidate) => (
                                        candidate.splitPlanId === draft.splitPlanId
                                          ? { ...candidate, startSeconds: value }
                                          : candidate
                                      )));
                                    }}
                                  />
                                </td>
                                <td>
                                  <input
                                    value={draft.endSeconds}
                                    onChange={(e) => {
                                      const value = e.target.value;
                                      setGuidedSplitDrafts((current) => current.map((candidate) => (
                                        candidate.splitPlanId === draft.splitPlanId
                                          ? { ...candidate, endSeconds: value }
                                          : candidate
                                      )));
                                    }}
                                  />
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      <button
                        type="button"
                        className="primary-button"
                        disabled={busyAction !== null}
                        onClick={() => void saveGuidedSplitDrafts()}
                      >
                        Save split times
                      </button>
                    </>
                  ) : (
                    <div className="empty-state">No split segments yet. Save file assignments first if a file should be split.</div>
                  )}
                </>
              ) : null}
              {modal === "map" ? (
                <>
                  <label>
                    <span>Bundle mapping</span>
                    <select value={modalValues.mappingId ?? ""} onChange={(e) => setModalValues((v) => ({ ...v, mappingId: e.target.value }))}>
                      {mappingOptions.map((mapping) => (
                        <option key={mapping.id} value={mapping.id}>
                          {mappingDisplay(mapping)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Episode start</span>
                    <select value={modalValues.episodeStart ?? ""} onChange={(e) => setModalValues((v) => ({ ...v, episodeStart: e.target.value }))}>
                      {episodeOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Episode end</span>
                    <select value={modalValues.episodeEnd ?? ""} onChange={(e) => setModalValues((v) => ({ ...v, episodeEnd: e.target.value }))}>
                      {episodeOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                </>
              ) : null}
              {modal === "file" ? (
                <>
                  <label>
                    <span>Bundle mapping</span>
                    <select value={modalValues.mappingId ?? ""} onChange={(e) => setModalValues((v) => ({ ...v, mappingId: e.target.value }))}>
                      {mappingOptions.map((mapping) => (
                        <option key={mapping.id} value={mapping.id}>
                          {mappingDisplay(mapping)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Source file</span>
                    <select value={modalValues.ripTitleId ?? ""} onChange={(e) => setModalValues((v) => ({ ...v, ripTitleId: e.target.value }))}>
                      {ripTitleOptions.map((ripTitle) => (
                        <option key={ripTitle.id} value={ripTitle.id}>
                          {ripTitleDisplay(ripTitle)}
                        </option>
                      ))}
                    </select>
                  </label>
                </>
              ) : null}
              {modal === "split" ? (
                <>
                  <label>
                    <span>Split plan</span>
                    <select value={modalValues.splitPlanId ?? ""} onChange={(e) => {
                      const next = splitPlanOptions.find((plan) => String(plan.id) === e.target.value);
                      setModalValues((v) => ({
                        ...v,
                        splitPlanId: e.target.value,
                        startSeconds: next?.start_seconds != null ? String(next.start_seconds) : "",
                        endSeconds: next?.end_seconds != null ? String(next.end_seconds) : "",
                      }));
                    }}>
                      {splitPlanOptions.map((plan) => (
                        <option key={plan.id} value={plan.id}>
                          {`${plan.id} • ${fileNameFromPath(plan.source_file)} • part ${plan.segment_index}`}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Start seconds</span>
                    <input value={modalValues.startSeconds ?? ""} onChange={(e) => setModalValues((v) => ({ ...v, startSeconds: e.target.value }))} />
                  </label>
                  <label>
                    <span>End seconds</span>
                    <input value={modalValues.endSeconds ?? ""} onChange={(e) => setModalValues((v) => ({ ...v, endSeconds: e.target.value }))} />
                  </label>
                </>
              ) : null}
            </div>
            <div className="modal-actions">
              <button onClick={() => { setModal(null); setModalJobId(null); }}>Close</button>
              {modal !== "review" ? (
                <button
                  className="primary-button"
                  disabled={modal === "tmdb" && !selectedTmdbCandidate}
                  onClick={() => void submitModalAction()}
                >
                  {modal === "tmdb" ? "Select" : "Apply"}
                </button>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
