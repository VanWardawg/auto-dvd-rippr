# Testing and Validation

## Fast validation (no physical disc)

1. Create job:

```powershell
python app\main.py --config app\config.json job create --disc-label "Bluey Season 1 Disc 1" --media-type tv
```

2. Run pipeline with mock rip:

```powershell
python app\main.py --config app\config.json pipeline run <job_id> --mock-rip
```

3. Check job:

```powershell
python app\main.py --config app\config.json job show <job_id>
```

Expected:

- Job transitions through pipeline statuses.
- `tmdb_candidates`, `episode_mappings`, `outputs` populated.
- If split/transfer needs review, response indicates where it stopped.

## Real disc validation

1. Confirm drive visibility:

```powershell
python app\main.py --config app\config.json rip drives
```

2. Create job + rip without mock:

```powershell
python app\main.py --config app\config.json job create --disc-label "Your Disc Label" --media-type tv
python app\main.py --config app\config.json rip run <job_id>
```

3. Continue stage-by-stage commands from `usage-cli.md`.

## Regression fixture recommendations

Keep small fixture samples to test:

- single-episode titles
- combined episodes
- out-of-order disc sequence

Re-run:

```powershell
python app\main.py --config app\config.json pipeline run <job_id> --mock-rip
```

