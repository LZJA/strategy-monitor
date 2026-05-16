from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR, settings
from app.core.database import get_db
from app.models import ScanRun, Signal, User
from app.scanner.daemon_strategies import run_daemon_strategy_scan
from app.services.auth import require_admin


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
scan_lock = Lock()


def parse_float(value: Optional[str]) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("%", ""))
    except ValueError:
        return None


def parse_int(value: Optional[str]) -> Optional[int]:
    parsed = parse_float(value)
    return int(parsed) if parsed is not None else None


def find_latest_scan_csv() -> Path:
    scan_dir = ROOT_DIR / "data" / "scan_results"
    csv_files = [path for path in scan_dir.rglob("*.csv") if path.is_file()]
    if not csv_files:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"没有找到扫描结果 CSV，请先把结果放到 {scan_dir}",
        )
    return max(csv_files, key=lambda path: path.stat().st_mtime)


def latest_cached_kline_date(max_date: date) -> Optional[date]:
    cache_dir = ROOT_DIR / "data" / "kline_cache"
    latest: Optional[date] = None
    for path in cache_dir.glob("*.csv"):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                last_line = ""
                for line in handle:
                    if line.strip():
                        last_line = line
            if not last_line or last_line.lower().startswith("date,"):
                continue
            row_date = date.fromisoformat(last_line.split(",", 1)[0].strip())
        except (OSError, ValueError):
            continue
        if row_date <= max_date and (latest is None or row_date > latest):
            latest = row_date
    return latest


def run_local_scanner(signal_date: date) -> list[Path]:
    scanner_dir = ROOT_DIR
    scanner_script = ROOT_DIR / "backend" / "app" / "scanner" / "pattern_scan_tool.py"
    if not scanner_script.exists():
        return []

    venv_python_candidates = [
        ROOT_DIR / "backend" / ".venv" / "Scripts" / "python.exe",
        ROOT_DIR / "backend" / ".venv" / "bin" / "python",
    ]
    python_bin = next((path for path in venv_python_candidates if path.exists()), None)
    executable = str(python_bin or sys.executable)
    command = [
        executable,
        str(scanner_script),
        settings.scanner_board,
        "--date",
        signal_date.isoformat(),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=scanner_dir,
            capture_output=True,
            text=True,
            timeout=settings.scanner_timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Local scanner Python is missing or not executable: {executable}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Local scanner timed out after {settings.scanner_timeout_seconds} seconds",
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "扫描脚本执行失败").strip()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail[-1000:])

    output_dir = ROOT_DIR / "data" / "pattern_scan_cache" / signal_date.isoformat()
    csv_files = sorted(path for path in output_dir.glob("*_matched.csv") if path.is_file())
    csv_files.extend(sorted(path for path in output_dir.glob("*_watchlist.csv") if path.is_file()))
    csv_files.extend(run_daemon_strategy_scan(ROOT_DIR, signal_date))
    return csv_files


