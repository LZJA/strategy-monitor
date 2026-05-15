from __future__ import annotations

import csv
import json
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR, settings
from app.core.database import get_db
from app.models import ScanRun, Signal, User
from app.services.auth import require_admin


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


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


def run_external_scanner(signal_date: date) -> list[Path]:
    scanner_dir = Path(settings.scanner_project_path)
    scanner_script = scanner_dir / "pattern_scan_tool.py"
    if not scanner_script.exists():
        return []

    python_bin = scanner_dir / ".venv" / "bin" / "python"
    executable = str(python_bin if python_bin.exists() else "python3")
    command = [
        executable,
        str(scanner_script),
        settings.scanner_board,
        "--date",
        signal_date.isoformat(),
    ]
    result = subprocess.run(
        command,
        cwd=scanner_dir,
        capture_output=True,
        text=True,
        timeout=settings.scanner_timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "扫描脚本执行失败").strip()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail[-1000:])

    output_dir = scanner_dir / "data" / "pattern_scan_cache" / signal_date.isoformat()
    return sorted(path for path in output_dir.glob("*_matched.csv") if path.is_file())


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
    signal_date = date.today()
    csv_files = run_external_scanner(signal_date)
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
            signal_type="matched",
            source=f"manual_scan:{admin.username}:{csv_file.name}",
            replace_date=index == 0,
        )
        scan_run = current_run
        imported += current_imported

    source_files = [str(path.relative_to(ROOT_DIR)) if path.is_relative_to(ROOT_DIR) else str(path) for path in csv_files]
    return {
        "scan_run_id": scan_run.id if scan_run else None,
        "scan_date": signal_date,
        "source_file": ", ".join(source_files),
        "imported": imported,
    }


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
