"""Telegraf 설정 배포 스크립트.

node2 (18AFD226, 10.100.230.72)에 SSH로 telegraf.conf를 배포한다.

사용법:
    python scripts/setup_telegraf.py
"""

import logging
import sys
from pathlib import Path

import paramiko

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NODE2_IP = "10.100.230.72"
NODE2_USER = "hpcdev"
SSH_KEY_PATH = Path.home() / ".ssh" / "id_ed25519"
TELEGRAF_CONF_PATH = "/etc/telegraf/telegraf.conf"
SSH_TIMEOUT = 10

TELEGRAF_CONF = """\
[global_tags]
  datacenter = "TTA-HPC"

[agent]
  interval = "60s"
  round_interval = true
  metric_batch_size = 1000
  metric_buffer_limit = 10000
  collection_jitter = "5s"
  flush_interval = "10s"
  flush_jitter = "5s"

[[outputs.influxdb_v2]]
  urls = ["http://10.100.230.72:8428"]
  bucket = "hpc_metrics"
  token = ""
  organization = ""

[[inputs.ipmi_sensor]]
  servers = ["root:qwe123@localhost"]
  metric_version = 2
  timeout = "10s"

[[inputs.smart]]
  path = "/usr/bin/smartctl"
  interval = "600s"

[[inputs.cpu]]
  percpu = true
  totalcpu = true
  collect_cpu_time = false

[[inputs.mem]]

[[inputs.disk]]
  ignore_fs = ["tmpfs", "devtmpfs", "devfs", "iso9660", "overlay", "aufs", "squashfs"]

[[inputs.diskio]]

[[inputs.net]]

[[inputs.system]]

[[inputs.exec]]
  commands = ["/opt/failure_prediction/scripts/collect_edac.sh"]
  timeout = "10s"
  data_format = "influx"
  interval = "60s"
"""


def deploy_telegraf_conf() -> bool:
    """node2에 telegraf.conf를 배포한다.

    Returns:
        성공 여부.

    Raises:
        paramiko.SSHException: SSH 연결 실패.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        connect_kwargs = {
            "hostname": NODE2_IP,
            "username": NODE2_USER,
            "timeout": SSH_TIMEOUT,
        }
        if SSH_KEY_PATH.exists():
            connect_kwargs["key_filename"] = str(SSH_KEY_PATH)
        else:
            logger.warning("SSH 키 없음, 비밀번호 인증 시도")
            connect_kwargs["password"] = input("node2 password: ")

        client.connect(**connect_kwargs)
        logger.info("node2 (%s) SSH 연결 성공", NODE2_IP)

        # telegraf.conf 업로드
        sftp = client.open_sftp()
        tmp_path = "/tmp/telegraf.conf"
        with sftp.file(tmp_path, "w") as f:
            f.write(TELEGRAF_CONF)
        sftp.close()
        logger.info("telegraf.conf 업로드 완료: %s", tmp_path)

        # 설정 파일 이동 및 서비스 재시작
        commands = [
            f"sudo cp {tmp_path} {TELEGRAF_CONF_PATH}",
            "sudo systemctl restart telegraf",
            "sudo systemctl status telegraf --no-pager",
        ]

        for cmd in commands:
            _, stdout, stderr = client.exec_command(cmd, timeout=SSH_TIMEOUT)
            out = stdout.read().decode()
            err = stderr.read().decode()
            if out:
                logger.info("stdout: %s", out.strip())
            if err:
                logger.warning("stderr: %s", err.strip())

        logger.info("Telegraf 설정 배포 완료: %s", NODE2_IP)
        return True

    except Exception:
        logger.exception("Telegraf 배포 실패: %s", NODE2_IP)
        return False
    finally:
        client.close()


def create_edac_collect_script() -> None:
    """collect_edac.sh 스크립트를 로컬에 생성한다."""
    script_path = Path("/opt/failure_prediction/scripts/collect_edac.sh")
    script_content = """\
#!/bin/bash
# EDAC CE/UE 카운트를 InfluxDB Line Protocol로 출력
# Telegraf [[inputs.exec]]에서 호출

for mc_dir in /sys/devices/system/edac/mc/mc*; do
    [ -d "$mc_dir" ] || continue
    mc=$(basename "$mc_dir" | sed 's/mc//')

    for csrow_dir in "$mc_dir"/csrow*; do
        [ -d "$csrow_dir" ] || continue
        csrow=$(basename "$csrow_dir" | sed 's/csrow//')

        ce=$(cat "$csrow_dir/ce_count" 2>/dev/null || echo 0)
        ue=$(cat "$csrow_dir/ue_count" 2>/dev/null || echo 0)

        echo "memory_errors,server=$(hostname),mc=$mc,csrow=$csrow ce_count=${ce}i,ue_count=${ue}i"
    done
done
"""
    script_path.write_text(script_content)
    script_path.chmod(0o755)
    logger.info("collect_edac.sh 생성: %s", script_path)


if __name__ == "__main__":
    create_edac_collect_script()
    success = deploy_telegraf_conf()
    sys.exit(0 if success else 1)
