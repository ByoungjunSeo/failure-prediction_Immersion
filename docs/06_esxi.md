# 06. ESXi 연동

## 접근 방식

```
vCenter 미사용 이유: 네트워크 FQDN 제약
ESXi 접근 방법: SSH 읽기 + pyVmomi API 직접 연결

✅ SSH 허용: 읽기/모니터링 목적 (vmkernel 로그, DIMM 정보 등)
🚫 SSH 금지: 패키지 설치, 설정 변경, 파일 수정
```

---

## ESXi SSH 수집 (신규 추가)

```python
import paramiko
from typing import Optional

class ESXiSSHCollector:
    """ESXi SSH 읽기 전용 수집기"""

    def __init__(self, host_ip: str, username: str = "root",
                 password: str = "VMware!0", timeout: int = 10):
        self.host_ip  = host_ip
        self.username = username
        self.password = password
        self.timeout  = timeout

    def _exec(self, command: str) -> Optional[str]:
        """SSH 명령 실행 (읽기 전용만 허용)"""
        # 위험 명령어 차단
        forbidden = ["install", "rm ", "mv ", "chmod", "chown",
                     "esxcli software", "vim-cmd vmsvc/power",
                     ">", ">>", "|tee", "sed -i"]
        for kw in forbidden:
            if kw in command:
                raise ValueError(f"금지된 명령어 포함: {kw}")

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(self.host_ip, username=self.username,
                        password=self.password, timeout=self.timeout)
            _, stdout, stderr = ssh.exec_command(command, timeout=self.timeout)
            result = stdout.read().decode('utf-8', errors='ignore')
            ssh.close()
            return result
        except Exception as e:
            logging.error(f"ESXi SSH 오류 {self.host_ip}: {e}")
            return None

    def get_vmkernel_memory_errors(self, lines: int = 500) -> list:
        """vmkernel 로그에서 메모리 에러 추출"""
        cmd = (f"grep -i 'memory\\|DRAM\\|ECC\\|correctable\\|uncorrectable' "
               f"/var/log/vmkernel.log | tail -{lines}")
        output = self._exec(cmd)
        if not output:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def get_dimm_info(self) -> str:
        """DIMM 물리 정보 (FRU)"""
        return self._exec("esxcli hardware ipmi fru list") or ""

    def get_memory_stats(self) -> dict:
        """메모리 상세 통계"""
        output = self._exec("esxcli system stats memory get") or ""
        # 파싱 후 dict 반환
        stats = {}
        for line in output.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                stats[k.strip()] = v.strip()
        return stats

    def get_host_summary(self) -> str:
        """호스트 요약 정보"""
        return self._exec("vim-cmd hostsvc/hostsummary") or ""
```

---

## ESXi pyVmomi 연결

```python
import ssl
from pyVmomi import vim
from pyVim.connect import SmartConnect, Disconnect

def connect_esxi(host_ip: str, username: str = "root",
                 password: str = "VMware!0"):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode    = ssl.CERT_NONE
    return SmartConnect(host=host_ip, user=username,
                        pwd=password, sslContext=context)
```

---

## 리스크 레벨별 자동 대응

### WARNING (0.65 ~ 0.85)

```python
async def warning_response(host_ip: str, result):
    si   = connect_esxi(host_ip)
    host = get_host_object(si)

    # Admission Control: 신규 VM 배치 차단
    # (설정 변경 아닌 API 호출)
    spec = vim.host.ConfigSpec()
    host.ReconfigureHost_Task(spec)

    await send_slack_alert("🟡 WARNING", result)
    await log_action("WARNING", host_ip, result)
    Disconnect(si)
```

### CRITICAL (0.85 이상)

```python
async def critical_response(host_ip: str, result):
    si   = connect_esxi(host_ip)
    host = get_host_object(si)

    task = host.EnterMaintenanceMode(
        timeout=3600,
        evacuatePoweredOffVms=True
    )
    await wait_for_task(task)

    await send_slack_alert("🔴 CRITICAL — 수동 vMotion 필요", result)
    await create_maintenance_ticket(host_ip, result)
    await log_action("CRITICAL", host_ip, result)
    Disconnect(si)
```

### RECOVERY (0.30 이하)

```python
async def recovery_response(host_ip: str, result):
    si   = connect_esxi(host_ip)
    host = get_host_object(si)

    task = host.ExitMaintenanceMode(timeout=300)
    await wait_for_task(task)

    await send_slack_alert("✅ RECOVERY", result)
    await log_action("RECOVERY", host_ip, result)
    Disconnect(si)
```

---

## Slack 알림 포맷

```python
async def send_slack_alert(level: str, result):
    model_scores = "\n".join([
        f"  • {k}: {v:.3f}"
        for k, v in result.model_scores.items()
    ])
    top3 = "\n".join([
        f"  • {c['feature']}: {c['impact']:.3f}"
        for c in result.top_causes[:3]
    ])
    message = {
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text",
                      "text": f"{level} 메모리 장애 예측"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*서버:* {result.server_id}"},
                {"type": "mrkdwn", "text": f"*확률:* {result.failure_probability:.1%}"},
                {"type": "mrkdwn", "text": f"*의심 DIMM:* {result.suspect_dimm}"},
                {"type": "mrkdwn", "text": f"*모델별 스코어:*\n{model_scores}"},
                {"type": "mrkdwn", "text": f"*주요 원인:*\n{top3}"},
            ]}
        ]
    }
    await post_to_slack(SLACK_WEBHOOK_URL, message)
```

---

## audit_log 테이블

```sql
CREATE TABLE audit_log (
    id           SERIAL PRIMARY KEY,
    action_time  TIMESTAMP DEFAULT NOW(),
    host_ip      VARCHAR(20),
    server_id    VARCHAR(50),
    action_type  VARCHAR(20),
    probability  FLOAT,
    model_scores JSONB,
    suspect_dimm VARCHAR(50),
    operator     VARCHAR(50) DEFAULT 'auto',
    notes        TEXT
);
```
