import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  detectDisc,
  analyzeMenu,
  getReclaimableSpace,
  reclaimCompletedJobs,
  setWindowTheme,
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
  searchTvShows,
  getTvShowSeasons,
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
import {
  BOOLEAN_CONFIG_KEYS,
  discNumberFromLabel,
  seasonFromLabel,
  showQueryFromLabel,
  suggestEpisodeRange,
  DRIVE_CARDS_STORAGE_KEY,
  buildDriveCardState,
  deserializeDriveCards,
  serializeDriveCards,
  MOVIE_PIPELINE_STAGES,
  TV_PIPELINE_STAGES,
  buildConfigDraft,
  buildGuidedReviewRows,
  buildGuidedSplitDrafts,
  coerceConfigDraft,
  episodeLabel,
  fileNameFromPath,
  formatBytes,
  formatCalendarDate,
  formatCompletionSince,
  formatDurationSince,
  formatEta,
  formatRelativeTime,
  getActivityState,
  getHeroSubtitle,
  getPipelineStageIndex,
  getPipelineStages,
  getProgressPercent,
  isLogWarning,
  mappingDisplay,
  movieSlotCount,
  normalizeStartRequest,
  parseScoreBreakdown,
  parseTitles,
  ripTitleDisplay,
  tmdbCandidateDisplay,
} from "./lib";
import type { DriveCardState, GuidedReviewRowDraft, GuidedSplitDraft } from "./lib";
import type { TvShowDetail, TvShowResult } from "./types";
import type { DiscDrive, EpisodeMapping, JobLog, JobSnapshot, JobStatus, JobSummary, RipTitle, RuntimeConfigState, SelectedMovieSlot, SplitPlan, StartJobRequest, TmdbCandidate } from "./types";

const POLL_MS = 3000;

type QuickAction = {
  key: string;
  label: string;
  onClick: () => void;
  disabled: boolean;
  tone?: "default" | "danger";
};


/**
 * The show-lookup panel's state for one drive card.
 *
 * Deliberately separate from DriveCardState, which is persisted to
 * localStorage: this is all transient working state for choosing a show, and
 * none of it should come back when the app reopens weeks later.
 */
type ShowLookupState = {
  query: string;
  results: TvShowResult[] | null;
  detail: TvShowDetail | null;
  discsInSet: number | null;
  busy: boolean;
  error: string | null;
};

