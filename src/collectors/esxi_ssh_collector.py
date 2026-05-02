"""ESXi SSH 읽기 전용 데이터 수집기.

paramiko SSH로 ESXi 호스트에 접속하여 pyVmomi로 수집할 수 없는
추가 정보(vmkernel 로그, DIMM 물리 정보, 메모리 상세 통계)를 수집한다.

주의:
  - 읽기 전용 명령만 허용 (install, rm 등 위험 명령 차단)
  - ESXi에 패키지 설치·설정 변경 절대 금지
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import paramiko

logger = logging.getLogger(__name__)

SSH_TIMEOUT = 10

# 실행이 절대 금지되는 명령 키워드
BLOCKED_COMMANDS = frozenset({
    "install", "remove", "uninstall", "rm ", "rm\t", "rmdir",
    "esxcli software", "vib ", "reboot", "shutdown", "poweroff",
    "halt", "init ", "kill", "mkfs", "fdisk", "dd ", "format",
    "chmod", "chown", "mv ", "cp ", "wget", "curl",
    "firewall", "iptables", "esxcfg-", "vicfg-",
})

# 허용된 읽기 전용 명령 패턴
ALLOWED_PREFIXES = (
    "esxcli hardware",
    "esxcli system stats",
    "esxcli system syslog config get",
    "vim-cmd hostsvc/hostsummary",
    "cat /var/log/",
    "grep ",
    "tail ",
    "head ",
    "echo ",
)


@dataclass
class VmkernelMemoryError:
    """vmkernel 메모리 에러 로그 엔트리."""

    timestamp: str
    message: str
    severity: str = "warning"


@dataclass
class EsxiMemoryInfo:
    """ESXi 메모리 상세 통계."""

    physical_memory_bytes: int = 0
    free_memory_bytes: int = 0
    kernel_memory_bytes: int = 0


@dataclass
class EsxiSshData:
    """ESXi SSH 수집 전체 결과."""

    host_id: str
    host_ip: str
    vmkernel_errors: list[VmkernelMemoryError] = field(default_factory=list)
    memory_info: Optional[EsxiMemoryInfo] = None
    dimm_fru_info: str = ""
    host_summary: str = ""
    collected_at: datetime = field(default_factory=datetime.now)
    error_count: int = 0


class EsxiSshCollector:
    """ESXi SSH 읽기 전용 수집기.

    Args:
        host_id: 호스트 식별자 (예: vmgnode18).
        host_ip: ESXi 호스트 IP.
        username: SSH 사용자명.
        password: SSH 비밀번호.
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
        self._client: Optional[paramiko.SSHClient] = None

    def collect(self) -> EsxiSshData:
        """ESXi SSH로 전체 데이터를 수집한다.

        Returns:
            EsxiSshData 객체.
        """
        data = EsxiSshData(host_id=self._host_id, host_ip=self._host_ip)

        try:
            self._connect()

            data.vmkernel_errors = self._collect_vmkernel_errors()
            data.error_count = len(data.vmkernel_errors)
            data.memory_info = self._collect_memory_info()
            data.dimm_fru_info = self._collect_dimm_fru()
            data.host_summary = self._collect_host_summary()

        except Exception:
            logger.exception("ESXi SSH 수집 실패: %s (%s)", self._host_id, self._host_ip)
        finally:
            self._disconnect()

        data.collected_at = datetime.now()
        logger.info(
            "ESXi SSH %s: vmkernel_errors=%d",
            self._host_id,
            data.error_count,
        )
        return data

    def _connect(self) -> None:
        """SSH 연결을 수립한다.

        Raises:
            paramiko.SSHException: 연결 실패.
        """
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=self._host_ip,
            username=self._username,
            password=self._password,
            timeout=SSH_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        logger.debug("ESXi SSH 연결 성공: %s", self._host_ip)

    def _disconnect(self) -> None:
        """SSH 연결을 종료한다."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _exec_command(self, command: str) -> str:
        """SSH 명령을 실행한다 (읽기 전용만 허용).

        Args:
            command: 실행할 명령어.

        Returns:
            stdout 문자열.

        Raises:
            PermissionError: 차단된 명령어 사용 시.
            RuntimeError: SSH 클라이언트 미연결.
        """
        if not self._client:
            raise RuntimeError("SSH 미연결")

        self._validate_command(command)

        _, stdout, stderr = self._client.exec_command(command, timeout=SSH_TIMEOUT)
        output = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")

        if err:
            logger.debug("ESXi SSH stderr (%s): %s", self._host_id, err[:200])

        return output

    @staticmethod
    def _validate_command(command: str) -> None:
        """명령어가 읽기 전용인지 검증한다.

        Args:
            command: 검증할 명령어.

        Raises:
            PermissionError: 차단된 명령어 사용 시.
        """
        cmd_lower = command.lower().strip()

        for blocked in BLOCKED_COMMANDS:
            if blocked in cmd_lower:
                raise PermissionError(
                    f"차단된 명령어: '{command}' (금지 키워드: '{blocked}')"
                )

        if not any(cmd_lower.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            raise PermissionError(
                f"허용되지 않은 명령어: '{command}'. 허용 목록: {ALLOWED_PREFIXES}"
            )

    def _collect_vmkernel_errors(self) -> list[VmkernelMemoryError]:
        """vmkernel 로그에서 메모리 관련 에러를 수집한다.

        Returns:
            VmkernelMemoryError 리스트.
        """
        cmd = (
            'grep -i "memory\\|DRAM\\|ECC\\|correctable\\|uncorrectable\\|MCE" '
            "/var/log/vmkernel.log | tail -500"
        )
        try:
            output = self._exec_command(cmd)
        except Exception:
            logger.warning("vmkernel 로그 수집 실패: %s", self._host_id)
            return []

        errors: list[VmkernelMemoryError] = []
        for line in output.splitlines():
            if not line.strip():
                continue

            severity = "critical" if "uncorrectable" in line.lower() else "warning"
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
            timestamp = ts_match.group(1) if ts_match else ""

            errors.append(
                VmkernelMemoryError(
                    timestamp=timestamp,
                    message=line.strip(),
                    severity=severity,
                )
            )

        return errors

    def _collect_memory_info(self) -> Optional[EsxiMemoryInfo]:
        """ESXi 메모리 상세 통계를 수집한다.

        Returns:
            EsxiMemoryInfo 객체, 실패 시 None.
        """
        try:
            output = self._exec_command("esxcli hardware memory get")
        except Exception:
            logger.warning("메모리 정보 수집 실패: %s", self._host_id)
            return None

        info = EsxiMemoryInfo()
        for line in output.splitlines():
            if "Physical Memory" in line:
                match = re.search(r"(\d+)", line)
                if match:
                    info.physical_memory_bytes = int(match.group(1))

        try:
            stats_output = self._exec_command("esxcli system stats memory get")
            for line in stats_output.splitlines():
                line_lower = line.lower()
                match = re.search(r"(\d+)", line)
                if not match:
                    continue
                val = int(match.group(1))
                if "free" in line_lower:
                    info.free_memory_bytes = val
                elif "kernel" in line_lower:
                    info.kernel_memory_bytes = val
        except Exception:
            logger.debug("메모리 통계 수집 실패: %s", self._host_id)

        return info

    def _collect_dimm_fru(self) -> str:
        """DIMM FRU 물리 정보를 수집한다.

        Returns:
            FRU 정보 문자열.
        """
        try:
            return self._exec_command("esxcli hardware ipmi fru list")
        except Exception:
            logger.warning("DIMM FRU 수집 실패: %s", self._host_id)
            return ""

    def _collect_host_summary(self) -> str:
        """호스트 요약 정보를 수집한다.

        Returns:
            호스트 요약 문자열.
        """
        try:
            return self._exec_command("vim-cmd hostsvc/hostsummary")
        except Exception:
            logger.warning("호스트 요약 수집 실패: %s", self._host_id)
            return ""
