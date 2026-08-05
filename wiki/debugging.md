# Debugging Guide

## Where logs and state live

- Main log: `log_path` from config (JSON lines).
- DB: `db_path` from config.
- Per-job files under:
  - `staging_root\jobs\<job_id>\logs\`
  - `staging_root\jobs\<job_id>\rip_output\`
  - `staging_root\jobs\<job_id>\split_output\`
  - `staging_root\jobs\<job_id>\finalized\`

## Common issues

### 1) Config error at startup

Run:

```powershell
python app\main.py --config app\config.json validate-config
```

Fix missing/invalid keys.

### 2) MakeMKV failure

- Verify `makemkv_path` executable exists.
- Open per-job `makemkv.log` for command/stdout/stderr.
- Confirm disc is readable in MakeMKV GUI manually.

### 3) TMDB mismatch/low confidence

- Use:
  - `tmdb candidates <job_id>`
  - `tmdb select <job_id> <tv|movie> <tmdb_id>`
- Confirm disc label is descriptive (show + season).

### 4) Split failure

- Inspect split plans:
  - `split list <job_id>`
- Set manual timestamps:
  - `split override <split_plan_id> --start X --end Y`
- Re-run:
  - `split run <job_id>`

### 5) Transfer failure to NAS

- Verify `nas_root` permissions/connectivity.
- Check `outputs.last_error` and `transfer_attempts`.
- Re-run transfer:
  - `transfer <job_id>`

## Database inspection (quick)

Use sqlite tooling or Python to inspect:

- `jobs`
- `tmdb_candidates`
- `episode_mappings`
- `split_plans`
- `outputs`
- `transfer_attempts`

