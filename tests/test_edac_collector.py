"""EDAC Collector 단위 테스트.

mock 기반으로 edac-util, dmidecode, rasdaemon DB 파싱을 검증한다.
"""

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.collectors.edac_collector import (
    DimmLocation,
    EdacCollector,
    EdacStatus,
    MemoryErrorEvent,
)

# === edac-util 파싱 테스트 ===

EDAC_UTIL_OUTPUT = """\
mc0/csrow0/ch0: 0 Uncorrected Errors, 5 Corrected Errors
mc0/csrow0/ch1: 0 Uncorrected Errors, 3 Corrected Errors
mc0/csrow1/ch0: 1 Uncorrected Error, 0 Corrected Errors
mc1/csrow0/ch0: 0 Uncorrected Errors, 12 Corrected Errors
"""


class TestParseEdacUtil:
    """edac-util 파싱 테스트."""

    @patch.object(EdacCollector, "_run_cmd", return_value=EDAC_UTIL_OUTPUT)
    def test_parse_ce_ue_counts(self, mock_cmd: MagicMock) -> None:
        """CE/UE 카운트가 올바르게 파싱되는지 확인."""
        collector = EdacCollector()
        result = collector._parse_edac_util()

        assert result[(0, 0, 0)] == (5, 0)   # mc0/csrow0/ch0: CE=5, UE=0
        assert result[(0, 0, 1)] == (3, 0)   # mc0/csrow0/ch1: CE=3, UE=0
        assert result[(0, 1, 0)] == (0, 1)   # mc0/csrow1/ch0: CE=0, UE=1
        assert result[(1, 0, 0)] == (12, 0)  # mc1/csrow0/ch0: CE=12, UE=0
        assert len(result) == 4

    @patch.object(EdacCollector, "_run_cmd", return_value="")
    def test_empty_output(self, mock_cmd: MagicMock) -> None:
        """빈 출력 시 빈 딕셔너리 반환."""
        collector = EdacCollector()
        result = collector._parse_edac_util()
        assert result == {}


# === dmidecode 파싱 테스트 ===

DMIDECODE_OUTPUT = """\
# dmidecode 3.3
Getting SMBIOS data from sysfs.
SMBIOS 3.1.1 present.

Handle 0x0036, DMI type 17, 84 bytes
Memory Device
\tTotal Width: 72 bits
\tData Width: 64 bits
\tSize: 64 GB
\tForm Factor: DIMM
\tLocator: DIMM_A0
\tBank Locator: BANK 0
\tType: DDR4

Handle 0x0037, DMI type 17, 84 bytes
Memory Device
\tTotal Width: 72 bits
\tData Width: 64 bits
\tSize: 64 GB
\tForm Factor: DIMM
\tLocator: DIMM_B0
\tBank Locator: BANK 0
\tType: DDR4

Handle 0x0038, DMI type 17, 84 bytes
Memory Device
\tTotal Width: Unknown
\tData Width: Unknown
\tSize: No Module Installed
\tForm Factor: DIMM
\tLocator: DIMM_C0
\tBank Locator: BANK 1
"""


class TestParseDmidecode:
    """dmidecode 파싱 테스트."""

    @patch.object(EdacCollector, "_run_cmd", return_value=DMIDECODE_OUTPUT)
    def test_parse_installed_dimms(self, mock_cmd: MagicMock) -> None:
        """설치된 DIMM만 파싱되는지 확인 (빈 슬롯 제외)."""
        collector = EdacCollector()
        dimms = collector._parse_dmidecode()

        assert len(dimms) == 2
        assert dimms[0].slot_label == "DIMM_A0"
        assert dimms[1].slot_label == "DIMM_B0"

    @patch.object(EdacCollector, "_run_cmd", return_value="")
    def test_empty_output(self, mock_cmd: MagicMock) -> None:
        """빈 출력 시 빈 리스트 반환."""
        collector = EdacCollector()
        dimms = collector._parse_dmidecode()
        assert dimms == []


# === rasdaemon SQLite 폴링 테스트 ===

