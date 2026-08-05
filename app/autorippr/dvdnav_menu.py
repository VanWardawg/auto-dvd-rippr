import importlib
import json
import time
from pathlib import Path
from typing import Any


def extract_dvdnav_menu_artifacts(
    staging_root: str,
    job_id: str,
    drive_root: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    artifact_dir = Path(staging_root) / "jobs" / job_id / "dvdnav_menu"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "dvdnav_menu.json"
    if artifact_path.exists():
        try:
            return json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    pydvdnav_mod = _import_pydvdnav_module()
    if pydvdnav_mod is None:
        payload = {
            "available": False,
            "reason": "pydvdnav not installed or native libdvdnav unavailable",
            "buttons": [],
        }
        artifact_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    DVDStream = getattr(pydvdnav_mod, "DVDStream", None)
    NavigationEvent = getattr(pydvdnav_mod, "NavigationEvent", None)
    if DVDStream is None:
        payload = {
            "available": False,
            "reason": "pydvdnav missing DVDStream interface",
            "buttons": [],
        }
        artifact_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    payload = {
        "available": True,
        "drive_root": drive_root,
        "buttons": [],
        "toc": {},
        "reason": None,
    }
    try:
        stream = DVDStream(device_name=drive_root)
        try:
            payload["toc"] = _safe_json_obj(getattr(stream, "table_of_contents", {}))
            nav_event = _first_nav_event(stream, NavigationEvent, timeout_seconds)
            if nav_event is None:
                payload["available"] = False
                payload["reason"] = "No NAV_PACKET/button information surfaced from libdvdnav"
            else:
                buttons = []
                button_info = dict(getattr(nav_event, "button_info", {}) or {})
                for button_id, rect in sorted(button_info.items()):
                    target = _probe_button_target(
                        DVDStream=DVDStream,
                        NavigationEvent=NavigationEvent,
                        drive_root=drive_root,
                        button_id=int(button_id),
                        timeout_seconds=min(30, timeout_seconds),
                    )
                    buttons.append(
                        {
                            "button_id": int(button_id),
                            "rect": {
                                "x1": int(rect[0]),
                                "y1": int(rect[1]),
                                "x2": int(rect[2]),
                                "y2": int(rect[3]),
                                "auto_action_mode": int(rect[4]) if len(rect) > 4 else 0,
                            },
                            "target": target,
                        }
                    )
                payload["buttons"] = buttons
        finally:
            close_fn = getattr(stream, "__dealloc__", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass
    except Exception as exc:
        payload["available"] = False
        payload["reason"] = str(exc)

    artifact_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _import_pydvdnav_module():
    candidates = (
        "pydvdnav.dvd_stream",
        "pydvdnav",
    )
    for name in candidates:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _first_nav_event(stream, navigation_event_type, timeout_seconds: int):
    start = time.monotonic()
    for event in stream:
        if time.monotonic() - start > timeout_seconds:
            return None
        event_name = str(getattr(event, "event_type", ""))
        if navigation_event_type is not None and isinstance(event, navigation_event_type):
            if getattr(event, "button_info", None):
                return event
        if event_name == "NAV_PACKET" and getattr(event, "button_info", None):
            return event
        complete = getattr(event, "complete", None)
        if callable(complete):
            try:
                complete()
            except Exception:
                pass
    return None


def _probe_button_target(
    DVDStream,
    NavigationEvent,
    drive_root: str,
    button_id: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    target: dict[str, Any] = {
        "title": None,
        "chapter": None,
        "title_program": None,
        "time_seconds": None,
    }
    start = time.monotonic()
    stream = DVDStream(device_name=drive_root)
    try:
        nav_event = _first_nav_event(stream, NavigationEvent, timeout_seconds)
        if nav_event is None:
            return target
        select = getattr(nav_event, "select_button", None)
        if callable(select):
            try:
                select(button_id)
            except Exception:
                return target
        for event in stream:
            if time.monotonic() - start > timeout_seconds:
                break
            event_name = str(getattr(event, "event_type", ""))
            if event_name in {"CELL_CHANGE", "VTS_CHANGE"}:
                try:
                    target["title"] = int(getattr(event, "title", 0) or 0) or None
                    target["chapter"] = int(getattr(event, "chapter", 0) or 0) or None
                except Exception:
                    pass
                try:
                    target["title_program"] = _safe_json_obj(stream.current_title_program)
                except Exception:
                    pass
                try:
                    target["time_seconds"] = float(stream.current_time)
                except Exception:
                    pass
                break
            complete = getattr(event, "complete", None)
            if callable(complete):
                try:
                    complete()
                except Exception:
                    pass
    except Exception:
        return target
    finally:
        close_fn = getattr(stream, "__dealloc__", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
    return target


def _safe_json_obj(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, tuple):
            return [_safe_json_obj(v) for v in value]
        if isinstance(value, list):
            return [_safe_json_obj(v) for v in value]
        if isinstance(value, dict):
            return {str(k): _safe_json_obj(v) for k, v in value.items()}
        return str(value)