function emptyLookup(query: string): ShowLookupState {
  return { query, results: null, detail: null, discsInSet: null, busy: false, error: null };
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
  const [showLookups, setShowLookups] = useState<Record<string, ShowLookupState>>({});

  // Until the user types, the lookup box tracks the disc label -- cleaned the
  // same way the backend cleans it, so what they see is what will be searched.
  const lookupFor = (card: DriveCardState): ShowLookupState =>
    showLookups[card.id] ?? emptyLookup(showQueryFromLabel(card.form.discLabel));

  const patchLookup = (cardId: string, patch: Partial<ShowLookupState>, fallbackQuery = "") => {
    setShowLookups((previous) => {
      const base = previous[cardId] ?? emptyLookup(fallbackQuery);
      return { ...previous, [cardId]: { ...base, ...patch } };
    });
  };

  const runShowSearch = async (card: DriveCardState) => {
    const state = lookupFor(card);
    const query = state.query.trim();
    if (!query) return;
    patchLookup(card.id, { busy: true, error: null, query }, query);
    try {
      const found = await searchTvShows(query);
      patchLookup(
        card.id,
        {
          busy: false,
          results: found.results,
          detail: null,
          error: found.results.length ? null : `No show on TMDB matches "${query}".`,
        },
        query,
      );
    } catch (error) {
      patchLookup(card.id, { busy: false, error: String(error) }, query);
    }
  };

  const chooseShow = async (card: DriveCardState, show: TvShowResult) => {
    patchLookup(card.id, { busy: true, error: null, results: null });
    try {
      const detail = await getTvShowSeasons(show.tmdb_id);
      patchLookup(card.id, { busy: false, detail, query: show.name });
      updateDriveCard(card.id, (value) => ({
        ...value,
        form: { ...value.form, tmdbShowId: show.tmdb_id },
      }));
      // A label that named its season already answered the next question.
      const season = seasonFromLabel(card.form.discLabel);
      if (season !== null && detail.seasons.some((entry) => entry.season_number === season)) {
        applySeason(card, detail, season, null);
      }
    } catch (error) {
      patchLookup(card.id, { busy: false, error: String(error) });
    }
  };

  const applySeason = (
    card: DriveCardState,
    detail: TvShowDetail,
    seasonNumber: number,
    discsInSet: number | null,
  ) => {
    const season = detail.seasons.find((entry) => entry.season_number === seasonNumber);
    const range = season
      ? suggestEpisodeRange(season.episode_count, discNumberFromLabel(card.form.discLabel), discsInSet)
      : null;
    updateDriveCard(card.id, (value) => ({
      ...value,
      form: {
        ...value.form,
        seasonNumber,
        // Only narrow the scope when there is a real range to narrow it to.
        discScope: range ? "partial_season" : value.form.discScope,
        episodeRangeStart: range ? range.start : value.form.episodeRangeStart,
        episodeRangeEnd: range ? range.end : value.form.episodeRangeEnd,
      },
    }));
  };

  const [driveCards, setDriveCards] = useState<DriveCardState[]>(() =>
    deserializeDriveCards(window.localStorage.getItem(DRIVE_CARDS_STORAGE_KEY)),
  );
  const [modal, setModal] = useState<null | "tmdb" | "map" | "file" | "split" | "review">(null);
  const [modalJobId, setModalJobId] = useState<string | null>(null);
  const [modalValues, setModalValues] = useState<Record<string, string>>({});
  const [guidedReviewRows, setGuidedReviewRows] = useState<GuidedReviewRowDraft[]>([]);
  const [guidedSplitDrafts, setGuidedSplitDrafts] = useState<GuidedSplitDraft[]>([]);
  const [actionsMenuOpen, setActionsMenuOpen] = useState(false);
  const [reclaimable, setReclaimable] = useState<{ total_bytes: number; job_count: number } | null>(null);
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const stored = window.localStorage.getItem("autorippr-theme");
    if (stored === "light" || stored === "dark") return stored;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
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

  async function refreshReclaimable() {
    try {
      const summary = await getReclaimableSpace();
      setReclaimable({ total_bytes: summary.total_bytes, job_count: summary.job_count });
    } catch {
      // Space reporting is advisory; never surface it as a job error.
    }
  }

  async function reclaimAll() {
    const amount = formatBytes(reclaimable?.total_bytes) ?? "space";
    const count = reclaimable?.job_count ?? 0;
    const confirmed = window.confirm(
      `Free ${amount} by deleting the staged files of ${count} completed job(s)?

` +
        "Their NAS copies and job records are kept. Re-ripping the disc would be needed to get the local files back.",
    );
    if (!confirmed) return;
    await runAction("Reclaiming space", async () => {
      await reclaimCompletedJobs();
      await refreshReclaimable();
    });
  }

  async function confirmDeleteJob() {
    if (!selectedJobId) return;
    const job = jobs.find((entry) => entry.id === selectedJobId);
    const label = job?.disc_label || "this job";
    const size = localArtifactSize ? ` and ${localArtifactSize} of staged files` : "";
    const confirmed = window.confirm(
      `Delete ${label}?\n\n` +
        `This removes the job, its history${size}, and the record of what it produced. ` +
        "Anything already copied to the NAS is left alone. This cannot be undone.",
    );
    if (!confirmed) return;
    await runSelectedAction("Delete", deleteJob);
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

  // Drive setup is a property of the machine, not of a session. Losing it on
  // every reload meant re-adding and re-assigning a second drive by hand.
  useEffect(() => {
    try {
      window.localStorage.setItem(DRIVE_CARDS_STORAGE_KEY, serializeDriveCards(driveCards));
    } catch {
      // Storage full or blocked: the app still works, it just forgets.
    }
  }, [driveCards]);

  useEffect(() => {
    busyActionRef.current = busyAction;
  }, [busyAction]);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    window.localStorage.setItem("autorippr-theme", theme);
    // The title bar is drawn by the OS and does not follow the page, so it has
    // to be told separately or a dark app keeps a light title bar.
    void setWindowTheme(theme).catch(() => {
      // Non-Windows, or an older webview: the chrome simply stays as it is.
    });
  }, [theme]);

  useEffect(() => {
    void loadConfigState();
  }, []);

  useEffect(() => {
    if (!configReady) return;
    void refreshJobs();
    void refreshAllDriveCards();
    void refreshReclaimable();
    const timer = window.setInterval(() => void refreshJobs(), POLL_MS);
    const spaceTimer = window.setInterval(() => void refreshReclaimable(), 30000);
    return () => {
      window.clearInterval(timer);
      window.clearInterval(spaceTimer);
    };
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

          // Only a job that is still in flight should suppress a new one.
          // Matching finished jobs too meant that re-inserting a disc you had
          // already ripped did nothing at all, reported as "already has an
          // active job" -- a silent stall, which is the worst outcome when
          // nobody is watching the screen.
          const matchingExistingJob = jobsRef.current.find(
            (job) =>
              job.disc_label === disc.volume_label &&
              (job.optical_drive ?? "").toUpperCase() === disc.drive.toUpperCase() &&
              job.status !== "done" &&
              job.status !== "error",
          );
          if (matchingExistingJob) {
            lastAutoStartedDiscRef.current[card.id] = signature;
            setDriveCards((cards) =>
              cards.map((entry) =>
                entry.id === card.id
                  ? {
                      ...entry,
                      continuousStatus: `Disc already has a job in progress (${matchingExistingJob.status}): ${disc.volume_label}`,
                    }
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

  const localArtifactSize = useMemo(
    () => formatBytes(snapshot?.storage?.job_bytes),
    [snapshot],
  );

  const stagingFree = useMemo(() => formatBytes(snapshot?.storage?.free_bytes), [snapshot]);

  /** Fraction of the staging volume in use, for the capacity bar. */
  const stagingUsedFraction = useMemo(() => {
    const total = snapshot?.storage?.total_bytes ?? 0;
    const free = snapshot?.storage?.free_bytes ?? 0;
    if (!total) return null;
    return Math.min(1, Math.max(0, (total - free) / total));
  }, [snapshot]);

  const jobsAwaitingReview = useMemo(
    () => jobs.filter((job) => Boolean(job.awaiting_review)),
    [jobs],
  );

  // The taskbar is the only part of the app visible when it is not focused,
  // so the count of jobs waiting on a person belongs in the window title.
  useEffect(() => {
    const pending = jobsAwaitingReview.length;
    document.title = pending ? `(${pending}) Auto-Ripper — needs you` : "Auto-Ripper";
  }, [jobsAwaitingReview]);

  const featureRuntimeLabel = useMemo(() => {
    const durations = (snapshot?.rip_titles ?? [])
      .map((title) => title.duration_seconds ?? 0)
      .filter((seconds) => seconds > 0);
    if (!durations.length) return "-";
    const minutes = Math.max(...durations) / 60;
    if (minutes < 60) return `${Math.round(minutes)}m`;
    return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`;
  }, [snapshot]);

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

  /**
   * Whether the bar should animate: the job is in a stage that does work and
   * is not parked waiting for a person. Derived from state rather than the
   * activity label, which is prose and changes wording.
   */
  const jobIsProgressing = Boolean(
    snapshot &&
      !reviewNeeded &&
      !snapshot.job.awaiting_review &&
      ["queued", "ripping", "identifying", "mapping", "splitting", "renaming", "copying"].includes(
        snapshot.job.status,
      ),
  );
  const showBundleOverview = snapshot?.job.media_type === "tv";
  const isMultiMovie = snapshot?.job.media_type === "movie" && snapshot.job.movie_mode !== "single";
  const requiredMovieSlots = movieSlotCount(snapshot?.job.movie_mode);
  const activityState = snapshot ? getActivityState(snapshot.job.status, reviewNeeded || ripReviewNeeded, lastActivityAt) : "Monitoring";
  const progressPercent = snapshot ? Math.round((progressState?.overall_fraction ?? (getProgressPercent(snapshot.job.status, pipelineStages) / 100)) * 100) : 0;
  const currentStageIndex = snapshot ? getPipelineStageIndex(snapshot.job.status, pipelineStages, snapshot.job.current_stage) : 0;
  const progressEta = formatEta(progressState?.eta_seconds);
  const showSettingsTab = activeTab === "settings" || !configReady;

  useEffect(() => {
    if (!modal) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setModal(null);
        setModalJobId(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [modal]);

  useEffect(() => {
    if (!showSettingsTab || !configReady) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setActiveTab("overview");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showSettingsTab, configReady]);
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
        label: localArtifactSize ? `Free ${localArtifactSize}` : "Clear Local Artifacts",
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
        label: "Delete job",
        disabled,
        tone: "danger",
        onClick: () => void confirmDeleteJob(),
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
            <p>{configReady ? "Ready" : "Setup required"}</p>
          </div>
          <div className="toolbar-actions">
            <button
              className="icon-button"
              title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
              aria-label="Toggle colour theme"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? "\u2600" : "\u263D"}
            </button>
            <button
              className={`icon-button ${showSettingsTab ? "is-active" : ""}`}
              title={showSettingsTab ? "Back to jobs" : "Settings"}
              aria-label="Settings"
              disabled={!configReady && showSettingsTab}
              onClick={() => setActiveTab(showSettingsTab ? "overview" : "settings")}
            >
              {"\u2699"}
            </button>
          </div>
        </div>

        <div className="sidebar-scroll">
        {!configReady ? (
          <div className="review-banner review-banner-danger">
            <div>
              <strong>Setup required</strong>
              <p>Add your tool paths and TMDB key before ripping.</p>
            </div>
            <div className="review-banner-actions">
              <button className="primary-button" onClick={() => setActiveTab("settings")}>
                Open Settings
              </button>
            </div>
          </div>
        ) : makemkvBlocking ? (
          <div className="review-banner">
            <div>
              <strong>MakeMKV needs attention</strong>
              <p>{makemkvStatus?.message}</p>
            </div>
          </div>
        ) : null}

        <section className="start-card">
          <div className="section-header">
            <h2>Start new disc</h2>
            <button type="button" disabled={busyAction !== null || !configReady} onClick={addDriveCard}>
              + Drive
            </button>
          </div>
          <div className="drive-cards">
            {driveCards.map((card, index) => {
              const lookup = lookupFor(card);
              const labelDisc = discNumberFromLabel(card.form.discLabel);
              const chosenSeason = lookup.detail?.seasons.find(
                (entry) => entry.season_number === card.form.seasonNumber,
              );
              const showDiscScope = card.form.mediaType === "tv";
              const showSeasonNumber = card.form.mediaType === "tv";
              const showEpisodeRange = card.form.mediaType === "tv" && card.form.discScope === "partial_season";
              const showMovieMode = card.form.mediaType === "movie";
              return (
                <div key={card.id} className="drive-card">
                  <div className="drive-card-header">
                    <div className="drive-card-title">
                      <h3>{`Drive ${index + 1}`}</h3>
                      <span className="drive-card-target">
                        {card.form.opticalDrive ? card.form.opticalDrive : "auto"}
                      </span>
                    </div>
                    <div className="drive-card-actions">
                      <button
                        type="button"
                        className={card.continuousMode ? "primary-button" : undefined}
                        aria-pressed={card.continuousMode}
                        aria-label={`Continuous mode for drive ${index + 1}`}
                        disabled={busyAction !== null || !configReady || (makemkvBlocking && !card.continuousMode)}
                        onClick={() =>
                          updateDriveCard(card.id, (value) => ({
                            ...value,
                            continuousMode: !value.continuousMode,
                            continuousStatus: !value.continuousMode ? "Continuous mode on — waiting for disc" : null,
                          }))
                        }
                      >
                        {card.continuousMode ? "Continuous on" : "Continuous off"}
                      </button>
                      {driveCards.length > 1 ? (
                        <button
                          type="button"
                          className="icon-button"
                          title="Remove this drive"
                          aria-label={`Remove drive ${index + 1}`}
                          disabled={busyAction !== null}
                          onClick={() => removeDriveCard(card.id)}
                        >
                          {"×"}
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
                      <button
                        type="button"
                        aria-label={`Refresh disc in drive ${index + 1}`}
                        disabled={!configReady}
                        onClick={() => void refreshDriveCard(card.id)}
                      >
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
                    {card.form.mediaType === "tv" ? (
                      <div className="show-lookup">
                        <label className="show-lookup-query">
                          <span>Show</span>
                          <div className="show-lookup-row">
                            <input
                              type="text"
                              value={lookup.query}
                              placeholder="Search TMDB for the series"
                              onChange={(e) => patchLookup(card.id, { query: e.target.value }, e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  void runShowSearch(card);
                                }
                              }}
                            />
                            <button
                              type="button"
                              className="secondary-button"
                              disabled={lookup.busy || !lookup.query.trim()}
                              onClick={() => void runShowSearch(card)}
                            >
                              {lookup.busy ? "Looking up..." : "Look up"}
                            </button>
                          </div>
                        </label>

                        {lookup.error ? <p className="show-lookup-error">{lookup.error}</p> : null}

                        {lookup.results?.length ? (
                          <ul className="show-lookup-results">
                            {lookup.results.slice(0, 6).map((show) => (
                              <li key={show.tmdb_id}>
                                <button type="button" onClick={() => void chooseShow(card, show)}>
                                  <span className="show-name">{show.name}</span>
                                  {show.year ? <span className="show-year">{show.year}</span> : null}
                                </button>
                              </li>
                            ))}
                          </ul>
                        ) : null}

                        {lookup.detail ? (
                          <div className="show-lookup-detail">
                            <p className="show-lookup-chosen">
                              <strong>{lookup.detail.name}</strong>
                              {lookup.detail.year ? ` (${lookup.detail.year})` : ""} &middot;{" "}
                              {lookup.detail.total_episodes} episodes
                            </p>
                            <div className="show-lookup-row">
                              <label>
                                <span>Season</span>
                                <select
                                  value={card.form.seasonNumber ?? ""}
                                  onChange={(e) =>
                                    applySeason(card, lookup.detail!, Number(e.target.value), lookup.discsInSet)
                                  }
                                >
                                  <option value="">Choose...</option>
                                  {lookup.detail.seasons.map((season) => (
                                    <option key={season.season_number} value={season.season_number}>
                                      {season.is_specials ? "Specials" : `Season ${season.season_number}`}
                                      {` \u2014 ${season.episode_count} episodes`}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <label>
                                <span>Discs in set</span>
                                <input
                                  type="number"
                                  min={1}
                                  value={lookup.discsInSet ?? ""}
                                  placeholder="e.g. 4"
                                  onChange={(e) => {
                                    const discs = e.target.value ? Number(e.target.value) : null;
                                    patchLookup(card.id, { discsInSet: discs });
                                    if (card.form.seasonNumber != null && lookup.detail) {
                                      applySeason(card, lookup.detail, card.form.seasonNumber, discs);
                                    }
                                  }}
                                />
                              </label>
                            </div>
                            {chosenSeason ? (
                              <p className="show-lookup-hint">
                                {chosenSeason.is_specials
                                  ? `${chosenSeason.episode_count} specials. These are a grab-bag rather than a run, so pick the episodes by hand.`
                                  : labelDisc && lookup.discsInSet
                                    ? `Disc ${labelDisc} of ${lookup.discsInSet} \u2014 episodes ${card.form.episodeRangeStart ?? "?"}\u2013${card.form.episodeRangeEnd ?? "?"} of ${chosenSeason.episode_count}.`
                                    : labelDisc
                                      ? `The label says disc ${labelDisc}. Say how many discs the set has and the episode range fills itself in.`
                                      : `${chosenSeason.episode_count} episodes. Choose a scope below.`}
                              </p>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    ) : null}
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
                    aria-label={`Start ripping on drive ${index + 1}`}
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
            <h2>Jobs {jobs.length ? `(${jobs.length})` : ""}</h2>
            <button
              className="icon-button"
              title="Refresh jobs"
              aria-label="Refresh jobs"
              disabled={!configReady || busyAction !== null}
              onClick={() => void refreshJobs()}
            >
              {"\u21BB"}
            </button>
          </div>
          {jobsAwaitingReview.length > 0 ? (
            <button
              className="review-summary"
              onClick={() => setSelectedJobId(jobsAwaitingReview[0].id)}
            >
              {jobsAwaitingReview.length} job{jobsAwaitingReview.length === 1 ? "" : "s"} waiting on you
            </button>
          ) : null}
          {!configReady ? (
            <div className="empty-state">Finish setup before jobs can run.</div>
          ) : jobs.length === 0 ? (
            <div className="empty-state">No jobs yet. Insert a disc to begin.</div>
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
                  <span className={`status-pill ${job.awaiting_review ? "status-review" : `status-${job.status}`}`}>
                    {job.awaiting_review ? "needs you" : job.status}
                  </span>
                </div>
                <div className="job-row-meta">
                  <span>
                    {job.media_type === "tv" ? "TV" : "Movie"}
                    {job.optical_drive ? ` \u00B7 ${job.optical_drive}` : ""}
                  </span>
                  <span>{formatRelativeTime(job.updated_at)}</span>
                </div>
              </button>
            ))}
          </div>
        </section>
        </div>

        {snapshot?.storage?.total_bytes ? (
          <div
            className={`storage-strip ${
              stagingUsedFraction !== null && stagingUsedFraction > 0.9 ? "is-critical" : ""
            }`}
            title={snapshot.storage.staging_root}
          >
            <div className="storage-strip-top">
              <span>Staging</span>
              <strong>{stagingFree ? `${stagingFree} free` : "unknown"}</strong>
            </div>
            <div className="storage-bar-track">
              <div
                className="storage-bar-fill"
                style={{ width: `${Math.round((stagingUsedFraction ?? 0) * 100)}%` }}
              />
            </div>
            {reclaimable && reclaimable.total_bytes > 0 ? (
              <button
                className="storage-reclaim"
                disabled={busyAction !== null}
                onClick={() => void reclaimAll()}
              >
                Reclaim {formatBytes(reclaimable.total_bytes)} from {reclaimable.job_count} finished job
                {reclaimable.job_count === 1 ? "" : "s"}
              </button>
            ) : null}
          </div>
        ) : null}
      </aside>

      <main className="content">
        {!showSettingsTab && snapshot ? (
          <section className="panel progress-panel hero-panel">
            <div className="hero-top">
              <div className="toolbar-title hero-title-block">
                <h2 className="hero-title">{snapshot.job.disc_label}</h2>
                {heroSubtitle ? <p className="hero-subtitle">{heroSubtitle}</p> : null}
                <p className="hero-meta">
                  <span>{activityState}</span>
                  <span className="hero-meta-sep">|</span>
                  <span>{formatRelativeTime(lastActivityAt)}</span>
                  {snapshot.job.optical_drive ? (
                    <>
                      <span className="hero-meta-sep">|</span>
                      <span>Drive {snapshot.job.optical_drive}</span>
                    </>
                  ) : null}
                  {selectedTmdbSource ? (
                    <>
                      <span className="hero-meta-sep">|</span>
                      <span>via {String(selectedTmdbSource)}</span>
                    </>
                  ) : null}
                </p>
              </div>
              <div className="hero-actions">
                {primaryActions.map((action, index) => (
                  <button
                    key={action.key}
                    className={
                      index === 0
                        ? "primary-button"
                        : action.tone === "danger"
                          ? "danger-button"
                          : undefined
                    }
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
              {progressState?.detail && snapshot.job.status !== "done" ? (
                <div className="progress-detail">
                  <span>{progressState.detail}</span>
                  {progressState.rate_mb_s ? (
                    <>
                      <span className="progress-detail-sep">|</span>
                      <span>{progressState.rate_mb_s.toFixed(1)} MB/s</span>
                    </>
                  ) : null}
                  {progressEta ? (
                    <>
                      <span className="progress-detail-sep">|</span>
                      <span>{progressEta}</span>
                    </>
                  ) : null}
                  {progressState.title_count && progressState.title_count > 1 ? (
                    <>
                      <span className="progress-detail-sep">|</span>
                      <span>
                        title {progressState.title_index} of {progressState.title_count}
                      </span>
                    </>
                  ) : null}
                </div>
              ) : null}
              {snapshot.job.status !== "done" &&
              (stageDuration || stageProgressPercent !== null) ? (
                <div className="stage-substatus">
                  {stageDuration ? <span>{stageDuration}</span> : null}
                  {stageProgressPercent !== null ? <span>{stageProgressPercent}% through stage</span> : null}
                </div>
              ) : null}
              <div
                className="progress-bar-track"
                role="progressbar"
                aria-valuenow={progressPercent}
                aria-valuemin={0}
                aria-valuemax={100}
              >
                <div
                  className={`progress-bar-fill ${
                    snapshot.job.status === "done"
                      ? "is-done"
                      : snapshot.job.status === "error"
                        ? "is-error"
                        : jobIsProgressing
                          ? "is-active"
                          : ""
                  }`}
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
              <div className="stage-list">
                {pipelineStages.map((stage, stageIndex) => {
                  const completed = currentStageIndex >= stageIndex;
                  const active = snapshot.job.status === stage;
                  return (
                    <Fragment key={stage}>
                      {stageIndex > 0 ? (
                        <span className={`stage-connector ${completed ? "is-complete" : ""}`} />
                      ) : null}
                      <span className={`stage-step ${completed ? "is-complete" : ""} ${active ? "is-active" : ""}`}>
                        <span className="stage-step-dot" />
                        {stage}
                      </span>
                    </Fragment>
                  );
                })}
              </div>
            </div>
          </section>
        ) : null}

        {error ? <div className="error-banner">{error}</div> : null}
        {!showSettingsTab && showMakemkvBanner ? (
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
        {!showSettingsTab && reviewState?.rip?.needed ? (
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
        {!showSettingsTab && reviewState?.tmdb.needed ? (
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
        {!showSettingsTab && reviewState?.mapping.needed ? (
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

        {!showSettingsTab ? (
          <div className="tabs">
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
        ) : null}

        {showSettingsTab ? (
          <section className="panel settings-panel">
            <div className="settings-header">
              <div className="settings-header-title">
                {configReady ? (
                  <button
                    className="icon-button"
                    aria-label="Back to jobs"
                    title="Back to jobs (Esc)"
                    onClick={() => setActiveTab("overview")}
                  >
                    {"←"}
                  </button>
                ) : null}
                <div>
                  <h3>{configReady ? "Settings" : "First-run setup"}</h3>
                  <p>{configState?.configPath ?? "Loading config path..."}</p>
                </div>
              </div>
              <div className="toolbar-actions">
                <span className={`status-pill status-${configReady ? "done" : "error"}`}>
                  {configReady ? "ready" : "setup required"}
                </span>
                {configReady ? (
                  <button className="primary-button" onClick={() => setActiveTab("overview")}>
                    Done
                  </button>
                ) : null}
              </div>
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
                    <button disabled={busyAction !== null} onClick={() => void loadConfigState()}>
                      Reload
                    </button>
                    <button disabled={busyAction !== null} onClick={() => void autodetectConfig()}>
                      Auto-detect tools
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
                        <span>Eject when the disc is done with</span>
                        <select
                          value={configDraft.eject_after_rip ?? "false"}
                          onChange={(e) => setConfigDraft((value) => ({ ...value, eject_after_rip: e.target.value }))}
                        >
                          <option value="false">Leave the disc in the drive</option>
                          <option value="true">Eject automatically</option>
                        </select>
                        <small>
                          Ejects once identification is finished — including when a job stops to ask you
                          something — so the drive is free even while a job waits. With continuous mode on,
                          dropping the next disc into the open tray starts it without touching the app.
                        </small>
                      </label>
                      <label>
                        <span>After a verified NAS copy</span>
                        <select
                          value={configDraft.clear_local_after_transfer ?? "false"}
                          onChange={(e) =>
                            setConfigDraft((value) => ({ ...value, clear_local_after_transfer: e.target.value }))
                          }
                        >
                          <option value="false">Keep the local copy</option>
                          <option value="true">Delete the staged files</option>
                        </select>
                        <small>
                          A rip leaves several GB in staging and is redundant once the NAS copy is
                          confirmed. Deleting it automatically keeps a long session from stopping to
                          free space by hand. Job records, including the NAS path and checksum, are kept.
                        </small>
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
              {snapshot.job.media_type === "tv" ? (
                <>
                  <div className="metric-card">
                    <span>Scope</span>
                    <strong>{snapshot.job.disc_scope ?? "unspecified"}</strong>
                  </div>
                  <div className="metric-card">
                    <span>Season</span>
                    <strong>{snapshot.job.season_number ?? "-"}</strong>
                  </div>
                  <div className="metric-card">
                    <span>Episodes mapped</span>
                    <strong>{snapshot.episode_mappings.length}</strong>
                  </div>
                </>
              ) : (
                <>
                  <div className="metric-card">
                    <span>Titles ripped</span>
                    <strong>{snapshot.rip_titles.length}</strong>
                  </div>
                  <div className="metric-card">
                    <span>Feature length</span>
                    <strong>{featureRuntimeLabel}</strong>
                  </div>
                  <div className="metric-card">
                    <span>Mode</span>
                    <strong>{(snapshot.job.movie_mode ?? "single").replace(/_/g, " ")}</strong>
                  </div>
                </>
              )}
              <div className="metric-card">
                <span>On NAS</span>
                <strong>
                  {snapshot.outputs.filter((o) => o.transfer_status === "done").length}
                  {snapshot.outputs.length ? ` / ${snapshot.outputs.length}` : ""}
                </strong>
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
                {recentLogs.map((logItem, index) => {
                  const level = String(logItem.level || "INFO").toUpperCase();
                  const tone = level === "ERROR" ? "is-error" : isLogWarning(logItem) ? "is-warning" : "";
                  return (
                    <div key={`${logItem.timestamp}-${index}`} className={`log-row ${tone}`}>
                      <span className={`log-level ${tone || "is-info"}`}>{level}</span>
                      <p className="log-message">{logItem.message}</p>
                      <span className="log-time" title={logItem.timestamp}>
                        {formatRelativeTime(logItem.timestamp)}
                      </span>
                    </div>
                  );
                })}
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
                      autoFocus
                      value={modalValues.searchQuery ?? ""}
                      onChange={(e) => setModalValues((v) => ({ ...v, searchQuery: e.target.value }))}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && (modalValues.searchQuery ?? "").trim() && busyAction === null) {
                          e.preventDefault();
                          void runManualTmdbSearch();
                        }
                      }}
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
