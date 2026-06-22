"""
PUE Data Exporter — wide-format 추출 로직 (Excel 친화).

메트릭마다 파일 하나, 컬럼은 [datetime, node1, node2, ...].
계산형/단위변환은 PromQL 표현식으로 처리해 노드별 1시계열을 만든 뒤 pivot 한다.

- 노드 식별: GPU(DCGM)=Hostname, NPU(furiosa)=hostname, node_exporter=instance IP→노드명
- 시각 컬럼: KST(UTC+9), "YYYY-MM-DD HH:MM:SS" (Excel 자동 인식)
- CSV 인코딩: utf-8-sig(BOM) — Excel 한글 안깨짐

URL 은 하드코딩 금지(app.py 가 env 로 주입).
"""

import csv
import os
import shutil
import tarfile
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))

# node_exporter instance IP → 노드명 (하드코딩, 사용자 확정)
IP_MAP = {
    "10.100.230.130": "node1",
    "10.100.230.131": "node2",
    "10.100.230.132": "node3",
    "10.100.230.133": "node4",
    "10.100.230.134": "node5",
}

GPU_NODES = ["node1", "node2", "node3", "node4"]   # node1=control-plane(GPU 부하 미참여)
NPU_NODES = ["node5"]
ALL_NODES = ["node1", "node2", "node3", "node4", "node5"]

