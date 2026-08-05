# Usage (GUI)

Launch:

```powershell
python app\main.py --config app\config.json gui
```

## What the GUI provides

- Live job list and status.
- Selected job details (JSON view).
- Buttons for:
  - TMDB Identify
  - Run Mapping
  - Override TMDB candidate
  - Override Mapping
  - Override Split timestamps

## Recommended GUI flow

1. Create job via CLI.
2. Run rip via CLI (or pipeline).
3. In GUI, select job and click **TMDB Identify**.
4. If needed, click **Override TMDB**.
5. Click **Run Mapping**.
6. If needed, apply mapping/split overrides.
7. Continue finalize/transfer via CLI (current MVP flow).

