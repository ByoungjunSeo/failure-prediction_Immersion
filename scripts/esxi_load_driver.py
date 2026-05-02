"""ESXi 원격 부하 생성기.

node3에서 ESXi 4대에 SSH로 지속적인 CPU 부하를 건다.
SSH 세션을 유지한 채로 명령을 실행하므로 프로세스가 유지된다.

사용법: python scripts/esxi_load_driver.py --hours 3
"""

import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HOSTS = [
    ("vmgnode18", "10.148.148.118"),
    ("vmgnode23", "10.148.148.123"),
    ("vmgnode26", "10.148.148.126"),
    ("vmgnode30", "10.148.148.130"),
]
ESXI_PASS = "VMware!0"


def run_load_on_host(host_id: str, host_ip: str, duration_sec: int) -> None:
    """단일 ESXi 호스트에 CPU 부하를 건다.

    SSH 세션을 유지한 채로 python CPU 부하를 실행.
    세션이 유지되는 동안 프로세스가 살아있음.
    """
    # Python으로 CPU 100% 사용하는 원라이너 (8 프로세스)
    load_cmd = (
        f"python -c \""
        f"import os, time; "
        f"[os.fork() == 0 or None for _ in range(7)]; "
        f"end=time.time()+{duration_sec}; "
        f"x=0; "
        f"exec('while time.time()<end:\\n x+=1') "
        f"\""
    )

    logger.info("%s: SSH 부하 세션 시작 (%d초)", host_id, duration_sec)

    try:
        proc = subprocess.Popen(
            [
                "sshpass", "-p", ESXI_PASS,
                "ssh", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=60",
                f"root@{host_ip}",
                load_cmd,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("%s: PID %d로 실행 중", host_id, proc.pid)
        proc.wait(timeout=duration_sec + 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.info("%s: 시간 만료, 종료", host_id)
    except Exception:
        logger.exception("%s: 오류", host_id)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=3)
    args = parser.parse_args()

    duration = args.hours * 3600
    logger.info("ESXi 4대 부하 시작 (%d시간)", args.hours)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []
        for host_id, host_ip in HOSTS:
            f = executor.submit(run_load_on_host, host_id, host_ip, duration)
            futures.append(f)

        for f in futures:
            f.result()

    logger.info("전체 부하 완료")


if __name__ == "__main__":
    main()
