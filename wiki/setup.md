# Setup

## 1) Prerequisites

- Windows 10/11
- Python 3.11+
- MakeMKV installed (`makemkvcon64.exe`)
- FFmpeg bundle installed (`ffmpeg.exe`, `ffprobe.exe`)
- TMDB API key
- NAS share path accessible from this machine

## 2) Config

1. Copy `app\config.example.json` to `app\config.json`.
2. Fill values:
   - `tmdb_api_key`
   - `makemkv_path`
   - `ffmpeg_path`
   - `ffprobe_path`
   - `staging_root`
   - `nas_root`
3. Validate:

```powershell
python app\main.py --config app\config.json validate-config
```

## 3) Optional env overrides

These can override config file values:

- `TMDB_API_KEY`
- `MAKEMKV_PATH`
- `FFMPEG_PATH`
- `FFPROBE_PATH`
- `STAGING_ROOT`
- `NAS_ROOT`
- `DB_PATH`
- `LOG_PATH`

