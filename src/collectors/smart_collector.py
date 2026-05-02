"""SMART 디스크 헬스 데이터 수집기.

smartctl을 통해 SSD/HDD의 SMART 속성을 수집한다.

수집 항목:
  - Reallocated_Sector_Ct (5)
  - Reported_Uncorrect (187)
  - Temperature_Celsius (194)
  - Current_Pending_Sector (197)
  - Offline_Uncorrectable (198)
  - Power_On_Hours (9)
  - Media_Wearout_Indicator (233) — SSD
  - Wear_Leveling_Count (177) — SSD
"""

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

CMD_TIMEOUT = 10

CRITICAL_ATTRS = {
    5: "Reallocated_Sector_Ct",
    9: "Power_On_Hours",
    177: "Wear_Leveling_Count",
    187: "Reported_Uncorrect",
    194: "Temperature_Celsius",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
    233: "Media_Wearout_Indicator",
}


@dataclass
class SmartAttribute:
    """SMART 개별 속성."""

    attr_id: int
    name: str
    value: int
    worst: int
    threshold: int
    raw_value: int


@dataclass
class DiskSmartData:
    """디스크 SMART 전체 데이터."""

    device: str
    model: str = ""
    serial: str = ""
    health_passed: bool = True
    attributes: list[SmartAttribute] = field(default_factory=list)
    temperature: Optional[float] = None
    power_on_hours: Optional[int] = None
    collected_at: datetime = field(default_factory=datetime.now)


class SmartCollector:
    """SMART 디스크 헬스 수집기.

    Args:
        devices: 모니터링할 디바이스 경로 리스트.
    """

    def __init__(self, devices: Optional[list[str]] = None):
        self._devices = devices or self._detect_devices()

    def collect(self) -> list[DiskSmartData]:
        """전체 디스크 SMART 데이터를 수집한다.

        Returns:
            DiskSmartData 리스트.
        """
        results: list[DiskSmartData] = []
        for device in self._devices:
            try:
                data = self._collect_device(device)
                results.append(data)
            except Exception:
                logger.warning("SMART 수집 실패: %s", device)
        return results

    def _collect_device(self, device: str) -> DiskSmartData:
        """단일 디스크의 SMART 데이터를 수집한다.

        Args:
            device: 디바이스 경로 (예: /dev/sda).

        Returns:
            DiskSmartData 객체.

        Raises:
            subprocess.SubprocessError: smartctl 실행 실패.
        """
        output = self._run_smartctl(device)
        data = DiskSmartData(device=device)

        for line in output.splitlines():
            if "Model Family:" in line or "Device Model:" in line:
                data.model = line.split(":", 1)[1].strip()
            elif "Serial Number:" in line:
                data.serial = line.split(":", 1)[1].strip()
            elif "SMART overall-health" in line:
                data.health_passed = "PASSED" in line

        data.attributes = self._parse_attributes(output)

        for attr in data.attributes:
            if attr.attr_id == 194:
                data.temperature = float(attr.raw_value)
            elif attr.attr_id == 9:
                data.power_on_hours = attr.raw_value

        data.collected_at = datetime.now()
        logger.info("SMART %s: model=%s, health=%s", device, data.model, data.health_passed)
        return data

    def _parse_attributes(self, output: str) -> list[SmartAttribute]:
        """smartctl 출력에서 SMART 속성을 파싱한다.

        Args:
            output: smartctl stdout 문자열.

        Returns:
            SmartAttribute 리스트 (주요 속성만).
        """
        attrs: list[SmartAttribute] = []
        # 패턴: ID# ATTRIBUTE_NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
        pattern = re.compile(
            r"^\s*(\d+)\s+(\S+)\s+\S+\s+(\d+)\s+(\d+)\s+(\d+)\s+\S+\s+\S+\s+\S+\s+(\d+)"
        )

        for line in output.splitlines():
            match = pattern.match(line)
            if not match:
                continue

            attr_id = int(match.group(1))
            if attr_id not in CRITICAL_ATTRS:
                continue

            attrs.append(
                SmartAttribute(
                    attr_id=attr_id,
                    name=match.group(2),
                    value=int(match.group(3)),
                    worst=int(match.group(4)),
                    threshold=int(match.group(5)),
                    raw_value=int(match.group(6)),
                )
            )

        return attrs

    def _run_smartctl(self, device: str) -> str:
        """smartctl 명령을 실행한다.

        Args:
            device: 디바이스 경로.

        Returns:
            stdout 문자열.

        Raises:
            subprocess.SubprocessError: 명령 실행 실패.
        """
        cmd = ["sudo", "smartctl", "-a", device]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT,
            )
            # smartctl은 비트마스크 종료 코드 사용 — stderr만 체크
            if result.returncode & 0b111 and result.stderr:
                logger.warning("smartctl 경고 %s: %s", device, result.stderr.strip())
            return result.stdout
        except subprocess.TimeoutExpired:
            logger.error("smartctl 타임아웃: %s", device)
            raise

    def _detect_devices(self) -> list[str]:
        """시스템에서 SMART 지원 디바이스를 탐지한다.

        Returns:
            디바이스 경로 리스트.
        """
        try:
            result = subprocess.run(
                ["sudo", "smartctl", "--scan"],
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT,
                check=True,
            )
            devices = []
            for line in result.stdout.splitlines():
                if line.strip():
                    devices.append(line.split()[0])
            logger.info("SMART 디바이스 탐지: %s", devices)
            return devices
        except Exception:
            logger.warning("SMART 디바이스 자동 탐지 실패, /dev/sda 사용")
            return ["/dev/sda"]
