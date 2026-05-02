"""EDAC (Error Detection And Correction) 데이터 수집기.

edac-util, rasdaemon SQLite, dmidecode를 통해
DIMM별 CE/UE 에러 카운트 및 MCE 이벤트를 수집한다.

수집 항목:
  - edac-util: mc/csrow/channel별 CE/UE 카운트
  - rasdaemon SQLite: 신규 MCE 이벤트 (last_id 관리)
  - dmidecode --type 17: DIMM 슬롯 물리 위치 매핑
"""

import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RASDAEMON_DB_PATH = "/var/lib/rasdaemon/ras-mc_event.db"
EDAC_CMD = "edac-util"
DMIDECODE_CMD = "dmidecode"
CMD_TIMEOUT = 10


@dataclass
class DimmLocation:
    """DIMM 물리적 위치 정보."""

    mc: int
    csrow: int
    channel: int
    slot_label: str = ""
    socket_id: int = 0


@dataclass
class MemoryErrorEvent:
    """메모리 에러 이벤트."""

    timestamp: datetime
    dimm_loc: DimmLocation
    ce_count: int = 0
    ue_count: int = 0
    address: Optional[str] = None
    error_type: str = "corrected"


@dataclass
class EdacStatus:
    """EDAC 전체 상태 요약."""

    dimm_locations: list[DimmLocation] = field(default_factory=list)
    error_events: list[MemoryErrorEvent] = field(default_factory=list)
    total_ce: int = 0
    total_ue: int = 0
    collected_at: datetime = field(default_factory=datetime.now)


