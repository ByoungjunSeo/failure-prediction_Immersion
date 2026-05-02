"""학습 데이터 확보 자동화 시나리오.

ESXi 4대에 VM 자동 배포 + node3 워크로드 + 가상 CE 패턴 push.
모델이 정상/이상을 구분할 수 있는 다양한 데이터를 생성한다.

실행: python scripts/auto_training_scenario.py [--phase 1|2|3|4|all]
"""

import argparse
import logging
import os
import ssl
import subprocess
import sys
import time
from datetime import datetime

import numpy as np
import requests

sys.path.insert(0, "/opt/failure_prediction")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VM_URL = "http://10.100.230.72:8428"
ESXI_HOSTS = [
    {"id": "vmgnode18", "ip": "10.148.148.118"},
    {"id": "vmgnode23", "ip": "10.148.148.123"},
    {"id": "vmgnode26", "ip": "10.148.148.126"},
    {"id": "vmgnode30", "ip": "10.148.148.130"},
]
ESXI_USER = "root"
ESXI_PASS = "VMware!0"
ISO_PATH = "[SAN-HITACHI-01] ISO/CentOS-7-x86_64-DVD-1810.iso"
DATASTORE = "SAN-HITACHI-01"


# ══════════════════════════════════════════════
#  Phase 1: ESXi VM 자동 배포
# ══════════════════════════════════════════════

def create_vm_on_esxi(host_ip: str, host_id: str, vm_name: str,
                       cpu: int = 4, mem_gb: int = 16, disk_gb: int = 50) -> bool:
    """pyVmomi로 ESXi에 VM을 생성한다.

    Args:
        host_ip: ESXi IP.
        host_id: 호스트 식별자.
        vm_name: VM 이름.
        cpu: vCPU 수.
        mem_gb: 메모리 (GB).
        disk_gb: 디스크 (GB).

    Returns:
        성공 여부.
    """
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim

    try:
        context = ssl._create_unverified_context()
        si = SmartConnect(host=host_ip, user=ESXI_USER, pwd=ESXI_PASS, sslContext=context)
        content = si.RetrieveContent()

        # 데이터센터
        datacenter = content.rootFolder.childEntity[0]
        vm_folder = datacenter.vmFolder

        # 호스트
        host_list = datacenter.hostFolder.childEntity
        compute_resource = host_list[0]
        resource_pool = compute_resource.resourcePool

        # 데이터스토어
        datastore = None
        for ds in compute_resource.host[0].datastore:
            if DATASTORE in ds.name:
                datastore = ds
                break

        if not datastore:
            logger.error("데이터스토어 %s 를 찾을 수 없음: %s", DATASTORE, host_id)
            Disconnect(si)
            return False

        # VM 설정
        vmx_file = vim.vm.FileInfo(
            logDirectory=None,
            snapshotDirectory=None,
            suspendDirectory=None,
            vmPathName=f"[{DATASTORE}] {vm_name}/{vm_name}.vmx"
        )

        # 디스크 설정
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.fileOperation = "create"
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk_spec.device = vim.vm.device.VirtualDisk()
        disk_spec.device.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        disk_spec.device.backing.diskMode = "persistent"
        disk_spec.device.backing.thinProvisioned = True
        disk_spec.device.backing.fileName = f"[{DATASTORE}] {vm_name}/{vm_name}.vmdk"
        disk_spec.device.unitNumber = 0
        disk_spec.device.capacityInKB = disk_gb * 1024 * 1024
        disk_spec.device.controllerKey = 1000

        # SCSI 컨트롤러
        scsi_spec = vim.vm.device.VirtualDeviceSpec()
        scsi_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        scsi_spec.device = vim.vm.device.ParaVirtualSCSIController()
        scsi_spec.device.key = 1000
        scsi_spec.device.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing

        # 네트워크
        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        nic_spec.device = vim.vm.device.VirtualVmxnet3()
        nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        nic_spec.device.backing.deviceName = "VM Network"
        nic_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        nic_spec.device.connectable.startConnected = True
        nic_spec.device.connectable.connected = True

        # CD-ROM (ISO 마운트)
        cdrom_spec = vim.vm.device.VirtualDeviceSpec()
        cdrom_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        cdrom_spec.device = vim.vm.device.VirtualCdrom()
        cdrom_spec.device.backing = vim.vm.device.VirtualCdrom.IsoBackingInfo()
        cdrom_spec.device.backing.fileName = ISO_PATH
        cdrom_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        cdrom_spec.device.connectable.startConnected = True
        cdrom_spec.device.connectable.connected = True
        cdrom_spec.device.controllerKey = 200  # IDE controller

        # IDE 컨트롤러
        ide_spec = vim.vm.device.VirtualDeviceSpec()
        ide_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        ide_spec.device = vim.vm.device.VirtualIDEController()
        ide_spec.device.key = 200

        config = vim.vm.ConfigSpec(
            name=vm_name,
            memoryMB=mem_gb * 1024,
            numCPUs=cpu,
            files=vmx_file,
            guestId="centos7_64Guest",
            version="vmx-13",
            deviceChange=[scsi_spec, disk_spec, nic_spec, ide_spec, cdrom_spec],
        )

        task = vm_folder.CreateVM_Task(config=config, pool=resource_pool)

        # 태스크 완료 대기
        while task.info.state in ("queued", "running"):
            time.sleep(2)

        if task.info.state == "success":
            logger.info("VM 생성 완료: %s on %s", vm_name, host_id)

            # VM 전원 켜기
            vm = task.info.result
            vm.PowerOnVM_Task()
            logger.info("VM 전원 ON: %s", vm_name)
            Disconnect(si)
            return True
        else:
            logger.error("VM 생성 실패: %s, error=%s", vm_name, task.info.error)
            Disconnect(si)
            return False

    except Exception:
        logger.exception("VM 생성 오류: %s on %s", vm_name, host_id)
        return False