class TestPollRasdaemon:
    """rasdaemon DB 폴링 테스트."""

    def _create_test_db(self, db_path: str) -> None:
        """테스트용 rasdaemon DB를 생성한다."""
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE mc_event (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                err_count INTEGER,
                err_type TEXT,
                mc INTEGER,
                top_layer INTEGER,
                middle_layer INTEGER,
                address TEXT
            )
        """)
        conn.execute("""
            INSERT INTO mc_event VALUES
            (1, '2025-04-01 12:00:00 +0900', 1, 'Corrected', 0, 0, 0, '0x1234')
        """)
        conn.execute("""
            INSERT INTO mc_event VALUES
            (2, '2025-04-01 12:01:00 +0900', 1, 'Uncorrected', 0, 1, 0, '0x5678')
        """)
        conn.execute("""
            INSERT INTO mc_event VALUES
            (3, '2025-04-01 12:02:00 +0900', 3, 'Corrected', 1, 0, 1, '0x9abc')
        """)
        conn.commit()
        conn.close()

    def test_poll_new_events(self, tmp_path: Path) -> None:
        """신규 이벤트를 올바르게 폴링하는지 확인."""
        db_path = str(tmp_path / "ras-mc_event.db")
        last_id_file = str(tmp_path / "last_id")
        self._create_test_db(db_path)

        collector = EdacCollector(rasdaemon_db_path=db_path, last_id_file=last_id_file)
        events = collector._poll_rasdaemon()

        assert len(events) == 3
        assert events[0].ce_count == 1
        assert events[0].ue_count == 0
        assert events[1].ue_count == 1  # Uncorrected
        assert events[1].ce_count == 0
        assert events[2].ce_count == 3

    def test_poll_incremental(self, tmp_path: Path) -> None:
        """last_id 이후 이벤트만 폴링하는지 확인."""
        db_path = str(tmp_path / "ras-mc_event.db")
        last_id_file = str(tmp_path / "last_id")
        self._create_test_db(db_path)

        # last_id=2로 저장
        Path(last_id_file).write_text("2")

        collector = EdacCollector(rasdaemon_db_path=db_path, last_id_file=last_id_file)
        events = collector._poll_rasdaemon()

        assert len(events) == 1
        assert events[0].dimm_loc.mc == 1

    def test_missing_db(self, tmp_path: Path) -> None:
        """DB 파일이 없을 때 빈 리스트 반환."""
        collector = EdacCollector(
            rasdaemon_db_path=str(tmp_path / "nonexistent.db"),
            last_id_file=str(tmp_path / "last_id"),
        )
        events = collector._poll_rasdaemon()
        assert events == []


# === 전체 collect() 통합 테스트 ===

class TestCollect:
    """collect() 통합 테스트 (mock 기반)."""

    @patch.object(EdacCollector, "_poll_rasdaemon", return_value=[])
    @patch.object(EdacCollector, "_parse_edac_util", return_value={(0, 0, 0): (5, 0)})
    @patch.object(EdacCollector, "_parse_dmidecode", return_value=[
        DimmLocation(mc=0, csrow=0, channel=0, slot_label="DIMM_A0")
    ])
    def test_collect_success(self, mock_dmi: MagicMock, mock_edac: MagicMock, mock_ras: MagicMock) -> None:
        """전체 수집이 정상 동작하는지 확인."""
        collector = EdacCollector()
        status = collector.collect()

        assert isinstance(status, EdacStatus)
        assert status.total_ce == 5
        assert status.total_ue == 0
        assert len(status.dimm_locations) == 1
        assert status.dimm_locations[0].slot_label == "DIMM_A0"

    @patch.object(EdacCollector, "_poll_rasdaemon", side_effect=Exception("DB error"))
    @patch.object(EdacCollector, "_parse_edac_util", side_effect=Exception("cmd error"))
    @patch.object(EdacCollector, "_parse_dmidecode", side_effect=Exception("cmd error"))
    def test_collect_all_failures(self, mock_dmi: MagicMock, mock_edac: MagicMock, mock_ras: MagicMock) -> None:
        """모든 소스 실패 시에도 EdacStatus 반환."""
        collector = EdacCollector()
        status = collector.collect()

        assert isinstance(status, EdacStatus)
        assert status.total_ce == 0
        assert status.total_ue == 0