class EdacCollector:
    """EDAC 데이터 수집기.

    Args:
        rasdaemon_db_path: rasdaemon SQLite DB 경로.
        last_id_file: 마지막 처리 이벤트 ID 파일 경로.
    """

    def __init__(
        self,
        rasdaemon_db_path: str = RASDAEMON_DB_PATH,
        last_id_file: str = "/tmp/edac_last_id",
    ):
        self._rasdaemon_db_path = rasdaemon_db_path
        self._last_id_file = Path(last_id_file)
        self._last_id = self._load_last_id()

    def collect(self) -> EdacStatus:
        """전체 EDAC 상태를 수집한다.

        Returns:
            EdacStatus: DIMM 위치, 에러 이벤트, CE/UE 합계.

        Raises:
            RuntimeError: 수집 과정에서 모든 소스가 실패한 경우.
        """
        status = EdacStatus()

        try:
            status.dimm_locations = self._parse_dmidecode()
        except Exception:
            logger.warning("dmidecode 파싱 실패, 슬롯 매핑 없이 진행")

        try:
            ce_ue_map = self._parse_edac_util()
            status.total_ce = sum(v[0] for v in ce_ue_map.values())
            status.total_ue = sum(v[1] for v in ce_ue_map.values())
        except Exception:
            logger.warning("edac-util 파싱 실패")

        try:
            events = self._poll_rasdaemon()
            status.error_events = events
        except Exception:
            logger.warning("rasdaemon 폴링 실패")

        if not status.dimm_locations and not status.error_events and status.total_ce == 0:
            logger.error("모든 EDAC 수집 소스 실패")

        status.collected_at = datetime.now()
        return status

    def _run_cmd(self, cmd: list[str]) -> str:
        """외부 명령을 실행하고 stdout을 반환한다.

        Args:
            cmd: 실행할 명령어 리스트.

        Returns:
            명령 stdout 문자열.

        Raises:
            subprocess.SubprocessError: 명령 실행 실패.
        """
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
            logger.error("명령 타임아웃: %s", " ".join(cmd))
            raise
        except subprocess.CalledProcessError as e:
            logger.error("명령 실패: %s, stderr: %s", " ".join(cmd), e.stderr)
            raise

    def _parse_edac_util(self) -> dict[tuple[int, int, int], tuple[int, int]]:
        """edac-util -s 출력을 파싱하여 mc/csrow/channel별 CE/UE를 반환한다.

        Returns:
            {(mc, csrow, channel): (ce_count, ue_count)} 딕셔너리.
        """
        output = self._run_cmd(["sudo", EDAC_CMD, "-s"])
        result: dict[tuple[int, int, int], tuple[int, int]] = {}

        # 패턴: mc0/csrow0/ch0: 0 Uncorrected Errors, 2 Corrected Errors
        pattern = re.compile(
            r"mc(\d+)/csrow(\d+)/ch(\d+):\s+"
            r"(\d+)\s+Uncorrected\s+Errors?,\s+"
            r"(\d+)\s+Corrected\s+Errors?"
        )

        for line in output.splitlines():
            match = pattern.search(line)
            if match:
                mc, csrow, ch = int(match.group(1)), int(match.group(2)), int(match.group(3))
                ue, ce = int(match.group(4)), int(match.group(5))
                result[(mc, csrow, ch)] = (ce, ue)
                logger.debug("EDAC mc%d/csrow%d/ch%d: CE=%d, UE=%d", mc, csrow, ch, ce, ue)

        return result

    def _parse_dmidecode(self) -> list[DimmLocation]:
        """dmidecode --type 17 출력을 파싱하여 DIMM 슬롯 정보를 반환한다.

        Returns:
            DimmLocation 리스트.
        """
        output = self._run_cmd(["sudo", DMIDECODE_CMD, "--type", "17"])
        dimms: list[DimmLocation] = []

        # Handle 기준으로 블록을 나누어 파싱
        blocks = re.split(r"(?=Handle\s+0x)", output)

        for block in blocks:
            if "Memory Device" not in block:
                continue

            slot_label = ""
            socket_id = 0
            has_module = False

            for line in block.splitlines():
                line = line.strip()
                if line.startswith("Locator:") and not line.startswith("Bank Locator:"):
                    slot_label = line.split(":", 1)[1].strip()
                elif line.startswith("Bank Locator:"):
                    bank = line.split(":", 1)[1].strip()
                    bank_match = re.search(r"(\d+)", bank)
                    if bank_match:
                        socket_id = int(bank_match.group(1))
                elif line.startswith("Size:") and "No Module Installed" not in line:
                    has_module = True

            if has_module:
                dimms.append(
                    DimmLocation(
                        mc=socket_id,
                        csrow=len(dimms),
                        channel=len(dimms) % 8,
                        slot_label=slot_label,
                        socket_id=socket_id,
                    )
                )

        logger.info("dmidecode: %d개 DIMM 감지", len(dimms))
        return dimms

    def _poll_rasdaemon(self) -> list[MemoryErrorEvent]:
        """rasdaemon SQLite DB에서 신규 MCE 이벤트를 폴링한다.

        Returns:
            신규 MemoryErrorEvent 리스트.
        """
        if not Path(self._rasdaemon_db_path).exists():
            logger.warning("rasdaemon DB 없음: %s", self._rasdaemon_db_path)
            return []

        events: list[MemoryErrorEvent] = []

        try:
            conn = sqlite3.connect(f"file:{self._rasdaemon_db_path}?mode=ro", uri=True)
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT id, timestamp, err_count, err_type, mc, top_layer, middle_layer, address
                FROM mc_event
                WHERE id > ?
                ORDER BY id ASC
                """,
                (self._last_id,),
            )

            for row in cursor.fetchall():
                event_id, ts_str, err_count, err_type, mc, csrow, channel, address = row
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S %z")
                except ValueError:
                    ts = datetime.now()

                is_ue = "uncorrected" in (err_type or "").lower()
                event = MemoryErrorEvent(
                    timestamp=ts,
                    dimm_loc=DimmLocation(mc=mc or 0, csrow=csrow or 0, channel=channel or 0),
                    ce_count=0 if is_ue else (err_count or 1),
                    ue_count=err_count if is_ue else 0,
                    address=address,
                    error_type=err_type or "unknown",
                )
                events.append(event)
                self._last_id = event_id

            conn.close()
        except sqlite3.Error:
            logger.exception("rasdaemon DB 읽기 실패")
            raise

        if events:
            self._save_last_id()
            logger.info("rasdaemon: %d건 신규 이벤트 (last_id=%d)", len(events), self._last_id)

        return events

    def _load_last_id(self) -> int:
        """파일에서 마지막 처리 이벤트 ID를 로드한다.

        Returns:
            마지막 처리 이벤트 ID (없으면 0).
        """
        try:
            if self._last_id_file.exists():
                return int(self._last_id_file.read_text().strip())
        except (ValueError, OSError):
            logger.warning("last_id 파일 읽기 실패, 0부터 시작")
        return 0

    def _save_last_id(self) -> None:
        """마지막 처리 이벤트 ID를 파일에 저장한다."""
        try:
            self._last_id_file.write_text(str(self._last_id))
        except OSError:
            logger.exception("last_id 파일 저장 실패")