def phase1_deploy_vms():
    """Phase 1: ESXi 4대에 VM 배포."""
    logger.info("=" * 60)
    logger.info("Phase 1: ESXi VM 자동 배포")
    logger.info("=" * 60)

    for host in ESXI_HOSTS:
        for i in range(2):
            vm_name = f"stress-{host['id']}-{i+1:02d}"
            logger.info("VM 생성: %s → %s", vm_name, host["id"])
            success = create_vm_on_esxi(
                host_ip=host["ip"],
                host_id=host["id"],
                vm_name=vm_name,
                cpu=4,
                mem_gb=16,
                disk_gb=50,
            )
            if success:
                logger.info("  → 성공")
            else:
                logger.warning("  → 실패, 다음 VM으로 진행")
            time.sleep(5)

    logger.info("Phase 1 완료: VM 배포")


# ══════════════════════════════════════════════
#  Phase 2: node3 워크로드 생성
# ══════════════════════════════════════════════

def phase2_node3_workload(duration_hours: int = 4):
    """Phase 2: node3에서 메모리/CPU 스트레스 워크로드 실행.

    Args:
        duration_hours: 워크로드 지속 시간 (시간).
    """
    logger.info("=" * 60)
    logger.info("Phase 2: node3 워크로드 생성 (%d시간)", duration_hours)
    logger.info("=" * 60)

    # stress-ng 설치
    logger.info("stress-ng 설치...")
    subprocess.run(["dnf", "install", "-y", "stress-ng"], capture_output=True, timeout=120)

    duration_sec = duration_hours * 3600

    # CPU + 메모리 스트레스 (cuda:0/1은 건드리지 않음)
    logger.info("CPU+메모리 스트레스 시작 (CPU 16코어, MEM 32GB, %d시간)", duration_hours)
    proc = subprocess.Popen(
        ["stress-ng", "--cpu", "16", "--vm", "4", "--vm-bytes", "32G",
         "--timeout", str(duration_sec)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    logger.info("stress-ng PID: %d", proc.pid)
    logger.info("Phase 2 시작됨 (백그라운드 %d시간 실행 중)", duration_hours)

    return proc


# ══════════════════════════════════════════════
#  Phase 3: 가상 CE 시계열 패턴 생성
# ══════════════════════════════════════════════

def push_to_vm(lines: list[str]) -> bool:
    """InfluxDB line protocol로 VictoriaMetrics에 push."""
    influx_lines = []
    for line in lines:
        # Prometheus → InfluxDB 변환
        import re
        match = re.match(r'^([a-zA-Z_]\w*)\{(.+?)\}\s+(.+?)(\s+\d+)?$', line)
        if match:
            metric, labels_str, value = match.group(1), match.group(2), match.group(3)
            tags = []
            for pair in labels_str.split(","):
                k, v = pair.strip().split("=", 1)
                clean_v = v.strip().strip('"')
                tags.append(f"{k.strip()}={clean_v}")
            influx_lines.append(f"{metric},{','.join(tags)} value={value}")
        else:
            match2 = re.match(r'^([a-zA-Z_]\w*)\s+(.+?)$', line)
            if match2:
                influx_lines.append(f"{match2.group(1)} value={match2.group(2)}")

    if not influx_lines:
        return False

    try:
        resp = requests.post(f"{VM_URL}/write", data="\n".join(influx_lines) + "\n",
                            headers={"Content-Type": "text/plain"}, timeout=10)
        return resp.status_code == 204
    except Exception:
        return False


def phase3_ce_patterns(duration_hours: int = 6):
    """Phase 3: 다양한 CE 패턴을 VictoriaMetrics에 실시간 push.

    60초마다 CE 값을 push하면서 패턴을 변경한다.

    패턴 순서 (각 1.5시간):
      1. 정상 (CE 0~2/분)           → 기준선 학습
      2. 점진적 증가 (0→50/분)       → DIMM 열화 패턴
      3. 급증 (100+/분)             → 장애 직전 패턴
      4. 복구 (50→0/분)             → RECOVERY 패턴

    Args:
        duration_hours: 총 실행 시간.
    """
    logger.info("=" * 60)
    logger.info("Phase 3: 가상 CE 패턴 생성 (%d시간)", duration_hours)
    logger.info("=" * 60)

    total_minutes = duration_hours * 60
    phase_minutes = total_minutes // 4
    servers = ["vmgnode18", "vmgnode23", "vmgnode26", "vmgnode30", "18AFD199"]

    np.random.seed(int(time.time()) % 10000)

    for minute in range(total_minutes):
        phase = minute // phase_minutes

        lines = []
        for srv in servers:
            if phase == 0:
                # 정상
                ce = np.random.poisson(1)
                pattern_name = "normal"
            elif phase == 1:
                # 점진적 증가
                progress = (minute - phase_minutes) / phase_minutes
                ce = np.random.poisson(2 + int(progress * 50))
                pattern_name = "ramp"
            elif phase == 2:
                # 급증
                ce = np.random.poisson(100)
                pattern_name = "burst"
            else:
                # 복구
                progress = (minute - 3 * phase_minutes) / phase_minutes
                ce = np.random.poisson(max(1, int(50 * (1 - progress))))
                pattern_name = "recovery"

            lines.append(f'memory_errors{{server="{srv}",mc="0",csrow="0",channel="0"}} {ce}')
            lines.append(f'memory_errors_ce_total{{server="{srv}"}} {ce}')

        ok = push_to_vm(lines)
        if minute % 30 == 0:
            logger.info("  [%d/%d분] 패턴=%s, CE=%d, push=%s",
                        minute, total_minutes, pattern_name, ce, "OK" if ok else "FAIL")

        time.sleep(60)

    logger.info("Phase 3 완료")


# ══════════════════════════════════════════════
#  Phase 4: 시간대별 부하 변동
# ══════════════════════════════════════════════

def phase4_variable_workload(duration_hours: int = 12):
    """Phase 4: 시간대별로 부하를 변동시킨다.

    - 매 30분마다 부하 수준 변경
    - 저부하(25%) → 중부하(50%) → 고부하(90%) → 중부하 → 저부하 반복

    Args:
        duration_hours: 총 실행 시간.
    """
    logger.info("=" * 60)
    logger.info("Phase 4: 시간대별 부하 변동 (%d시간)", duration_hours)
    logger.info("=" * 60)

    levels = [
        ("low",    4, 2, "8G"),
        ("medium", 12, 3, "24G"),
        ("high",   24, 6, "48G"),
        ("medium", 12, 3, "24G"),
        ("low",    4, 2, "8G"),
    ]

    total_cycles = duration_hours * 2  # 30분 단위
    current_proc = None

    for cycle in range(total_cycles):
        level_idx = cycle % len(levels)
        name, cpus, vms, mem = levels[level_idx]

        # 이전 프로세스 종료
        if current_proc:
            current_proc.terminate()
            try:
                current_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                current_proc.kill()

        logger.info("  [%d/%d] 부하 수준: %s (CPU=%d, VM=%d, MEM=%s)",
                    cycle + 1, total_cycles, name, cpus, vms, mem)

        current_proc = subprocess.Popen(
            ["stress-ng", "--cpu", str(cpus), "--vm", str(vms),
             "--vm-bytes", mem, "--timeout", "1800"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        time.sleep(1800)  # 30분

    if current_proc:
        current_proc.terminate()

    logger.info("Phase 4 완료")


# ══════════════════════════════════════════════
#  전체 실행
# ══════════════════════════════════════════════

def run_all():
    """전체 시나리오를 순차 실행한다."""
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║  학습 데이터 확보 자동화 시나리오 시작          ║")
    logger.info("╚══════════════════════════════════════════════╝")

    # Phase 1: VM 배포
    phase1_deploy_vms()

    # Phase 2: node3 워크로드 (백그라운드)
    stress_proc = phase2_node3_workload(duration_hours=4)

    # Phase 3: CE 패턴 생성 (Phase 2와 병행)
    phase3_ce_patterns(duration_hours=4)

    # Phase 2 종료 대기
    if stress_proc:
        stress_proc.terminate()

    # Phase 4: 부하 변동
    phase4_variable_workload(duration_hours=6)

    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║  전체 시나리오 완료                            ║")
    logger.info("╚══════════════════════════════════════════════╝")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="학습 데이터 확보 자동화")
    parser.add_argument("--phase", type=str, default="all",
                       choices=["1", "2", "3", "4", "all"],
                       help="실행할 Phase (1=VM배포, 2=워크로드, 3=CE패턴, 4=부하변동, all=전체)")
    parser.add_argument("--hours", type=int, default=4,
                       help="Phase 지속 시간 (시간)")
    args = parser.parse_args()

    if args.phase == "1":
        phase1_deploy_vms()
    elif args.phase == "2":
        proc = phase2_node3_workload(args.hours)
        proc.wait()
    elif args.phase == "3":
        phase3_ce_patterns(args.hours)
    elif args.phase == "4":
        phase4_variable_workload(args.hours)
    else:
        run_all()
