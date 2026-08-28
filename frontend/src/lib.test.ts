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
  buildDriveCardState,
  deserializeDriveCards,
  serializeDriveCards,
  MOVIE_PIPELINE_STAGES,
  TV_PIPELINE_STAGES,
  buildConfigDraft,
  buildGuidedReviewRows,
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
  showQueryFromLabel,
  suggestEpisodeRange,
  seasonFromLabel,
  discNumberFromLabel,
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


describe("drive card persistence", () => {
  it("remembers which drive each card is assigned to", () => {
    const cards = [
      { ...buildDriveCardState(), form: { ...buildDriveCardState().form, opticalDrive: "E:" } },
      { ...buildDriveCardState(), form: { ...buildDriveCardState().form, opticalDrive: "F:" } },
    ];
    const restored = deserializeDriveCards(serializeDriveCards(cards));
    expect(restored).toHaveLength(2);
    expect(restored.map((c) => c.form.opticalDrive)).toEqual(["E:", "F:"]);
  });

  it("keeps the tv settings on a tv card", () => {
    const card = buildDriveCardState();
    card.form = { ...card.form, mediaType: "tv", seasonNumber: 4, discScope: "partial_season" };
    const [restored] = deserializeDriveCards(serializeDriveCards([card]));
    expect(restored.form.mediaType).toBe("tv");
    expect(restored.form.seasonNumber).toBe(4);
    expect(restored.form.discScope).toBe("partial_season");
  });

  it("does not carry a stale disc label across sessions", () => {
    // The label belongs to whatever disc is in the drive now, not to the one
    // that happened to be there last time.
    const card = buildDriveCardState();
    card.form = { ...card.form, discLabel: "PONYO" };
    const [restored] = deserializeDriveCards(serializeDriveCards([card]));
    expect(restored.form.discLabel).toBe("");
  });

  it("does not restore continuous mode", () => {
    // Otherwise an app opened weeks later would start ripping whatever disc
    // was left in the tray.
    const card = { ...buildDriveCardState(), continuousMode: true, continuousStatus: "on" };
    const [restored] = deserializeDriveCards(serializeDriveCards([card]));
    expect(restored.continuousMode).toBe(false);
    expect(restored.continuousStatus).toBeNull();
  });

  it("falls back to a single card when nothing is stored", () => {
    expect(deserializeDriveCards(null)).toHaveLength(1);
  });

  it("never leaves the user with a blank sidebar", () => {
    // Anything unusable must degrade to one working card rather than none.
    for (const bad of ["{not json", "[]", "null", '"a string"', "[null, 3]"]) {
      const restored = deserializeDriveCards(bad);
      expect(restored.length, `input ${bad}`).toBeGreaterThanOrEqual(1);
      expect(restored[0].form.mediaType).toBeTruthy();
    }
  });

  it("fills in fields written by an older version", () => {
    const restored = deserializeDriveCards(JSON.stringify([{ id: "x", form: { opticalDrive: "E:" } }]));
    expect(restored[0].form.mediaType).toBe("movie");
    expect(restored[0].form.movieMode).toBeTruthy();
    expect(restored[0].id).toBe("x");
  });

  it("gives a card without an id a fresh one", () => {
    const restored = deserializeDriveCards(JSON.stringify([{ form: { opticalDrive: "F:" } }]));
    expect(typeof restored[0].id).toBe("string");
    expect(restored[0].id.length).toBeGreaterThan(0);
  });
});