# ----------------------------------------------------------------------------
# 출력 메트릭 정의 (wide-format). query 안의 __RATE__ 는 step 기반 rate window 로 치환.
#   node_from: 'Hostname' | 'hostname' | 'instance_ip'
# ----------------------------------------------------------------------------
OUTPUT_METRICS = [
    # ----- GPU (node1~4) -----
    {"group": "gpu", "file": "gpu_usage_pct.csv", "title": "GPU 사용률", "unit": "%",
     "source": "prometheus", "node_from": "Hostname", "nodes": GPU_NODES,
     "query": "DCGM_FI_DEV_GPU_UTIL"},
    {"group": "gpu", "file": "gpu_power_w.csv", "title": "GPU 전력 소비", "unit": "W",
     "source": "prometheus", "node_from": "Hostname", "nodes": GPU_NODES,
     "query": "DCGM_FI_DEV_POWER_USAGE"},
    {"group": "gpu", "file": "gpu_memory_used_gb.csv", "title": "GPU 메모리 사용량", "unit": "GB",
     "source": "prometheus", "node_from": "Hostname", "nodes": GPU_NODES,
     "query": "DCGM_FI_DEV_FB_USED / 1024"},
    {"group": "gpu", "file": "gpu_memory_total_gb.csv", "title": "GPU 메모리 총량", "unit": "GB",
     "source": "prometheus", "node_from": "Hostname", "nodes": GPU_NODES,
     "query": "(DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE) / 1024"},
    {"group": "gpu", "file": "gpu_temperature_c.csv", "title": "GPU 온도", "unit": "°C",
     "source": "prometheus", "node_from": "Hostname", "nodes": GPU_NODES,
     "query": "DCGM_FI_DEV_GPU_TEMP"},

    # ----- NPU (node5) — core_utilization 은 8코어 평균, 온도는 peak -----
    {"group": "npu", "file": "npu_usage_pct.csv", "title": "NPU 사용률(8코어 평균)", "unit": "%",
     "source": "prometheus", "node_from": "hostname", "nodes": NPU_NODES,
     "query": "avg by(hostname)(furiosa_npu_core_utilization)"},
    {"group": "npu", "file": "npu_power_w.csv", "title": "NPU 전력 소비", "unit": "W",
     "source": "prometheus", "node_from": "hostname", "nodes": NPU_NODES,
     "query": "avg by(hostname)(furiosa_npu_hw_power)"},
    {"group": "npu", "file": "npu_memory_used_mb.csv", "title": "NPU 메모리 사용량", "unit": "MB",
     "source": "prometheus", "node_from": "hostname", "nodes": NPU_NODES,
     "query": "avg by(hostname)(furiosa_npu_dram_usage) / 1024 / 1024"},
    {"group": "npu", "file": "npu_memory_total_mb.csv", "title": "NPU 메모리 총량", "unit": "MB",
     "source": "prometheus", "node_from": "hostname", "nodes": NPU_NODES,
     "query": "avg by(hostname)(furiosa_npu_dram_total) / 1024 / 1024"},
    {"group": "npu", "file": "npu_temperature_c.csv", "title": "NPU 온도(peak)", "unit": "°C",
     "source": "prometheus", "node_from": "hostname", "nodes": NPU_NODES,
     "query": "avg by(hostname)(furiosa_npu_hw_temperature{label=\"peak\"})"},

    # ----- CPU (node1~5) -----
    {"group": "cpu", "file": "cpu_usage_pct.csv", "title": "CPU 사용률", "unit": "%",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "100 - avg by(instance)(rate(node_cpu_seconds_total{mode=\"idle\"}[__RATE__])) * 100"},
    {"group": "cpu", "file": "cpu_load1.csv", "title": "Load Average 1분", "unit": "load",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "node_load1"},

    # ----- 메모리 (node1~5) -----
    {"group": "memory", "file": "memory_used_gb.csv", "title": "메모리 사용량", "unit": "GB",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / 1024 / 1024 / 1024"},
    {"group": "memory", "file": "memory_used_pct.csv", "title": "메모리 사용률", "unit": "%",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100"},
    {"group": "memory", "file": "memory_total_gb.csv", "title": "메모리 총량", "unit": "GB",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "node_memory_MemTotal_bytes / 1024 / 1024 / 1024"},

    # ----- 디스크 (node1~5, mountpoint=/) -----
    {"group": "disk", "file": "disk_used_pct.csv", "title": "디스크 사용률(/)", "unit": "%",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "(1 - node_filesystem_avail_bytes{mountpoint=\"/\"} / node_filesystem_size_bytes{mountpoint=\"/\"}) * 100"},
    {"group": "disk", "file": "disk_used_gb.csv", "title": "디스크 사용량(/)", "unit": "GB",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "(node_filesystem_size_bytes{mountpoint=\"/\"} - node_filesystem_avail_bytes{mountpoint=\"/\"}) / 1024 / 1024 / 1024"},

    # ----- 네트워크 (node1~5, 물리 NIC en* 합산) -----
    {"group": "network", "file": "network_tx_mb_per_sec.csv", "title": "네트워크 송신", "unit": "MB/s",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "sum by(instance)(rate(node_network_transmit_bytes_total{device=~\"en.*\"}[__RATE__])) / 1024 / 1024"},
    {"group": "network", "file": "network_rx_mb_per_sec.csv", "title": "네트워크 수신", "unit": "MB/s",
     "source": "prometheus", "node_from": "instance_ip", "nodes": ALL_NODES,
     "query": "sum by(instance)(rate(node_network_receive_bytes_total{device=~\"en.*\"}[__RATE__])) / 1024 / 1024"},
]

# UI/처리 그룹 순서
GROUP_ORDER = ["gpu", "npu", "cpu", "memory", "disk", "network"]

# 참고용 — PUE/AI 는 wide-format 메뉴에서 제외(사용자 불필요). 필요시 부활용으로 정의만 보존.
LEGACY_VM_GROUPS = {
    "pue": ["pue_target_gpu_util", "pue_gpu_vram_pct", "pue_training_batch_size",
            "pue_training_loss", "pue_training_sleep", "pue_training_step"],
    "ai": ["failure_pred_score", "failure_pred_risk_level",
           "failure_pred_model_score", "failure_prediction_up"],
}


# ----------------------------------------------------------------------------
# 시간/duration 유틸
# ----------------------------------------------------------------------------
def normalize_epoch(value):
    """epoch ms/s, ISO8601, datetime-local → unix epoch 초(float)."""
    if value is None:
        raise ValueError("시간값이 없습니다")
    s = str(value).strip()
    if not s:
        raise ValueError("시간값이 비어있습니다")
    try:
        num = float(s)
        return num / 1000.0 if num > 1e12 else num
    except ValueError:
        pass
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_step_seconds(step):
    """'30s','1m','5m','1h' → 초."""
    step = str(step).strip().lower()
    unit = step[-1]
    mult = {"s": 1, "m": 60, "h": 3600}.get(unit)
    if mult is None:  # 숫자만이면 초로 간주
        return int(float(step))
    return int(float(step[:-1]) * mult)


def rate_window(step):
    """rate() 윈도우: max(120s, step*4). 분 단위로 떨어지면 'Nm' 아니면 'Ns'."""
    sec = max(120, parse_step_seconds(step) * 4)
    return f"{sec // 60}m" if sec % 60 == 0 else f"{sec}s"


def fmt_kst(epoch):
    return datetime.fromtimestamp(float(epoch), tz=KST).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------
# 쿼리 / 노드 식별 / pivot
# ----------------------------------------------------------------------------
def query_range(source_url, query, start_epoch, end_epoch, step="30s", timeout=120):
    r = requests.get(
        f"{source_url.rstrip('/')}/api/v1/query_range",
        params={"query": query, "start": start_epoch, "end": end_epoch, "step": step},
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"query 실패: {payload.get('error', payload)}")
    return payload["data"]["result"]


def node_of(metric, strategy):
    """series 라벨에서 노드명 추출. 못 찾으면 None."""
    if strategy == "Hostname":
        return metric.get("Hostname")
    if strategy == "hostname":
        return metric.get("hostname")
    if strategy == "instance_ip":
        inst = metric.get("instance", "")
        ip = inst.split(":")[0]
        return IP_MAP.get(ip)
    return None


def _round(v):
    """문자열 값 → 보기 좋은 숫자(소수 2자리). NaN/파싱불가 → None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, 2)


def to_wide_csv(result, spec, csv_path):
    """query_range result → wide CSV [datetime, node...]. 반환: 데이터 행 수."""
    nodes = spec["nodes"]
    # ts(int) -> {node: value}
    table = {}
    for series in result:
        node = node_of(series.get("metric", {}), spec["node_from"])
        if node is None or node not in nodes:
            continue
        for ts, val in series.get("values", []):
            ts_i = int(float(ts))
            table.setdefault(ts_i, {})[node] = _round(val)

    rows = 0
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime"] + nodes)
        for ts in sorted(table.keys()):
            row = [fmt_kst(ts)]
            for n in nodes:
                v = table[ts].get(n)
                row.append("" if v is None else v)
            writer.writerow(row)
            rows += 1
    return rows


# ----------------------------------------------------------------------------
# README / MANIFEST
# ----------------------------------------------------------------------------
NODE_INFO = [
    ("node1", "10.100.230.130", "control-plane, RTX 5060 Ti, GPU 부하 미참여"),
    ("node2", "10.100.230.131", "worker, RTX 5060 Ti"),
    ("node3", "10.100.230.132", "worker, RTX 5060 Ti"),
    ("node4", "10.100.230.133", "worker, RTX 5080"),
    ("node5", "10.100.230.134", "worker, Furiosa RNGD NPU"),
]


def write_readme(path, specs, start_epoch, end_epoch, groups):
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("PUE 측정/분석 데이터\n")
        f.write("─────────────────\n")
        f.write(f"추출 범위(KST): {fmt_kst(start_epoch)} ~ {fmt_kst(end_epoch)}\n")
        f.write(f"선택 그룹: {', '.join(groups)}\n")
        f.write("시각 기준: KST (UTC+9)\n\n")
        f.write("[파일 설명]\n")
        for s in specs:
            f.write(f"  {s['file']:26s} {s['title']:18s} 단위: {s['unit']}\n")
        f.write("\n[노드 정보]\n")
        for name, ip, desc in NODE_INFO:
            f.write(f"  {name}: {ip} ({desc})\n")
        f.write("\n[Excel 사용 팁]\n")
        f.write("  1. CSV 더블클릭 → Excel 에서 열림 (UTF-8 BOM 포함, 한글 안깨짐)\n")
        f.write("  2. 첫 행이 헤더: datetime, node1, node2, ...\n")
        f.write("  3. datetime 은 'YYYY-MM-DD HH:MM:SS' (KST) — Excel 이 날짜로 자동 인식\n")
        f.write("  4. 차트: 데이터 전체 선택 → 삽입 → 꺾은선형\n")
        f.write("  5. 빈 셀 = 해당 시각 그 노드 데이터 없음(스크레이프 누락 등)\n")


def write_manifest(path, stats, start_epoch, end_epoch, groups, step, rwin, errors):
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("PUE Data Export — MANIFEST\n")
        f.write(f"Export 시각(KST): {fmt_kst(datetime.now(timezone.utc).timestamp())}\n")
        f.write(f"데이터 범위(KST): {fmt_kst(start_epoch)} ~ {fmt_kst(end_epoch)}\n")
        f.write(f"데이터 범위(UTC): "
                f"{datetime.fromtimestamp(start_epoch, tz=timezone.utc).isoformat()} ~ "
                f"{datetime.fromtimestamp(end_epoch, tz=timezone.utc).isoformat()}\n")
        f.write(f"해상도(step): {step}   rate window: {rwin}\n")
        f.write(f"선택 그룹: {', '.join(groups)}\n\n")
        f.write("[파일별 행 수]\n")
        for fn, n in stats.items():
            f.write(f"  {fn:26s} {n:>8d} 행\n")
        if errors:
            f.write("\n[경고: 일부 쿼리 실패]\n")
            for e in errors:
                f.write(f"  - {e}\n")


# ----------------------------------------------------------------------------
# 메인 export
# ----------------------------------------------------------------------------
def export_data(start, end, groups, prometheus_url, vm_url, step="30s", timeout=120):
    start_epoch = normalize_epoch(start)
    end_epoch = normalize_epoch(end)
    if end_epoch <= start_epoch:
        raise ValueError(f"종료시간이 시작보다 빠르거나 같습니다 (start={start_epoch}, end={end_epoch})")

    rwin = rate_window(step)
    selected = [g for g in GROUP_ORDER if g in groups]
    specs = [s for s in OUTPUT_METRICS if s["group"] in selected]

    work_dir = tempfile.mkdtemp(prefix="pue-export-")
    payload_dir = os.path.join(work_dir, "export")
    os.makedirs(payload_dir, exist_ok=True)

    stats = {}      # file -> rows
    errors = []     # "file: 사유"

    for spec in specs:
        source_url = prometheus_url if spec["source"] == "prometheus" else vm_url
        query = spec["query"].replace("__RATE__", rwin)
        csv_path = os.path.join(payload_dir, spec["file"])
        try:
            result = query_range(source_url, query, start_epoch, end_epoch, step, timeout)
            rows = to_wide_csv(result, spec, csv_path)
            stats[spec["file"]] = rows
        except Exception as e:  # noqa: BLE001 — 개별 파일 실패는 빈 파일로 남기고 계속
            errors.append(f"{spec['file']}: {e}")
            # 헤더만 있는 빈 파일 생성(누락 표시)
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(["datetime"] + spec["nodes"])
            stats[spec["file"]] = 0

    write_readme(os.path.join(payload_dir, "README.txt"), specs, start_epoch, end_epoch, selected)
    write_manifest(os.path.join(payload_dir, "MANIFEST.txt"), stats,
                   start_epoch, end_epoch, selected, step, rwin, errors)

    archive_path = os.path.join(
        work_dir,
        f"pue-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.tar.gz",
    )
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(payload_dir, arcname="export")

    return {
        "archive_path": archive_path,
        "work_dir": work_dir,
        "stats": stats,
        "errors": errors,
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "step": step,
        "rate_window": rwin,
    }


def cleanup(work_dir):
    shutil.rmtree(work_dir, ignore_errors=True)
