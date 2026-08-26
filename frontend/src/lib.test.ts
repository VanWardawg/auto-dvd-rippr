/**
 * Tests for the pure presentation helpers.
 *
 * These decide what the user is actually told: how far along a job is, how
 * long is left, whether it is working or stalled, and what gets sent to the
 * backend when a rip starts. They were the only part of the frontend testable
 * without a browser, and none of it had ever been tested.
 */
import { describe, expect, it } from "vitest";

import {
  BOOLEAN_CONFIG_KEYS,
  MOVIE_PIPELINE_STAGES,
  TV_PIPELINE_STAGES,
  buildConfigDraft,
  coerceConfigDraft,
  episodeLabel,
  fileNameFromPath,
  formatBytes,
  formatEta,
  formatRelativeTime,
  getActivityState,
  getPipelineStageIndex,
  getPipelineStages,
  getProgressPercent,
  isLogWarning,
  movieSlotCount,
  normalizeStartRequest,
  parseTitles,
} from "./lib";
import type { EpisodeMapping, JobLog, StartJobRequest } from "./types";

describe("formatBytes", () => {
  it("uses GB above a gigabyte", () => {
    expect(formatBytes(5.5 * 1024 ** 3)).toBe("5.5 GB");
  });

  it("drops the decimal once the number is large", () => {
    expect(formatBytes(42 * 1024 ** 3)).toBe("42 GB");
  });

  it("uses MB below a gigabyte", () => {
    expect(formatBytes(250 * 1024 ** 2)).toBe("250 MB");
  });

  it("returns null for nothing to show, so callers can hide the label", () => {
    expect(formatBytes(0)).toBeNull();
    expect(formatBytes(null)).toBeNull();
    expect(formatBytes(undefined)).toBeNull();
  });

  it("never reports a real amount as zero", () => {
    // A few hundred KB is still worth something; rounding to "0 MB" would
    // read as nothing to reclaim.
    expect(formatBytes(300 * 1024)).toBe("1 MB");
  });
});

describe("formatEta", () => {
  it("shows seconds under a minute", () => {
    expect(formatEta(45)).toContain("45");
  });

  it("shows minutes and seconds for a typical rip", () => {
    const out = formatEta(384) ?? "";
    expect(out).toContain("6");
    expect(out).toMatch(/m|min/);
  });

  it("returns null when there is no estimate rather than guessing", () => {
    expect(formatEta(null)).toBeNull();
    expect(formatEta(undefined)).toBeNull();
  });

  it("does not present a negative estimate", () => {
    const out = formatEta(-5);
    expect(out === null || !out.includes("-")).toBe(true);
  });
});

describe("pipeline stages", () => {
  it("movies skip mapping and splitting", () => {
    const stages = getPipelineStages("movie");
    expect(stages).toEqual(MOVIE_PIPELINE_STAGES);
    expect(stages).not.toContain("mapping");
    expect(stages).not.toContain("splitting");
  });

  it("tv identifies before ripping, because mapping needs the episode list", () => {
    const stages = getPipelineStages("tv");
    expect(stages).toEqual(TV_PIPELINE_STAGES);
    expect(stages.indexOf("identifying")).toBeLessThan(stages.indexOf("ripping"));
  });

  it("falls back to the tv path when the media type is unknown", () => {
    expect(getPipelineStages(null)).toEqual(TV_PIPELINE_STAGES);
  });

  it("progress runs from zero at queued to a hundred at done", () => {
    expect(getProgressPercent("queued", MOVIE_PIPELINE_STAGES)).toBe(0);
    expect(getProgressPercent("done", MOVIE_PIPELINE_STAGES)).toBe(100);
  });

  it("progress increases monotonically through the stages", () => {
    let previous = -1;
    for (const stage of MOVIE_PIPELINE_STAGES) {
      const pct = getProgressPercent(stage, MOVIE_PIPELINE_STAGES);
      expect(pct).toBeGreaterThanOrEqual(previous);
      previous = pct;
    }
  });

  it("an errored job does not report a nonsense position", () => {
    const pct = getProgressPercent("error", MOVIE_PIPELINE_STAGES);
    expect(pct).toBeGreaterThanOrEqual(0);
    expect(pct).toBeLessThanOrEqual(100);
  });

  it("locates the current stage in the stepper", () => {
    expect(getPipelineStageIndex("identifying", MOVIE_PIPELINE_STAGES, null)).toBe(
      MOVIE_PIPELINE_STAGES.indexOf("identifying"),
    );
  });
});

