# DVD Menu-to-File Association for Hard Discs - Proposal

## 1. Problem statement

The current app can now recover **episode names** from difficult DVDs, but it still fails to reliably answer the harder question:

> Which recovered menu bundle/episode group corresponds to which ripped file (`B1_t00.mkv`, `B2_t01.mkv`, etc.)?

On easy discs, file order, chapter structure, and durations are often enough.
On hard discs like this Paw Patrol example:

- menu order differs from ripped file order,
- chapter markers are absent or useless,
- there is a large **Play All** title,
- menu text exists only in rendered DVD menu states,
- TMDB names are correct but get attached to the wrong ripped bundle.

This document proposes a deterministic subsystem for solving that association problem more reliably.

## 2. Key research findings

### 2.1 What works today

- **MakeMKV + ffprobe** reliably produce ripped files and durations.
- **TMDB season API** reliably gives correct episode IDs and names.
- **DVD-Archaeology-style parsing** can recover useful DVD navigation/menu structure and button rectangles even on this disc, especially when corrupt IFOs are skipped.
- **VLC rendered screenshots** can show readable menu text that raw menu VOB extraction often cannot.

### 2.2 What does not work reliably today

- Raw menu VOB/frame extraction often produces black or unusable frames.
- OCR can recover correct episode names, but not always enough structure to attach them to the correct ripped bundle.
- Current mapping still falls back too often to:
  - file order,
  - duration,
  - “consume next remaining episodes”.

### 2.3 Important technical constraints

- DVD menus do **not** usually store button labels as clean text metadata.
- Visible labels are often rendered from:
  - menu video,
  - SPU/subpicture overlays,
  - navigation state/highlight state.
- Therefore, a full solution must combine:
  1. **navigation/target parsing**, and
  2. **label extraction** from rendered or overlay-backed menu content.

## 3. Recommendation

Preserve the useful parts of the current approach, but split the subsystem into clear layers:

1. **Disc Menu Analysis**
2. **Bundle Association**
3. **Episode Mapping**
4. **Split Boundary Resolution**

The most important change is:

> Stop assigning TMDB episodes directly to ripped files by sequence.
> First build a stable **menu bundle -> ripped file** association.

## 4. Target architecture

### 4.1 Layer A - Disc Menu Analysis

Purpose: recover menu pages, button geometry, page ordering, and button playback targets.

#### Preferred sources

1. **libdvdnav / pydvdnav** (long-term preferred)
   - authoritative menu navigation events,
   - button geometry from NAV packets,
   - button activation -> playback target transitions.

2. **DVD-Archaeology / pyparsedvd-style parsing** (short-term support path)
   - menu domains,
   - button rectangles,
   - PGC/title structure,
   - corrupt-IFO skipping.

3. **Rendered screenshot source**
   - VLC window capture or direct window capture,
   - only for visible labels/page state,
   - not for core navigation truth.

#### Required output artifact

Produce one cached artifact per job:

`menu_analysis.json`

Minimum schema:

```json
{
  "disc_root": "E:\\",
  "pages": [
    {
      "page_id": "episode_selection_page_1",
      "menu_id": "VTSM_06_pgc03",
      "screenshot_path": "...png",
      "buttons": [
        {
          "button_id": "btn12",
          "rect": {"x": 0, "y": 0, "w": 0, "h": 0},
          "label_text": "Pups Save the Sea Turtles / Pups and the Very Big Baby",
          "label_confidence": 0.91,
          "nav_target": {
            "title": 6,
            "pgc": 1,
            "cell": null,
            "time_seconds": null
          },
          "page_order_index": 0
        }
      ]
    }
  ]
}
```

### 4.2 Layer B - Bundle Association

Purpose: map each menu button/bundle entry to the corresponding ripped file.

#### Inputs

- `menu_analysis.json`
- `rip_titles`
- rip metadata (`makemkv_info`, duration, size, source filename)

#### Required output artifact

`bundle_association.json`

```json
{
  "bundles": [
    {
      "bundle_id": "page1_btn0",
      "label_text": "Pups Save the Sea Turtles / Pups and the Very Big Baby",
      "nav_target": {"title": 6, "pgc": 1},
      "rip_title_id": 8,
      "source_file": "B1_t00.mkv",
      "confidence": 0.93,
      "association_reason": "Matched by nav target + page order"
    }
  ],
  "play_all": {
    "rip_title_id": 7,
    "source_file": "A1_t05.mkv",
    "confidence": 0.98
  }
}
```

#### Matching priority

Use this order of evidence:

1. **Exact nav target match**
   - if button target can be mapped to title/cell/PGC and rip title has same title/cell identity.

2. **Menu page order**
   - page-local visual order should drive bundle ordering,
   - must support non-default order (e.g. right-to-left, top-to-bottom).