describe("disc label parsing", () => {
  it("drops season and disc markers from the search query", () => {
    // THE_WINGFEATHER_SAGA_S1 was searched verbatim and matched nothing.
    expect(showQueryFromLabel("THE_WINGFEATHER_SAGA_S1")).toBe("the wingfeather saga");
    expect(showQueryFromLabel("MICKEY_MOUSE_CLUBHOUSE_S2_D3")).toBe("mickey mouse clubhouse");
  });

  it("drops the pressing's own jargon", () => {
    expect(showQueryFromLabel("ALVIN_AND_THE_CHIPMUNKS_4X3")).toBe("alvin and the chipmunks");
    expect(showQueryFromLabel("PRINCESS_BRIDE_CE")).toBe("princess bride");
  });

  it("leaves a compilation title alone", () => {
    // MINNIES_PET_SALON is the name of a DVD, not of a show -- but mangling it
    // would only make the user's manual search harder.
    expect(showQueryFromLabel("MINNIES_PET_SALON")).toBe("minnies pet salon");
    expect(showQueryFromLabel("I_HEART_MINNIE")).toBe("i heart minnie");
  });

  it("reads the season through an underscore", () => {
    // An underscore is a word character, which is what broke this in Python.
    expect(seasonFromLabel("THE_WINGFEATHER_SAGA_S1")).toBe(1);
    expect(seasonFromLabel("PAW_PATROL_SEASON_3_DISC_2")).toBe(3);
    expect(seasonFromLabel("SOME_SHOW_S02E05")).toBe(2);
  });

  it("reads which disc of the set it is", () => {
    expect(discNumberFromLabel("TUTTLE_TWINS_S1_D2")).toBe(2);
    expect(discNumberFromLabel("PAW_PATROL_SEASON_3_DISC_2")).toBe(2);
  });

  it("claims nothing for a label that says nothing", () => {
    expect(seasonFromLabel("MINNIES_PET_SALON")).toBeNull();
    expect(discNumberFromLabel("MINNIES_PET_SALON")).toBeNull();
    expect(showQueryFromLabel(null)).toBe("");
    expect(seasonFromLabel(undefined)).toBeNull();
  });

  it("agrees with the backend on the discs it will actually meet", () => {
    // These are the labels sitting on the shelf right now.
    expect(showQueryFromLabel("TUTTLE_TWINS_S1_D1")).toBe("tuttle twins");
    expect(showQueryFromLabel("TUTTLE_TWINS_S1_D2")).toBe("tuttle twins");
  });
});

describe("suggestEpisodeRange", () => {
  it("splits a season across its discs", () => {
    expect(suggestEpisodeRange(24, 1, 4)).toEqual({ start: 1, end: 6 });
    expect(suggestEpisodeRange(24, 4, 4)).toEqual({ start: 19, end: 24 });
  });

  it("gives the remainder to the earlier discs", () => {
    // Mickey Mouse Clubhouse season 2: 39 over 4 discs is 10/10/10/9.
    expect([1, 2, 3, 4].map((n) => suggestEpisodeRange(39, n, 4))).toEqual([
      { start: 1, end: 10 },
      { start: 11, end: 20 },
      { start: 21, end: 30 },
      { start: 31, end: 39 },
    ]);
  });

  it("tiles the season exactly, dropping and repeating nothing", () => {
    for (const [count, discs] of [[26, 3], [39, 4], [32, 5], [13, 2]] as const) {
      const covered: number[] = [];
      for (let n = 1; n <= discs; n += 1) {
        const range = suggestEpisodeRange(count, n, discs)!;
        for (let e = range.start; e <= range.end; e += 1) covered.push(e);
      }
      expect(covered, `${count} eps over ${discs} discs`).toEqual(
        Array.from({ length: count }, (_, i) => i + 1),
      );
    }
  });

  it("agrees with the backend it mirrors", () => {
    // Same cases asserted in app/tests/test_disc_labels.py.
    expect(suggestEpisodeRange(26, 1, 1)).toEqual({ start: 1, end: 26 });
    expect(suggestEpisodeRange(26, null, 4)).toBeNull();
    expect(suggestEpisodeRange(26, 5, 4)).toBeNull();
    expect(suggestEpisodeRange(3, 1, 8)).toBeNull();
  });
});

describe("compilation discs", () => {
  const base: StartJobRequest = { discLabel: "MINNIES_PET_SALON", mediaType: "tv" };

  it("sends the specials choice, which is the only setting it has", () => {
    const out = normalizeStartRequest({ ...base, discScope: "compilation", includeSpecials: true });
    expect(out.includeSpecials).toBe(true);
    expect(out.discScope).toBe("compilation");
  });

  it("defaults to leaving the specials out", () => {
    // 47 specials against 123 episodes for Mickey Mouse Clubhouse -- opting in
    // should be deliberate, not the default.
    const out = normalizeStartRequest({ ...base, discScope: "compilation" });
    expect(out.includeSpecials).toBe(false);
  });

  it("carries no season or episode range", () => {
    // A compilation draws from anywhere in the show, so a range is meaningless
    // and a season number would put the files in the wrong folder.
    const out = normalizeStartRequest({
      ...base,
      discScope: "compilation",
      seasonNumber: 2,
      episodeRangeStart: 1,
      episodeRangeEnd: 5,
    });
    expect(out.seasonNumber ?? null).toBeNull();
    expect(out.episodeRangeStart ?? null).toBeNull();
    expect(out.episodeRangeEnd ?? null).toBeNull();
  });

  it("does not leak the specials flag onto a movie", () => {
    const out = normalizeStartRequest({ ...base, mediaType: "movie", includeSpecials: true });
    expect(out.includeSpecials ?? null).toBeNull();
  });

  it("does not leak it onto an ordinary season disc", () => {
    const out = normalizeStartRequest({
      ...base,
      discScope: "partial_season",
      seasonNumber: 2,
      includeSpecials: true,
    });
    expect(out.includeSpecials ?? null).toBeNull();
  });
});

