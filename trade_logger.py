import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path("data")
SIGNALS_FILE = DATA_DIR / "trade_signals.jsonl"
EXECUTIONS_FILE = DATA_DIR / "trade_executions.jsonl"
LIFECYCLE_FILE = DATA_DIR / "trade_lifecycle.csv"


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    _ensure_dir()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


def log_signal(record: dict[str, Any]) -> None:
    payload = {"type": "signal", "ts": _now_iso(), **_to_json_safe(record)}
    _append_jsonl(SIGNALS_FILE, payload)


def log_execution(record: dict[str, Any]) -> None:
    payload = {"type": "execution", "ts": _now_iso(), **_to_json_safe(record)}
    _append_jsonl(EXECUTIONS_FILE, payload)


def log_lifecycle_row(row: dict[str, Any]) -> None:
    _ensure_dir()
    exists = LIFECYCLE_FILE.exists()
    fields = [
        "ts",
        "position_id",
        "pair_label",
        "reason",
        "contracts",
        "entry_spread",
        "exit_spread",
        "spread_compression_pct",
        "hold_minutes",
        "realized_pnl",
        "direction",
        "yes_platform",
        "no_platform",
    ]
    clean = {k: _to_json_safe(row.get(k)) for k in fields}
    clean["ts"] = _now_iso()
    with open(LIFECYCLE_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(clean)
