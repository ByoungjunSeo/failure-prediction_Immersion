"""IPMI 센서 데이터 수집기.

ipmitool을 통해 BMC에서 온도, 전압, 팬, 전력 데이터를 수집한다.

수집 항목:
  - CPU/DIMM 온도
  - 전압 (12V, 5V, 3.3V, Vcore)
  - 팬 RPM
  - 전력 소비량 (W)
"""

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

CMD_TIMEOUT = 10


@dataclass
class IpmiSensorReading:
    """IPMI 센서 개별 읽기 값."""

    name: str
    value: float
    unit: str
    status: str = "ok"
    lower_critical: Optional[float] = None
    upper_critical: Optional[float] = None


@dataclass
class IpmiData:
    """IPMI 전체 수집 결과."""

    temperatures: list[IpmiSensorReading] = field(default_factory=list)
    voltages: list[IpmiSensorReading] = field(default_factory=list)
    fans: list[IpmiSensorReading] = field(default_factory=list)
    power: list[IpmiSensorReading] = field(default_factory=list)
    collected_at: datetime = field(default_factory=datetime.now)


class IpmiCollector:
    """IPMI 센서 데이터 수집기.

    Args:
        bmc_host: BMC IP 주소.
        bmc_user: BMC 사용자명.
        bmc_password: BMC 비밀번호.
    """

    def __init__(
        self,
        bmc_host: str = "10.100.231.71",
        bmc_user: str = "root",
        bmc_password: str = "qwe123",
    ):
        self._bmc_host = bmc_host
        self._bmc_user = bmc_user
        self._bmc_password = bmc_password

    def collect(self) -> IpmiData:
        """전체 IPMI 센서 데이터를 수집한다.

        Returns:
            IpmiData: 온도, 전압, 팬, 전력 센서 데이터.

        Raises:
            RuntimeError: ipmitool 실행 실패 시.
        """
        data = IpmiData()

        try:
            readings = self._parse_sensor_output()
        except Exception:
            logger.exception("IPMI 센서 수집 실패: %s", self._bmc_host)
            raise RuntimeError(f"IPMI 수집 실패: {self._bmc_host}")

        for reading in readings:
            unit_lower = reading.unit.lower()
            if "degrees" in unit_lower or "celsius" in unit_lower:
                data.temperatures.append(reading)
            elif "volts" in unit_lower:
                data.voltages.append(reading)
            elif "rpm" in unit_lower:
                data.fans.append(reading)
            elif "watts" in unit_lower:
                data.power.append(reading)

        data.collected_at = datetime.now()
        logger.info(
            "IPMI %s: temp=%d, volt=%d, fan=%d, power=%d",
            self._bmc_host,
            len(data.temperatures),
            len(data.voltages),
            len(data.fans),
            len(data.power),
        )
        return data

    def collect_power_reading(self) -> Optional[float]:
        """BMC에서 총 전력 소비량(W)을 수집한다.

        Returns:
            전력 소비량 (W), 실패 시 None.
        """
        try:
            output = self._run_ipmitool(["dcmi", "power", "reading"])
            match = re.search(r"(\d+)\s+Watts", output)
            if match:
                return float(match.group(1))
        except Exception:
            logger.warning("IPMI 전력 읽기 실패: %s", self._bmc_host)
        return None

    def _run_ipmitool(self, extra_args: list[str]) -> str:
        """ipmitool 명령을 실행한다.

        Args:
            extra_args: ipmitool 추가 인자.

        Returns:
            stdout 문자열.

        Raises:
            subprocess.SubprocessError: 명령 실행 실패.
        """
        cmd = [
            "ipmitool",
            "-I", "lanplus",
            "-H", self._bmc_host,
            "-U", self._bmc_user,
            "-P", self._bmc_password,
        ] + extra_args

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT,
                check=True,
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            logger.error("ipmitool 타임아웃: %s", self._bmc_host)
            raise
        except subprocess.CalledProcessError as e:
            logger.error("ipmitool 실패: %s, stderr: %s", self._bmc_host, e.stderr)
            raise

    def _parse_sensor_output(self) -> list[IpmiSensorReading]:
        """ipmitool sensor list 출력을 파싱한다.

        Returns:
            IpmiSensorReading 리스트.
        """
        output = self._run_ipmitool(["sensor", "list"])
        readings: list[IpmiSensorReading] = []

        for line in output.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue

            name = parts[0]
            value_str = parts[1]
            unit = parts[2]
            status = parts[3]

            if value_str in ("na", "0x0", ""):
                continue

            try:
                value = float(value_str)
            except ValueError:
                continue

            lower_crit = None
            upper_crit = None
            if len(parts) >= 9:
                try:
                    lower_crit = float(parts[7]) if parts[7] != "na" else None
                except ValueError:
                    pass
                try:
                    upper_crit = float(parts[8]) if parts[8] != "na" else None
                except ValueError:
                    pass

            readings.append(
                IpmiSensorReading(
                    name=name,
                    value=value,
                    unit=unit,
                    status=status,
                    lower_critical=lower_crit,
                    upper_critical=upper_crit,
                )
            )

        return readings