def import_signal_csv(
    db: Session,
    csv_file: Path,
    signal_date: date,
    strategy_name: str,
    signal_type: str,
    source: str,
    board: Optional[str] = None,
    replace_date: bool = False,
) -> tuple[ScanRun, int]:
    if replace_date:
        db.execute(delete(Signal).where(Signal.signal_date == signal_date))

    scan_run = ScanRun(
        scan_date=signal_date,
        board=board,
        source=source,
        status="success",
        started_at=datetime.utcnow(),
    )
    db.add(scan_run)
    db.flush()

    imported = 0
    with csv_file.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            symbol = row.get("代码") or row.get("symbol") or row.get("code")
            if not symbol:
                continue
            payload = {k: v for k, v in row.items() if v not in (None, "")}
            if row.get("payload_json"):
                try:
                    payload.update(json.loads(row["payload_json"]))
                except json.JSONDecodeError:
                    pass
            db.add(
                Signal(
                    scan_run_id=scan_run.id,
                    signal_date=signal_date,
                    strategy_name=row.get("周期") or row.get("策略") or row.get("strategy_name") or strategy_name,
                    signal_type=row.get("类型") or row.get("signal_type") or signal_type,
                    symbol=symbol,
                    name=row.get("名称") or row.get("name"),
                    close_price=parse_float(
                        row.get("收盘价") or row.get("当前价") or row.get("信号收盘价") or row.get("close_price")
                    ),
                    high_price=parse_float(row.get("最高价") or row.get("信号最高价") or row.get("high_price")),
                    breakout_price=parse_float(row.get("突破价") or row.get("breakout_price")),
                    stop_loss_price=parse_float(row.get("止损价") or row.get("stop_loss_price")),
                    take_profit_price=parse_float(row.get("止盈价") or row.get("take_profit_price")),
                    amount_rank=parse_int(row.get("成交额排名") or row.get("成交额名次") or row.get("amount_rank")),
                    payload_json=json.dumps(payload, ensure_ascii=False),
                )
            )
            imported += 1

    scan_run.finished_at = datetime.utcnow()
    db.commit()
    db.refresh(scan_run)
    return scan_run, imported


@router.post("/scan/today")
def scan_today(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="已有扫描任务正在运行，请稍后再试")
    try:
        signal_date = latest_cached_kline_date(date.today()) or date.today()
        csv_files = run_local_scanner(signal_date)
        if not csv_files:
            csv_files = [find_latest_scan_csv()]

        imported = 0
        scan_run: Optional[ScanRun] = None
        for index, csv_file in enumerate(csv_files):
            current_run, current_imported = import_signal_csv(
                db=db,
                csv_file=csv_file,
                signal_date=signal_date,
                strategy_name="manual_scan",
                signal_type="watch" if "watchlist" in csv_file.stem else "matched",
                source=f"manual_scan:{admin.username}:{csv_file.name}",
                replace_date=index == 0,
            )
            scan_run = current_run
            imported += current_imported

        source_files = [
            str(path.relative_to(ROOT_DIR)) if path.is_relative_to(ROOT_DIR) else str(path) for path in csv_files
        ]
        if not settings.keep_scan_artifacts:
            cache_root = ROOT_DIR / "data" / "pattern_scan_cache"
            for folder in {path.parent for path in csv_files if path.is_relative_to(cache_root)}:
                shutil.rmtree(folder, ignore_errors=True)
        return {
            "scan_run_id": scan_run.id if scan_run else None,
            "scan_date": signal_date,
            "source_file": ", ".join(source_files),
            "imported": imported,
        }
    finally:
        scan_lock.release()


@router.post("/scan/import-csv")
async def import_csv(
    file: UploadFile = File(...),
    signal_date: date = Form(...),
    strategy_name: str = Form("unknown"),
    signal_type: str = Form("matched"),
    board: Optional[str] = Form(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only CSV files are supported")

    run_dir = ROOT_DIR / "data" / "tmp" / f"import_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    saved_file = run_dir / file.filename
    saved_file.write_bytes(await file.read())

    scan_run, imported = import_signal_csv(
        db=db,
        csv_file=saved_file,
        signal_date=signal_date,
        strategy_name=strategy_name,
        signal_type=signal_type,
        source=f"csv_import:{admin.username}",
        board=board,
    )

    if not settings.keep_scan_artifacts:
        shutil.rmtree(run_dir, ignore_errors=True)

    return {"scan_run_id": scan_run.id, "imported": imported}


@router.delete("/signals")
def delete_signals(
    signal_date: date,
    strategy_name: Optional[str] = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    del admin
    stmt = delete(Signal).where(Signal.signal_date == signal_date)
    if strategy_name:
        stmt = stmt.where(Signal.strategy_name == strategy_name)
    result = db.execute(stmt)
    db.commit()
    return {
        "deleted": result.rowcount or 0,
        "signal_date": signal_date,
        "strategy_name": strategy_name,
    }