3. **Per-bundle duration similarity**
   - only as a tiebreaker.

4. **Filename class**
   - e.g. `A*` likely Play All, `B*`/`C*` likely episode bundles.

#### Explicit non-goal

Never use “next remaining episode group” as the primary association rule when stronger menu evidence exists.

### 4.3 Layer C - Episode Mapping

Purpose: attach TMDB episode IDs/names to an already-associated bundle.

#### Inputs

- `bundle_association.json`
- TMDB season episodes
- job disc scope (`full_season`, `partial_season`, `special`)
- episode range for partial discs

#### Output

Current `episode_mappings`, but each mapping row must be derived from:

`menu bundle -> rip file -> TMDB episode group`

not directly from file order.

### 4.4 Layer D - Split Boundary Resolution

Purpose: cut a mapped bundle into episodes.

Current status:

- only midpoint/duration-based splitting is working reliably on this disc.

Proposed improvement:

1. Use chapter boundaries when present.
2. Use menu-derived transition structure if available.
3. Use duration as weak fallback only.
4. Expose manual override for exact timestamps.

## 5. Strong proposal for hard-disc handling

### 5.1 Add explicit job step: Analyze DVD Menu

This should be a separate step/button, not hidden inside normal mapping.

Why:

- menu analysis is slow,
- it may require disc presence,
- it should be cached and reused,
- repeated re-map runs should not relaunch VLC or external parsers.

### 5.2 Cache everything

Per job, cache:

- `menu_analysis.json`
- `bundle_association.json`
- rendered menu screenshots
- OCR text
- button crops

Then:

- **Analyze DVD Menu** = expensive, run once
- **Re-run Mapping** = fast, reuse cached artifacts only

## 6. Proposed deterministic strategy for hard discs

For discs like this one:

1. Detect likely **Play All** file and exclude it from primary bundle matching.
2. Capture episode-selection pages in rendered form.
3. OCR button labels and preserve page-local order.
4. Resolve each button’s playback target through nav parsing.
5. Match those targets to ripped bundle files.
6. Map TMDB episode names/IDs onto those bundles.
7. Only then split the bundle file.

This is the first path that avoids the current failure mode of:

> “correct episode names, wrong file”.

## 7. Implementation recommendation

### Phase 1 - stabilize current system

1. Keep DVD-Archaeology support path.
2. Split **Analyze DVD Menu** out of **Run Mapping**.
3. Make mapping consume cached artifacts only.
4. Add bundle association artifact.
5. Add GUI visibility into:
   - menu pages,
   - button labels,
   - bundle -> file mapping,
   - play-all detection.

### Phase 2 - improve association quality

1. Use page order as a first-class feature.
2. Allow per-page reading direction:
   - left-to-right, top-to-bottom
   - right-to-left, top-to-bottom
   - column-first
3. Add bundle/file override UI.
4. Persist manual corrections as reusable job artifacts.

### Phase 3 - preferred long-term nav core

Replace the external helper as authoritative nav source with:

- **libdvdnav / pydvdnav**, if native Windows install is solved.

Use it to:

- walk menus deterministically,
- detect pages,
- inspect button geometry,
- activate buttons and observe target/title changes.

Keep OCR only for labels.

## 8. Windows environment requirements for the preferred path

If `libdvdnav` is pursued on Windows, the environment needs:

- `libdvdnav` library
- `libdvdread` library
- development headers (`dvd_types.h`, related headers)
- a build path that works with the chosen Python binding
- packaged DLLs discoverable at runtime

This is an environment/setup task, not just a Python code task.

## 9. Success criteria

For a hard disc like this Paw Patrol example:

1. The system should identify:
   - one **Play All** file,
   - five episode bundle files,
   - the correct episode pairs for each file.

2. The GUI should show:
   - the menu page screenshot,
   - the OCR’d label,
   - the associated ripped file,
   - the TMDB episodes attached to that file.

3. Re-running mapping should:
   - take under ~30 seconds when cached artifacts exist,
   - not reopen VLC repeatedly,
   - not rerun heavy external analysis unless explicitly requested.

4. Manual override should be a correction path, not the primary path.

## 10. Questions for a second LLM to answer

Give another model this exact problem and ask it to produce:

1. The best architecture for:
   - menu analysis,
   - bundle association,
   - episode mapping,
   - split boundary detection.

2. Whether the preferred long-term nav core should be:
   - `libdvdnav`,
   - DVD-Archaeology-style parser,
   - VLC/libdvdnav hybrid,
   - or another existing project.

3. A concrete data model for:
   - `menu_analysis.json`
   - `bundle_association.json`
   - manual correction persistence

4. A strategy for rendered menu capture on Windows that avoids the black-frame problem.

5. A robust algorithm for:
   - associating button labels to ripped bundle files,
   - even when file order differs from menu order.