describe("the chosen show reaches the job", () => {
  // Picking a show in the card only helps if it survives normalisation; it
  // was being silently dropped, which left compilation discs unidentifiable.
  const base: StartJobRequest = { discLabel: "D", mediaType: "tv", tmdbShowId: 3934 };

  it("survives on a compilation disc", () => {
    expect(normalizeStartRequest({ ...base, discScope: "compilation" }).tmdbShowId).toBe(3934);
  });

  it("survives on a partial season", () => {
    expect(normalizeStartRequest({ ...base, discScope: "partial_season" }).tmdbShowId).toBe(3934);
  });

  it("survives on a full season", () => {
    expect(normalizeStartRequest({ ...base, discScope: "full_season" }).tmdbShowId).toBe(3934);
  });

  it("is absent when no show was chosen", () => {
    const out = normalizeStartRequest({ discLabel: "D", mediaType: "tv", discScope: "full_season" });
    expect(out.tmdbShowId ?? null).toBeNull();
  });
});

describe("book and volume labels", () => {
  // AVATAR_BK3_VOL1: neither token was recognised, so a four-disc season set
  // produced no season, no disc number, and no suggested episode range.
  it("reads a book as a season", () => {
    expect(seasonFromLabel("AVATAR_BK3_VOL1")).toBe(3);
    expect(seasonFromLabel("AVATAR_BOOK_3_VOLUME_4")).toBe(3);
  });

  it("reads a volume as a disc", () => {
    expect(discNumberFromLabel("AVATAR_BK3_VOL1")).toBe(1);
    expect(discNumberFromLabel("AVATAR_BK3_VOL2")).toBe(2);
  });

  it("keeps both out of the search", () => {
    expect(showQueryFromLabel("AVATAR_BK3_VOL1")).toBe("avatar");
  });

  it("agrees with the backend on all four discs", () => {
    expect([1, 2, 3, 4].map((n) => discNumberFromLabel(`AVATAR_BK3_VOL${n}`))).toEqual([1, 2, 3, 4]);
    expect([1, 2, 3, 4].map((n) => seasonFromLabel(`AVATAR_BK3_VOL${n}`))).toEqual([3, 3, 3, 3]);
  });

  it("claims nothing from a book with no number", () => {
    expect(seasonFromLabel("THE_JUNGLE_BOOK")).toBeNull();
  });
});

describe("guided review rows carry a season", () => {
  // The draft had only episodeStart/episodeEnd, so a compilation row sitting
  // in season 3 could not be corrected without silently moving to season 1.
  const ripTitles = [
    { id: 1, source_file: "A1_t00.mkv", duration_seconds: 1440, chapter_count: 6 },
    { id: 2, source_file: "A1_t01.mkv", duration_seconds: 1440, chapter_count: 6 },
  ] as never[];

  it("seeds each row from its own mapping", () => {
    const rows = buildGuidedReviewRows(ripTitles, [
      { id: 10, rip_title_id: 1, season_number: 3, episode_start: 12, episode_end: 12 },
      { id: 11, rip_title_id: 2, season_number: 1, episode_start: 1, episode_end: 1 },
    ] as never[]);
    expect(rows.map((r) => r.seasonNumber)).toEqual(["3", "1"]);
  });

  it("keeps two rows from one disc in different seasons", () => {
    // The whole point: one compilation disc, several seasons.
    const rows = buildGuidedReviewRows(ripTitles, [
      { id: 10, rip_title_id: 1, season_number: 4, episode_start: 8, episode_end: 8 },
      { id: 11, rip_title_id: 2, season_number: 2, episode_start: 33, episode_end: 33 },
    ] as never[]);
    expect(new Set(rows.map((r) => r.seasonNumber))).toEqual(new Set(["4", "2"]));
  });

  it("leaves the season blank when the mapping has none", () => {
    // Blank means "leave it where it is", which is what an ordinary disc wants.
    const rows = buildGuidedReviewRows(ripTitles, [
      { id: 10, rip_title_id: 1, episode_start: 1, episode_end: 1 },
    ] as never[]);
    expect(rows[0].seasonNumber).toBe("");
  });

  it("an unmapped file still produces a usable row", () => {
    const rows = buildGuidedReviewRows(ripTitles, [] as never[]);
    expect(rows).toHaveLength(2);
    expect(rows[0].status).toBe("ignore");
  });
});
