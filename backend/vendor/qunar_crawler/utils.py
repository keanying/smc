from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_SPACE_RE = re.compile(r"[ \t\f\v]+")
_NEWLINE_RE = re.compile(r"\s*\n\s*")


def clean_text(value: object, *, preserve_newlines: bool = False) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text)
    if preserve_newlines:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n".join(lines)
    return re.sub(r"\s+", " ", text).strip()


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def stable_id(*parts: object, prefix: str = "") -> str:
    raw = "\x1f".join(clean_text(part, preserve_newlines=True) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


def china_now() -> str:
    try:
        china_timezone = ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        # Windows Python installations may not include the IANA timezone
        # database. China Standard Time has remained UTC+08:00 since 1991,
        # so a fixed offset is a safe fallback for current crawl timestamps.
        china_timezone = timezone(timedelta(hours=8), name="Asia/Shanghai")
    return datetime.now(china_timezone).strftime("%Y-%m-%d %H:%M:%S")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_existing_keys(path: Path, key: str) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row.get(key, "") for row in csv.DictReader(handle) if row.get(key)}


def append_csv_rows(
    path: Path,
    columns: Sequence[str],
    rows: Iterable[Mapping[str, object]],
    *,
    overwrite: bool = False,
) -> int:
    ensure_parent(path)
    has_content = path.exists() and path.stat().st_size > 0 and not overwrite
    mode = "a" if has_content else "w"
    count = 0
    with path.open(mode, encoding="utf-8-sig" if mode == "w" else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        if not has_content:
            writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
            count += 1
    return count


def append_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
