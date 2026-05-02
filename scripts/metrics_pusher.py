"""메트릭 Push 스크립트.

node3에서 EDAC CE/UE, IPMI 센서, ESXi 메트릭을
node2 VictoriaMetrics에 주기적으로 push한다.

VictoriaMetrics는 InfluxDB line protocol (/write)과
Prometheus remote write를 모두 지원.
여기서는 import API (/api/v1/import/prometheus)를 사용.

실행: python scripts/metrics_pusher.py
데몬: systemd 서비스 또는 cron으로 실행
"""

import logging
import os
import sys
import time

import requests

sys.path.insert(0, "/opt/failure_prediction")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VM_URL = os.getenv("VICTORIA_METRICS_URL", "http://10.100.230.72:8428")
PUSH_INTERVAL = 60  # 초


def push_metrics(lines: list[str]) -> bool:
    """메트릭을 VictoriaMetrics에 InfluxDB line protocol로 push한다.

    Prometheus 형식 라인을 InfluxDB line protocol로 변환하여 /write 엔드포인트에 전송.
    /write는 /api/v1/import/prometheus 대비 즉시 반영이 보장됨.

    Args:
        lines: Prometheus exposition 형식 라인 리스트.
              예: 'metric_name{label="val"} 0.5'

    Returns:
        성공 여부.
    """
    influx_lines = []
    for line in lines:
        converted = _prometheus_to_influx(line)
        if converted:
            influx_lines.append(converted)

    if not influx_lines:
        return False

    data = "\n".join(influx_lines) + "\n"
    try:
        resp = requests.post(
            f"{VM_URL}/write",
            data=data,
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        if resp.status_code == 204:
            return True
        logger.warning("Push 실패: %d %s", resp.status_code, resp.text[:100])
        return False
    except Exception:
        logger.exception("VictoriaMetrics push 오류")
        return False


def _prometheus_to_influx(prom_line: str) -> str:
    """Prometheus exposition 형식을 InfluxDB line protocol로 변환한다.

    입력: 'metric_name{label1="val1",label2="val2"} 0.5'
    출력: 'metric_name,label1=val1,label2=val2 value=0.5'

    Args:
        prom_line: Prometheus 형식 라인.

    Returns:
        InfluxDB line protocol 문자열.
    """
    import re
    prom_line = prom_line.strip()
    if not prom_line:
        return ""

    # {labels} 있는 경우
    match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\{(.+?)\}\s+(.+?)(\s+\d+)?$', prom_line)
    if match:
        metric = match.group(1)
        labels_str = match.group(2)
        value = match.group(3)
        # labels: key="val",key2="val2" → key=val,key2=val2
        tags = []
        for pair in labels_str.split(","):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                v = v.strip('"').strip("'")
                tags.append(f"{k.strip()}={v}")
        tag_str = "," + ",".join(tags) if tags else ""
        return f"{metric}{tag_str} value={value}"

    # {labels} 없는 경우
    match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s+(.+?)(\s+\d+)?$', prom_line)
    if match:
        metric = match.group(1)
        value = match.group(2)
        return f"{metric} value={value}"

    return ""


def collect_edac_metrics() -> list[str]:
    """EDAC CE/UE 메트릭을 수집한다.

    Returns:
        Prometheus 형식 메트릭 라인 리스트.
    """
    lines = []
    try:
        from src.collectors.edac_collector import EdacCollector
        collector = EdacCollector()
        status = collector.collect()

        lines.append(
            f'memory_errors_ce_total{{server="18AFD199"}} {status.total_ce}'
        )
        lines.append(
            f'memory_errors_ue_total{{server="18AFD199"}} {status.total_ue}'
        )

        for event in status.error_events:
            loc = event.dimm_loc
            lines.append(
                f'memory_errors{{server="18AFD199",mc="{loc.mc}",csrow="{loc.csrow}",channel="{loc.channel}"}} '
                f"{event.ce_count}"
            )
    except Exception:
        logger.debug("EDAC 수집 실패 (edac-util 미설치일 수 있음), 기본값 사용")
        lines.append('memory_errors_ce_total{server="18AFD199"} 0')
        lines.append('memory_errors_ue_total{server="18AFD199"} 0')
        lines.append('memory_errors{server="18AFD199",mc="0",csrow="0",channel="0"} 0')

    return lines


def collect_ipmi_metrics() -> list[str]:
    """IPMI 센서 메트릭을 수집한다.

    Returns:
        Prometheus 형식 메트릭 라인 리스트.
    """
    lines = []
    try:
        from src.collectors.ipmi_collector import IpmiCollector
        collector = IpmiCollector()
        data = collector.collect()

        for temp in data.temperatures:
            lines.append(
                f'ipmi_temperature{{server="18AFD199",name="{temp.name}"}} {temp.value}'
            )
        for fan in data.fans:
            lines.append(
                f'ipmi_fan{{server="18AFD199",name="{fan.name}"}} {fan.value}'
            )
        for power in data.power:
            lines.append(
                f'ipmi_power{{server="18AFD199",name="{power.name}"}} {power.value}'
            )
    except Exception:
        logger.debug("IPMI 수집 실패 (ipmitool 미설치일 수 있음)")

    return lines


def collect_esxi_metrics() -> list[str]:
    """ESXi 호스트 메트릭을 수집한다.

    Returns:
        Prometheus 형식 메트릭 라인 리스트.
    """
    lines = []
    esxi_hosts = [
        ("vmgnode18", "10.148.148.118"),
        ("vmgnode23", "10.148.148.123"),
        ("vmgnode26", "10.148.148.126"),
        ("vmgnode30", "10.148.148.130"),
    ]

    for host_id, host_ip in esxi_hosts:
        try:
            from src.collectors.esxi_collector import EsxiCollector
            collector = EsxiCollector(
                host_id=host_id, host_ip=host_ip,
                username="root", password=os.getenv("ESXI_PASSWORD", "VMware!0"),
            )
            data = collector.collect()

            lines.append(f'esxi_cpu_usage{{host="{host_id}"}} {data.host_metrics.cpu_usage_percent:.2f}')
            lines.append(f'esxi_mem_usage{{host="{host_id}"}} {data.host_metrics.mem_usage_percent:.2f}')
            lines.append(f'esxi_vm_count{{host="{host_id}"}} {data.vm_aggregate.vm_count}')
            lines.append(f'esxi_vm_balloon{{host="{host_id}"}} {data.vm_aggregate.mem_balloon_sum_mb:.0f}')
            lines.append(f'esxi_cpu_ready{{host="{host_id}"}} {data.vm_aggregate.cpu_ready_sum_ms:.0f}')
            lines.append(f'esxi_mem_swapped{{host="{host_id}"}} {data.vm_aggregate.mem_swapped_sum_mb:.0f}')
            lines.append(f'esxi_maintenance{{host="{host_id}"}} {1 if data.maintenance_mode else 0}')
        except Exception:
            logger.warning("ESXi 수집 실패: %s", host_id)

    # ESXi SSH vmkernel 에러 수집
    for host_id, host_ip in esxi_hosts:
        try:
            from src.collectors.esxi_ssh_collector import EsxiSshCollector
            ssh_collector = EsxiSshCollector(
                host_id=host_id, host_ip=host_ip,
                username="root", password=os.getenv("ESXI_PASSWORD", "VMware!0"),
            )
            ssh_data = ssh_collector.collect()
            lines.append(f'esxi_vmkernel_error_cnt{{host="{host_id}"}} {ssh_data.error_count}')
        except Exception:
            logger.warning("ESXi SSH 수집 실패: %s", host_id)

    return lines


def collect_prediction_metrics() -> list[str]:
    """FastAPI 추론 결과를 메트릭으로 수집한다.

    Returns:
        Prometheus 형식 메트릭 라인 리스트.
    """
    lines = []
    api_url = "http://127.0.0.1:8000"

    servers = ["vmgnode18", "vmgnode23", "vmgnode26", "vmgnode30", "18AFD199"]
    for server_id in servers:
        try:
            resp = requests.get(f"{api_url}/predict/{server_id}", timeout=120)
            if resp.status_code != 200:
                continue
            data = resp.json()
            lines.append(
                f'failure_probability{{server="{server_id}"}} {data["failure_probability"]:.4f}'
            )
            for model_name, score in data.get("model_scores", {}).items():
                lines.append(
                    f'model_score{{server="{server_id}",model="{model_name}"}} {score:.4f}'
                )
        except Exception:
            logger.warning("추론 메트릭 수집 실패: %s", server_id)

    return lines


def run_once() -> None:
    """1회 수집 + push."""
    all_lines = []
    all_lines.extend(collect_edac_metrics())
    all_lines.extend(collect_ipmi_metrics())
    all_lines.extend(collect_esxi_metrics())
    all_lines.extend(collect_prediction_metrics())

    if all_lines:
        ok = push_metrics(all_lines)
        logger.info("Push %d개 메트릭 → %s: %s", len(all_lines), VM_URL, "성공" if ok else "실패")


def run_daemon() -> None:
    """주기적 수집 데몬."""
    logger.info("메트릭 수집 데몬 시작 (interval=%ds, target=%s)", PUSH_INTERVAL, VM_URL)
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("수집 주기 오류")
        time.sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    else:
        run_once()