## 11. Amendments (post-review)

### Amendment A - Critical association bridge

The proposal assumes nav targets from menu buttons can be matched directly to MakeMKV ripped title numbers. This is **not directly true**.

The actual linkage chain is:

```
menu button → VTS title_id (from IFO nav parse)
           → DVD global title number (from VTS-to-title mapping in VIDEO_TS.IFO)
           → MakeMKV TINFO field 24 (disc title number stored per ripped file)
           → ripped file (B1_t00.mkv, etc.)
```

On the Paw Patrol disc, MakeMKV's TINFO field `24` values are:
- `B1_t00.mkv` → DVD title #19
- `B2_t01.mkv` → DVD title #36
- `B3_t02.mkv` → DVD title #43
- `C1_t03.mkv` → DVD title #58
- `C2_t04.mkv` → DVD title #70
- `A1_t05.mkv` → DVD title #86

And menu buttons target VTS-level `title_id` values (5, 6, 7, 8, etc.).

**Layer B must explicitly bridge these two numbering systems.** The join key is:
1. Parse `VIDEO_TS.IFO` to get the VTS-to-global-title mapping table.
2. For each menu button's target (VTS title_id + PGC), resolve to a global DVD title number.
3. Match that global title number against MakeMKV's stored `TINFO field 24` per ripped file.

Without this bridge, Layer B cannot work even with perfect Layer A data.

**Implementation requirement:** `pyparsedvd` or direct IFO binary parsing must expose the title search pointer table (TT_SRPT) from `VIDEO_TS.IFO`, which maps global title numbers to VTS + VTS_TTN pairs.

### Amendment B - Rendered page capture is an open problem

The proposal should explicitly acknowledge:

1. **VLC RC mode** does not produce rendered frames (confirmed).
2. **VLC visible-window automation** partially works but does not reliably navigate to page 2+.
3. **DVD-Archaeology's raw VOB frame extraction** produces black frames on subpicture/overlay-based menus (confirmed on this disc).

**Accepted fallback strategy:**
- Design the system to work with **partial page data** (e.g., only page 1 captured).
- For uncaptured pages, fall back to:
  - nav-graph-based ordering (button link traversal),
  - duration-based tiebreaking,
  - manual screenshot upload.
- Add a GUI action: **"Upload Menu Screenshot"** that lets the user paste/import a screenshot for any missing page.

**Future improvement path (not blocking):**
- Use `mss` or `pyautogui` for direct window capture instead of relying on VLC's snapshot feature.
- This avoids both the headless-RC problem and the black-frame problem.

### Amendment C - Primary nav core decision

**Use DVD-Archaeology + pyparsedvd as the primary nav core**, not `libdvdnav`.

Rationale:
- `libdvdnav` has no maintained standalone Windows build.
- The Python binding (`pydvdnav`) requires native compilation against the library.
- VLC bundles `libdvdnav` internally but doesn't expose it as a public API.
- DVD-Archaeology + pyparsedvd already work on this machine and produce usable nav/menu structure.

`libdvdnav` remains a **long-term optional enhancement** if someone solves the Windows environment setup (vcpkg or MSYS2 build path), but it should **not block progress**.

### Amendment D - Confidence gate for Layer B

Add an explicit **confidence gate** between Layer B and Layer C:

- If bundle association confidence is **≥ 0.85** for all bundles → proceed automatically.
- If any bundle has confidence **< 0.85** → pause and show the user:
  - the proposed file ↔ episode assignment,
  - the evidence (nav target, page order, duration match),
  - an option to confirm or override.

This prevents the "correct names, wrong file" failure from silently propagating into finalization and NAS transfer.

### Amendment E - Probability assessment

| Component | Probability of solving the problem | Notes |
|---|---|---|
| Layer A (menu analysis) | **75%** | Works for structure; rendered capture still shaky |
| Layer B (bundle association) | **60%** | Depends on bridging VTS title_id → MakeMKV title number reliably |
| Layer C (episode mapping) | **95%** | Already working well |
| Layer D (split boundaries) | **50%** | No clean solution without chapters; manual override is realistic fallback |
| **End-to-end automatic for hard discs** | **~45%** | Still likely needs human confirmation for edge cases |
| **End-to-end automatic for easy discs** | **~90%** | Duration/chapter/order usually sufficient |

## 12. Recommendation summary

The best current proposal is:

> Use DVD-Archaeology + pyparsedvd as the primary nav core. Introduce an explicit cached **bundle association** layer that bridges VTS-level nav targets to MakeMKV ripped files via DVD global title numbers (TINFO field 24). Accept that rendered multi-page capture is still an open problem and design for partial data + manual correction. Add a confidence gate so uncertain associations require human confirmation before proceeding.

That is the clearest path to handling even the hard DVDs instead of only the easy ones.