describe("getActivityState", () => {
  const justNow = new Date().toISOString();
  const longAgo = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();

  it("reports work in progress when the job is moving", () => {
    // "Active now" for the last 20 seconds, "Working" up to two minutes --
    // both mean progressing, which is the distinction that matters.
    expect(["Active now", "Working"]).toContain(getActivityState("ripping", false, justNow));
  });

  it("says so when a job has gone quiet", () => {
    expect(getActivityState("ripping", false, longAgo)).toMatch(/quiet/i);
  });

  it("a finished job is not described as working", () => {
    expect(getActivityState("done", false, longAgo)).not.toBe("Working");
  });

  it("needing review is distinguished from working", () => {
    // Telling "it is busy" from "it is waiting for you" is the single most
    // useful thing this label does.
    expect(getActivityState("identifying", true, justNow)).not.toBe("Working");
  });
});

describe("normalizeStartRequest", () => {
  const base: StartJobRequest = { discLabel: "DISC", mediaType: "movie" };

  it("drops tv-only fields from a movie job", () => {
    const out = normalizeStartRequest({
      ...base,
      mediaType: "movie",
      seasonNumber: 3,
      discScope: "full_season",
    });
    expect(out.seasonNumber ?? null).toBeNull();
  });

  it("keeps the season on a tv job", () => {
    const out = normalizeStartRequest({ ...base, mediaType: "tv", seasonNumber: 3 });
    expect(out.seasonNumber).toBe(3);
  });

  it("trims the disc label so a stray space does not become the search query", () => {
    const out = normalizeStartRequest({ ...base, discLabel: "  PONYO  " });
    expect(out.discLabel).toBe("PONYO");
  });
});

describe("config draft coercion", () => {
  it("turns the checkbox strings back into real booleans", () => {
    // The form stores every field as a string, and Python treats the string
    // "false" as true -- this is the conversion that stops that.
    const out = coerceConfigDraft({ eject_after_rip: "false", verify_transfers: "true" });
    expect(out.eject_after_rip).toBe(false);
    expect(out.verify_transfers).toBe(true);
  });

  it("leaves text settings alone", () => {
    const out = coerceConfigDraft({ nas_root: "Y:\\", eject_after_rip: "true" });
    expect(out.nas_root).toBe("Y:\\");
  });

  it("covers every boolean key the backend expects", () => {
    const draft: Record<string, string> = {};
    for (const key of BOOLEAN_CONFIG_KEYS) draft[key] = "false";
    const out = coerceConfigDraft(draft);
    for (const key of BOOLEAN_CONFIG_KEYS) {
      expect(typeof out[key], `${key} must coerce to boolean`).toBe("boolean");
    }
  });

  it("round-trips a saved config back into the form", () => {
    const draft = buildConfigDraft({ nas_root: "Y:\\", eject_after_rip: true });
    expect(draft.nas_root).toBe("Y:\\");
    expect(coerceConfigDraft(draft).eject_after_rip).toBe(true);
  });

  it("an absent config yields a usable empty draft rather than crashing", () => {
    expect(() => buildConfigDraft(null)).not.toThrow();
  });
});

describe("display helpers", () => {
  it("shows just the filename, not the whole staging path", () => {
    expect(fileNameFromPath("C:\\autorippr\\staging\\jobs\\x\\rip_output\\t00.mkv")).toBe("t00.mkv");
    expect(fileNameFromPath(null)).toBeTruthy();
  });

  it("parses the episode titles the backend stores as json", () => {
    expect(parseTitles('["One","Two"]')).toEqual(["One", "Two"]);
  });

  it("survives malformed episode title json", () => {
    expect(() => parseTitles("{not json")).not.toThrow();
    expect(parseTitles(undefined)).toEqual([]);
  });

  it("labels a single episode and a combined range differently", () => {
    const single = { episode_start: 3, episode_end: 3 } as EpisodeMapping;
    const combined = { episode_start: 3, episode_end: 4 } as EpisodeMapping;
    expect(episodeLabel(single)).not.toEqual(episodeLabel(combined));
    expect(episodeLabel(combined)).toContain("4");
  });

  it("maps movie mode to the number of features expected", () => {
    expect(movieSlotCount("single")).toBe(1);
    expect(movieSlotCount("double_feature")).toBe(2);
    expect(movieSlotCount("trilogy")).toBe(3);
    expect(movieSlotCount(null)).toBe(1);
  });

  it("flags warnings and errors in the activity log", () => {
    expect(isLogWarning({ level: "WARNING", message: "x", timestamp: "" } as JobLog)).toBe(true);
    expect(isLogWarning({ level: "INFO", message: "x", timestamp: "" } as JobLog)).toBe(false);
  });

  it("renders a relative time without throwing on bad input", () => {
    expect(typeof formatRelativeTime(new Date().toISOString())).toBe("string");
    expect(() => formatRelativeTime("not a date")).not.toThrow();
    expect(() => formatRelativeTime(null)).not.toThrow();
  });
});
