"""
PUE Data Exporter — FastAPI 메인.

- GET  /         : 데이터 추출 HTML UI (Grafana iframe 으로도, 단독으로도 동작)
- GET  /export   : 선택 그룹/시간범위 → tar.gz 다운로드
- GET  /healthz  : liveness/readiness probe

서비스 URL 은 env 로 주입(하드코딩 금지):
  PROMETHEUS_URL (기본 http://monitoring-kube-prometheus-prometheus.monitoring:9090)
  VM_URL         (기본 http://victoria-metrics-svc:8428)
  PORT           (기본 8080)
  QUERY_TIMEOUT  (기본 120, 초)
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

import exporter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pue-exporter")

PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring:9090"
)
VM_URL = os.environ.get("VM_URL", "http://victoria-metrics-svc:8428")
PORT = int(os.environ.get("PORT", "8080"))
QUERY_TIMEOUT = int(os.environ.get("QUERY_TIMEOUT", "120"))

app = FastAPI(title="PUE Data Exporter")

# UI 체크박스 정의: (value, 라벨, 기본체크 여부)
# PUE/AI 그룹은 사용자 요청으로 메뉴에서 제외(백엔드 코드는 보존).
GROUP_UI = [
    ("gpu", "GPU (node1~4)", True),
    ("npu", "NPU (node5)", True),
    ("cpu", "CPU (전체)", False),
    ("memory", "메모리 (전체)", False),
    ("disk", "디스크 (전체)", False),
    ("network", "네트워크 (전체)", False),
]


def _fmt_for_input(epoch):
    """epoch 초 → datetime-local 입력값(YYYY-MM-DDTHH:MM, UTC)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _resolve_range(start, end):
    """start/end(다양한 포맷) → (start_epoch, end_epoch). 비어있으면 최근 1시간."""
    now = datetime.now(timezone.utc).timestamp()
    try:
        end_epoch = exporter.normalize_epoch(end) if end else now
    except Exception:
        end_epoch = now
    try:
        start_epoch = exporter.normalize_epoch(start) if start else end_epoch - 3600
    except Exception:
        start_epoch = end_epoch - 3600
    return start_epoch, end_epoch


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok", "prometheus": PROMETHEUS_URL, "vm": VM_URL})


@app.get("/", response_class=HTMLResponse)
async def home(start: str = "", end: str = ""):
    start_epoch, end_epoch = _resolve_range(start, end)
    start_in = _fmt_for_input(start_epoch)
    end_in = _fmt_for_input(end_epoch)
    span_min = round((end_epoch - start_epoch) / 60, 1)

    checkboxes = "\n".join(
        f'<label class="group"><input type="checkbox" name="groups" value="{v}"'
        f'{" checked" if d else ""}> {label}</label>'
        for v, label, d in GROUP_UI
    )

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PUE 데이터 추출</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; padding: 20px; background: #111217; color: #d8d9da; margin: 0; }}
    h2 {{ color: #ff5c5c; margin-top: 0; }}
    h3 {{ color: #ccccdc; border-bottom: 1px solid #34343b; padding-bottom: 4px; }}
    .box {{ background: #1f1f23; padding: 14px 16px; margin-bottom: 16px; border-radius: 6px; }}
    .group {{ display: inline-block; margin: 6px 14px 6px 0; cursor: pointer; }}
    input[type=datetime-local], select {{ background: #2c2c33; color: #d8d9da; border: 1px solid #44444c;
      padding: 6px; border-radius: 4px; }}
    input[type=submit] {{ background: #ff5c5c; color: #fff; padding: 10px 22px; border: 0;
      border-radius: 4px; cursor: pointer; font-size: 14px; }}
    input[type=submit]:hover {{ background: #e04848; }}
    .muted {{ color: #7b7b85; font-size: 12px; }}
    code {{ background: #2c2c33; padding: 1px 5px; border-radius: 3px; }}
  </style>
</head>
<body>
  <h2>📊 메트릭 Raw 데이터 추출</h2>

  <form action="/export" method="get">
    <div class="box">
      <h3 style="margin-top:0">시간 범위 (UTC)</h3>
      <label>시작 <input type="datetime-local" name="start" value="{start_in}"></label>
      &nbsp;~&nbsp;
      <label>종료 <input type="datetime-local" name="end" value="{end_in}"></label>
      <div class="muted">현재 범위 ≈ {span_min} 분.
        Grafana iframe 으로 열면 대시보드 시간이 자동 반영됩니다.</div>
    </div>

    <div class="box">
      <h3 style="margin-top:0">메트릭 그룹</h3>
      {checkboxes}
    </div>

    <div class="box">
      <h3 style="margin-top:0">해상도 (step)</h3>
      <select name="step">
        <option value="10s">10초</option>
        <option value="30s" selected>30초 (권장)</option>
        <option value="1m">1분</option>
        <option value="5m">5분</option>
      </select>
      <span class="muted">범위가 길수록 큰 step 권장 (timeout/용량).</span>
    </div>

    <input type="submit" value="📥 다운로드 (tar.gz)">
  </form>

  <p class="muted">출력: <b>메트릭별 wide-format CSV</b>(컬럼 = datetime, node1, node2, …) +
    <code>README.txt</code> + <code>MANIFEST.txt</code> 를 <code>export/</code> 아래 묶은 tar.gz.
    시각은 KST, Excel 바로 열림.</p>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/export")
async def export(
    start: str = Query(""),
    end: str = Query(""),
    step: str = Query("30s"),
    groups: list[str] = Query(default=[]),
):
    if not groups:
        return JSONResponse({"error": "그룹을 하나 이상 선택하세요"}, status_code=400)

    start_epoch, end_epoch = _resolve_range(start, end)
    try:
        result = exporter.export_data(
            start=start_epoch,
            end=end_epoch,
            groups=groups,
            prometheus_url=PROMETHEUS_URL,
            vm_url=VM_URL,
            step=step,
            timeout=QUERY_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("export 실패")
        return JSONResponse({"error": str(e)}, status_code=500)

    total_rows = sum(result["stats"].values())
    log.info(
        "export ok: groups=%s rows=%d errors=%d file=%s",
        groups, total_rows, len(result["errors"]), os.path.basename(result["archive_path"]),
    )

    filename = os.path.basename(result["archive_path"])
    # 응답 전송 후 작업 디렉토리 정리(임시파일 자동 삭제)
    return FileResponse(
        result["archive_path"],
        media_type="application/gzip",
        filename=filename,
        headers={"X-Export-Rows": str(total_rows), "X-Export-Errors": str(len(result["errors"]))},
        background=BackgroundTask(exporter.cleanup, result["work_dir"]),
    )


if __name__ == "__main__":
    log.info("PUE Data Exporter 시작: port=%s prometheus=%s vm=%s", PORT, PROMETHEUS_URL, VM_URL)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
