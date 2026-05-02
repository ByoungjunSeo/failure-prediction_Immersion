"""ESXi pyVmomi 데이터 수집기.

pyVmomi API로 ESXi 호스트에 직접 연결하여
호스트 메트릭 및 VM 집계 데이터를 수집한다.

vCenter 미사용 — 각 ESXi 호스트에 개별 연결.

수집 메트릭:
  호스트: cpu.usage, mem.usage, mem.consumed, mem.swapinRate,
          net.errorsRx, net.errorsTx, power.power, sys.uptime
  VM 집계: vm_count, mem_balloon_sum, cpu_ready_sum, mem_swapped_sum
"""

import logging
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10


@dataclass
class EsxiHostMetrics:
    """ESXi 호스트 메트릭."""

    cpu_usage_percent: float = 0.0
    mem_usage_percent: float = 0.0
    mem_consumed_mb: float = 0.0
    mem_swapin_rate: float = 0.0
    net_errors_rx: int = 0
    net_errors_tx: int = 0
    power_watts: float = 0.0
    uptime_seconds: int = 0


@dataclass
class EsxiVmAggregate:
    """ESXi VM 집계 데이터."""

    vm_count: int = 0
    mem_balloon_sum_mb: float = 0.0
    cpu_ready_sum_ms: float = 0.0
    mem_swapped_sum_mb: float = 0.0


@dataclass
class EsxiData:
    """ESXi 전체 수집 결과."""

    host_id: str
    host_ip: str
    esxi_version: str = ""
    host_metrics: EsxiHostMetrics = field(default_factory=EsxiHostMetrics)
    vm_aggregate: EsxiVmAggregate = field(default_factory=EsxiVmAggregate)
    maintenance_mode: bool = False
    collected_at: datetime = field(default_factory=datetime.now)


class EsxiCollector:
    """ESXi pyVmomi 수집기.

    Args:
        host_id: 호스트 식별자 (예: vmgnode18).
        host_ip: ESXi 호스트 IP.
        username: ESXi 사용자명.
        password: ESXi 비밀번호.
    """

    def __init__(
        self,
        host_id: str,
        host_ip: str,
        username: str = "root",
        password: str = "",
    ):
        self._host_id = host_id
        self._host_ip = host_ip
        self._username = username
        self._password = password
        self._si: Optional[vim.ServiceInstance] = None

    def collect(self) -> EsxiData:
        """ESXi 호스트 메트릭 및 VM 집계를 수집한다.

        Returns:
            EsxiData 객체.
        """
        data = EsxiData(host_id=self._host_id, host_ip=self._host_ip)

        try:
            self._connect()
            content = self._si.RetrieveContent()
            data.esxi_version = content.about.fullName

            host = self._get_host(content)
            if host:
                data.host_metrics = self._collect_host_metrics(host)
                data.vm_aggregate = self._collect_vm_aggregate(host)
                data.maintenance_mode = host.runtime.inMaintenanceMode

        except Exception:
            logger.exception("ESXi pyVmomi 수집 실패: %s (%s)", self._host_id, self._host_ip)
        finally:
            self._disconnect()

        data.collected_at = datetime.now()
        logger.info(
            "ESXi %s: cpu=%.1f%%, mem=%.1f%%, vms=%d, maintenance=%s",
            self._host_id,
            data.host_metrics.cpu_usage_percent,
            data.host_metrics.mem_usage_percent,
            data.vm_aggregate.vm_count,
            data.maintenance_mode,
        )
        return data

    def _connect(self) -> None:
        """pyVmomi로 ESXi에 연결한다.

        Raises:
            vim.fault.InvalidLogin: 인증 실패.
            ConnectionError: 연결 실패.
        """
        context = ssl._create_unverified_context()
        try:
            self._si = SmartConnect(
                host=self._host_ip,
                user=self._username,
                pwd=self._password,
                sslContext=context,
            )
            logger.debug("ESXi pyVmomi 연결 성공: %s", self._host_ip)
        except Exception:
            logger.error("ESXi pyVmomi 연결 실패: %s", self._host_ip)
            raise

    def _disconnect(self) -> None:
        """pyVmomi 연결을 종료한다."""
        if self._si:
            try:
                Disconnect(self._si)
            except Exception:
                pass
            self._si = None

    def _get_host(self, content: vim.ServiceContent) -> Optional[vim.HostSystem]:
        """ESXi 호스트 객체를 가져온다.

        Args:
            content: vSphere ServiceContent.

        Returns:
            HostSystem 객체, 없으면 None.
        """
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        hosts = container.view
        container.Destroy()

        if hosts:
            return hosts[0]
        logger.warning("ESXi 호스트를 찾을 수 없음: %s", self._host_id)
        return None

    def _collect_host_metrics(self, host: vim.HostSystem) -> EsxiHostMetrics:
        """호스트 성능 메트릭을 수집한다.

        Args:
            host: HostSystem 객체.

        Returns:
            EsxiHostMetrics 객체.
        """
        metrics = EsxiHostMetrics()
        summary = host.summary
        hw = summary.hardware
        stats = summary.quickStats

        if hw.numCpuCores and stats.overallCpuUsage:
            total_mhz = hw.numCpuCores * hw.cpuMhz
            metrics.cpu_usage_percent = (stats.overallCpuUsage / total_mhz) * 100.0

        if hw.memorySize and stats.overallMemoryUsage:
            total_mb = hw.memorySize / (1024 * 1024)
            metrics.mem_usage_percent = (stats.overallMemoryUsage / total_mb) * 100.0
            metrics.mem_consumed_mb = float(stats.overallMemoryUsage)

        metrics.uptime_seconds = getattr(stats, "uptime", 0) or 0

        runtime = host.runtime
        if runtime and hasattr(runtime, "powerState"):
            logger.debug("ESXi %s power state: %s", self._host_id, runtime.powerState)

        return metrics

    def _collect_vm_aggregate(self, host: vim.HostSystem) -> EsxiVmAggregate:
        """VM 집계 데이터를 수집한다.

        Args:
            host: HostSystem 객체.

        Returns:
            EsxiVmAggregate 객체.
        """
        agg = EsxiVmAggregate()
        vms = host.vm

        if not vms:
            return agg

        agg.vm_count = len(vms)

        for vm_obj in vms:
            try:
                stats = vm_obj.summary.quickStats
                if stats.balloonedMemory:
                    agg.mem_balloon_sum_mb += stats.balloonedMemory
                if stats.overallCpuReadiness:
                    agg.cpu_ready_sum_ms += stats.overallCpuReadiness
                if stats.swappedMemory:
                    agg.mem_swapped_sum_mb += stats.swappedMemory
            except Exception:
                logger.debug("VM 메트릭 수집 실패: %s", vm_obj.name if hasattr(vm_obj, 'name') else 'unknown')

        return agg
