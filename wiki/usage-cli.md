# Usage (CLI)

## Create and run a job end-to-end

```powershell
python app\main.py --config app\config.json job create --disc-label "Bluey Season 1 Disc 1" --media-type tv
```

Use returned `job_id`.

### Option A: full pipeline in one command

```powershell
python app\main.py --config app\config.json pipeline run <job_id> --mock-rip
```

Remove `--mock-rip` for real disc ripping.

### Option B: stage-by-stage commands

```powershell
python app\main.py --config app\config.json rip run <job_id> --mock
python app\main.py --config app\config.json tmdb identify <job_id>
python app\main.py --config app\config.json mapping run <job_id>
python app\main.py --config app\config.json split plan <job_id>
python app\main.py --config app\config.json split run <job_id>
python app\main.py --config app\config.json finalize <job_id>
python app\main.py --config app\config.json transfer <job_id>
```

## Inspect state/data

```powershell
python app\main.py --config app\config.json job show <job_id>
python app\main.py --config app\config.json tmdb candidates <job_id>
python app\main.py --config app\config.json mapping list <job_id>
python app\main.py --config app\config.json split list <job_id>
```

## Manual overrides

TMDB override:

```powershell
python app\main.py --config app\config.json tmdb select <job_id> tv 82739
```

Mapping override:

```powershell
python app\main.py --config app\config.json mapping override <mapping_id> 5 6
```

Split override:

```powershell
python app\main.py --config app\config.json split override <split_plan_id> --start 0 --end 600
```

## Resume all incomplete jobs

```powershell
python app\main.py --config app\config.json pipeline resume-all --mock-rip
```

