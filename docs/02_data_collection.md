# 02. 데이터 수집

## 수집 대상 및 도구

| 소스 | 도구 | 주기 | 비고 |
|---|---|---|---|
| DRAM CE/UE 에러 | rasdaemon + edac-util | 60초 | node3 (18AFD199) 로컬 |
| CPU 온도/전압/팬/전력 | ipmitool (BMC) | 60초 | node3 (18AFD199) BMC |
| SSD/HDD SMART | smartctl | 600초 | node3 (18AFD199) 로컬 |
| ESXi 호스트 메트릭 | pyVmomi (API) | 60초 | 4대 직접 연결 |
| ESXi 커널 로그/DIMM | **SSH 읽기** | 300초 | vmkernel.log |
| OS 시스템 메트릭 | Telegraf (node2 / 18AFD226) | 60초 | VictoriaMetrics 저장 |

---

## rasdaemon + edac-util 설치 (node3 / 18AFD199)

```bash
sudo dnf install -y rasdaemon edac-utils
sudo systemctl enable --now rasdaemon

# DIMM 구조 확인
edac-util -v
# 예: mc0/csrow0/ch0: 0 Uncorrected Errors, 2 Corrected Errors

# rasdaemon DB 확인
ls /var/lib/rasdaemon/ras-mc_event.db
```

---

## src/collectors/edac_collector.py 명세

```python
"""
수집 항목:
  - edac-util: mc/csrow/channel별 CE/UE 카운트
  - rasdaemon SQLite: 신규 MCE 이벤트 (last_id 관리)
  - dmidecode --type 17: DIMM 슬롯 물리 위치 매핑

DataClass:
  DimmLocation(mc, csrow, channel, slot_label, socket_id)
  MemoryErrorEvent(timestamp, dimm_loc, ce_count, ue_count, address, error_type)
"""
```

---

## src/collectors/esxi_ssh_collector.py 명세 (신규)

```python
"""
ESXi SSH 읽기로 추가 수집 (pyVmomi 대비 더 많은 정보)

수집 항목:
  1. vmkernel 로그 (메모리 에러 키워드 필터)
     ssh root@{ip} 'grep -i "memory\|DRAM\|ECC\|correctable" /var/log/vmkernel.log | tail -500'

  2. DIMM 물리 정보
     ssh root@{ip} 'esxcli hardware ipmi fru list'

  3. 메모리 상세 통계
     ssh root@{ip} 'esxcli hardware memory get'
     ssh root@{ip} 'esxcli system stats memory get'

  4. 호스트 요약
     ssh root@{ip} 'vim-cmd hostsvc/hostsummary'

주의:
  - 읽기 전용 명령만 허용
  - 설치/변경 명령 절대 사용 금지
  - paramiko SSH 클라이언트 사용
  - timeout=10초 설정 필수
"""
```

---

## src/collectors/esxi_collector.py 명세 (pyVmomi)

```python
"""
pyVmomi 직접 연결 (vCenter 미사용)

수집 메트릭:
  호스트: cpu.usage, mem.usage, mem.consumed, mem.swapinRate,
          net.errorsRx, net.errorsTx, power.power, sys.uptime
  VM 집계: vm_count, mem_balloon_sum, cpu_ready_sum, mem_swapped_sum

연결:
  SmartConnect(host=esxi_ip, user='root', pwd='VMware!0',
               sslContext=no_verify_context)
"""
```

---

## Telegraf 설정 (node2 / 18AFD226 배포)

```toml
[global_tags]
  datacenter = "TTA-HPC"

[agent]
  interval = "60s"

[[outputs.influxdb_v2]]
  urls = ["http://10.100.230.72:8428"]
  bucket = "hpc_metrics"

[[inputs.ipmi_sensor]]
  servers = ["root:qwe123@localhost"]
  metric_version = 2

[[inputs.smart]]
  path = "/usr/bin/smartctl"
  interval = "600s"

[[inputs.cpu]]
  percpu = true
[[inputs.mem]]
[[inputs.disk]]
  ignore_fs = ["tmpfs", "devtmpfs"]

[[inputs.exec]]
  commands = ["/opt/failure_prediction/scripts/collect_edac.sh"]
  timeout = "10s"
  data_format = "influx"
  interval = "60s"
```

---

## VictoriaMetrics 메트릭 네이밍

```
memory_errors{server="18AFD199", mc="0", channel="0"}  ce=5, ue=0
ipmi_temperature{server="18AFD199", name="CPU1 Temp"}  value=72.0
esxi_cpu_usage{host="vmgnode18"}                    value=45.2
esxi_vm_balloon{host="vmgnode18"}                   value=2048
esxi_vmkernel_error_cnt{host="vmgnode18"}           value=3    ← SSH 수집 신규
```
