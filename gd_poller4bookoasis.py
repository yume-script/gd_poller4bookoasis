# -*- coding: utf-8 -*-
"""
gd_poller4bookoasis 플러그인
------------------------------
설정된 구글 드라이브 폴더(하위 포함)의 변경사항을 주기적으로 폴링하다가,
변경이 감지되면
  1) rclone RC API(/vfs/refresh)를 호출해 해당 mount의 VFS 캐시를 refresh
  2) (선택) 디스코드 웹훅으로 변경 내역 알림
을 수행하는 대시보드형 플러그인.

## 실행 방식 (APScheduler 연동)
cache_cleaner 플러그인과 동일한 패턴을 사용한다:
자체 스레드로 time.sleep 루프를 도는 대신, 코어가 이미 쓰고 있는
`services.scheduler_service.scheduler` (APScheduler BackgroundScheduler)
싱글톤에 잡을 등록해서 실행한다. CRON_SCHEDULE을 주면 진짜 crontab
표현식을 쓸 수 있고, 비워두면 POLL_INTERVAL_SECONDS 기반의 단순 반복
(IntervalTrigger)으로 동작한다.

주의: 코어의 `SchedulerService.reload_all_jobs()`는 호출될 때마다 등록된
잡을 전부 지우고 라이브러리 스캔 잡만 재등록한다. 그래서 이 플러그인의
잡도 다른 라이브러리 작업(추가/수정 등)으로 reload가 발생하면 같이
지워질 수 있다. 이를 막기 위해 5분마다 잡 생존 여부만 가볍게 확인해서
없으면 재등록하는 워치독 스레드를 별도로 둔다.

`services.scheduler_service`를 못 불러오는 환경(플러그인 샌드박스 등)이면
예전처럼 자체 스레드 루프로 폴백한다.

## db이름:REMOTE_NAME:구글폴더ID 다중 매핑 (WATCH_TARGETS)
플러그인 설정 화면이 general/adult/audiobook을 구분해서 값을 따로 넣을
수 있는 구조가 아니라(설정은 사실상 전역 하나), "db - remote - 폴더ID"
쌍을 여러 개 감시하고 싶으면 WATCH_TARGETS 설정 하나에 세미콜론(;)으로
구분해서 적는다.

    형식: <라벨>:<REMOTE_NAME>:<구글드라이브 폴더 ID>;<라벨>:<REMOTE_NAME>:<폴더ID>;...
    예)
    general:gds2:1AbCdEfGhIjKlMnOpQrSt;adult:gds2:1XyZ9876543210AbCdEfGh

세미콜론을 구분자로 쓰는 이유: config_schema가 실제 UI에서 한 줄짜리
입력창으로 렌더링되는 경우, 사용자가 입력한 줄바꿈이 저장 과정에서
공백으로 뭉개지는 문제가 실제로 있었다. 줄바꿈(엔터로 여러 줄 입력이
가능한 환경이면 그것도 여전히 지원됨)에 의존하지 않는 세미콜론 구분이
더 안전하다.

각 항목이 독립적으로 인덱싱/폴링/refresh/알림 처리된다. 라벨은 로그와
디스코드 메시지 구분용일 뿐, 실제 BookOasis 라이브러리 스코프와는
무관하다 (rclone VFS refresh 자체가 라이브러리 스코프와 무관한 공용
동작이기 때문).

## 첫 인덱싱 / Changes API page_token 영속화
구글 드라이브 폴더 하위 트리 전체를 매번 다시 훑으면 비효율적이므로,
타겟별로 최초 1회만 전체 인덱싱(folder_ids, item_ids)하고 이후에는
Drive Changes API의 page_token만 이어서 사용한다. 이 상태는 타겟별
상태 파일(state_<라벨>.json)에 저장해 프로세스/서버 재시작에도 이어진다.

## 상태 확인
1) 설정 화면(settings.html 또는 config_schema 자동 폼 옆 상태 영역): get_status() 참고
2) 로그 파일: <STATE_DIR>/gd_poller4bookoasis.log (타겟별 라벨이 각 줄에 표시됨)

수동으로 즉시 1회 확인하고 싶다면 handle_action(db_type, "check_now")를 호출하면 된다.
"""

import os
import json
import time
import threading
from datetime import datetime

from plugins.metadata.base import BaseMetadataProvider


def run_gd_poller4bookoasis_job(db_type):
    """
    APScheduler가 직접 호출하는 모듈 레벨 함수.
    인스턴스 상태에 의존하지 않도록 매번 새 provider를 만들어 쓴다.
    """
    provider = GdPoller4BookOasisProvider()
    provider.check_all_targets(db_type)


class GdPoller4BookOasisProvider(BaseMetadataProvider):
    id = "gd_poller4bookoasis"
    name = "gd_poller4bookoasis"
    is_searchable = False

    config_schema = [
        {"key": "WATCH_TARGETS", "label": "감시 대상 (라벨:REMOTE_NAME:구글폴더ID, 여러 개는 세미콜론 ; 으로 구분)",
         "type": "text", "required": True, "default": "general:gdrive:"},
        {"key": "RC_ADDR", "label": "rclone RC 주소", "type": "text",
         "required": True, "default": "http://localhost:5572"},
        {"key": "RC_USER", "label": "rclone RC 사용자 (선택)", "type": "text", "required": False, "default": ""},
        {"key": "RC_PASS", "label": "rclone RC 비밀번호 (선택)", "type": "password", "required": False, "default": ""},
        {"key": "RC_FS", "label": "rclone fs 지정 (여러 mount일 때만, 선택)", "type": "text",
         "required": False, "default": ""},
        {"key": "DISCORD_WEBHOOK_URL", "label": "디스코드 웹훅 URL (선택, 비우면 알림 없음)",
         "type": "password", "required": False, "default": ""},
        {"key": "POLL_INTERVAL_SECONDS", "label": "폴링 주기 (초) - CRON_SCHEDULE이 비어있을 때만 사용",
         "type": "text", "required": False, "default": "15"},
        {"key": "CRON_SCHEDULE", "label": "Cron 표현식 (비우면 위 주기(초) 기반 반복 사용)",
         "type": "text", "required": False, "default": ""},
        {"key": "HEARTBEAT_EVERY_N_RUNS", "label": "하트비트 알림 주기 (N번 폴링마다 1번, 0=끔)",
         "type": "text", "required": False, "default": "0"},
    ]

    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/<org>/<repo>/<branch>/plugins/metadata/gd_poller4bookoasis",
        "files": ["gd_poller4bookoasis.py", "__init__.py", "VERSION"],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }

    # 대시보드 카드 렌더러가 "도서 카드" 틀에 고정돼 있어 안 맞으므로
    # 대시보드에는 노출하지 않는다. 상태 확인은 설정 화면에서 처리.
    dashboard_widget = None

    _watchdog_threads = {}
    _scheduler_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 필수 인터페이스 (검색 미지원 플러그인)
    # ------------------------------------------------------------------
    def search(self, db_type, query):
        return []

    def apply(self, db_type, book_id, item_data):
        return False, "gd_poller4bookoasis는 백그라운드 감시 전용 플러그인입니다."

    # ------------------------------------------------------------------
    # 활성화/비활성화 훅
    # ------------------------------------------------------------------
    def on_enable(self, db_type):
        self._register_job(db_type)
        self._ensure_watchdog(db_type)
        self._notify_startup(db_type)

    def on_disable(self, db_type):
        try:
            from services.scheduler_service import scheduler
            job = scheduler.get_job(self._job_id(db_type))
            if job:
                scheduler.remove_job(self._job_id(db_type))
        except Exception:
            pass

    @staticmethod
    def _job_id(db_type):
        return f"gd_poller4bookoasis_{db_type}"

    def _build_trigger(self, db_type):
        cfg = self.get_plugin_config(db_type, default={})
        cron_expr = (cfg.get("CRON_SCHEDULE") or "").strip()
        if cron_expr:
            from apscheduler.triggers.cron import CronTrigger
            return CronTrigger.from_crontab(cron_expr)

        from apscheduler.triggers.interval import IntervalTrigger
        try:
            interval_sec = int(cfg.get("POLL_INTERVAL_SECONDS") or 15)
        except (TypeError, ValueError):
            interval_sec = 15
        return IntervalTrigger(seconds=max(5, interval_sec))

    def _register_job(self, db_type):
        try:
            from services.scheduler_service import scheduler
        except Exception:
            self._register_fallback_thread(db_type)
            return False

        try:
            trigger = self._build_trigger(db_type)
        except ValueError as e:
            self._log_line(f"[전체] 잘못된 CRON_SCHEDULE: {e}")
            return False

        try:
            scheduler.add_job(
                run_gd_poller4bookoasis_job,
                trigger,
                id=self._job_id(db_type),
                args=[db_type],
                replace_existing=True,
                max_instances=1,
            )
            return True
        except Exception as e:
            self._log_line(f"[전체] 스케줄러 등록 실패: {e}")
            return False

    def _ensure_watchdog(self, db_type):
        with GdPoller4BookOasisProvider._scheduler_lock:
            existing = GdPoller4BookOasisProvider._watchdog_threads.get(db_type)
            if existing and existing.is_alive():
                return
            t = threading.Thread(
                target=self._watchdog_loop,
                args=(db_type,),
                daemon=True,
                name=f"gd_poller4bookoasis_watchdog_{db_type}",
            )
            GdPoller4BookOasisProvider._watchdog_threads[db_type] = t
            t.start()

    def _watchdog_loop(self, db_type):
        while True:
            try:
                from services.scheduler_service import scheduler
                if not scheduler.get_job(self._job_id(db_type)):
                    self._register_job(db_type)
            except Exception:
                self._register_fallback_thread(db_type)
            time.sleep(300)

    # --- 폴백: 코어 스케줄러가 없는 환경용 자체 스레드 루프 ---
    def _register_fallback_thread(self, db_type):
        key = f"fallback_{db_type}"
        with GdPoller4BookOasisProvider._scheduler_lock:
            existing = GdPoller4BookOasisProvider._watchdog_threads.get(key)
            if existing and existing.is_alive():
                return
            t = threading.Thread(
                target=self._fallback_loop,
                args=(db_type,),
                daemon=True,
                name=f"gd_poller4bookoasis_fallback_{db_type}",
            )
            GdPoller4BookOasisProvider._watchdog_threads[key] = t
            t.start()

    def _fallback_loop(self, db_type):
        while True:
            cfg = self.get_plugin_config(db_type, default={})
            try:
                interval_sec = int(cfg.get("POLL_INTERVAL_SECONDS") or 15)
            except (TypeError, ValueError):
                interval_sec = 15
            self.check_all_targets(db_type)
            time.sleep(max(5, interval_sec))

    # ------------------------------------------------------------------
    # WATCH_TARGETS 파싱 ("라벨:REMOTE_NAME:폴더ID" 항목을 세미콜론(;)
    # 또는 줄바꿈으로 구분). 세미콜론을 기본 구분자로 안내하는 이유:
    # config_schema의 textarea 타입이 실제 UI에서 진짜 여러 줄 입력으로
    # 렌더링된다는 보장이 없고, 한 줄짜리 입력창으로 렌더링되면 사용자가
    # 입력한 줄바꿈이 저장 과정에서 공백으로 뭉개질 수 있기 때문이다
    # (실제로 이 문제가 발생해서 세미콜론 구분으로 바꿨다). 줄바꿈이
    # 살아있는 환경도 여전히 지원하도록 둘 다 구분자로 받는다.
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_watch_targets(cfg):
        import re
        raw = cfg.get("WATCH_TARGETS") or ""
        entries = [e.strip() for e in re.split(r"[;\n]+", raw)]
        targets = []
        for idx, entry in enumerate(entries, start=1):
            if not entry or entry.startswith("#"):
                continue
            parts = entry.split(":", 2)
            if len(parts) != 3:
                targets.append({
                    "label": f"항목{idx}",
                    "remote_name": None,
                    "folder_id": None,
                    "parse_error": f"형식 오류 (라벨:REMOTE_NAME:폴더ID 여야 함): '{entry}'",
                })
                continue
            label, remote_name, folder_id = (p.strip() for p in parts)
            if not label or not remote_name or not folder_id:
                targets.append({
                    "label": label or f"항목{idx}",
                    "remote_name": remote_name or None,
                    "folder_id": folder_id or None,
                    "parse_error": f"빈 값 있음: '{entry}'",
                })
                continue
            targets.append({
                "label": label,
                "remote_name": remote_name,
                "folder_id": folder_id,
                "parse_error": None,
            })
        return targets

    # ------------------------------------------------------------------
    # 상태/로그 파일 경로 (플러그인 폴더 내부, 타겟 라벨 기준)
    # ------------------------------------------------------------------
    def _plugin_dir(self):
        return os.path.dirname(os.path.abspath(__file__))

    def _safe_label(self, label):
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in label)

    def _state_path(self, label):
        return os.path.join(self._plugin_dir(), f"state_{self._safe_label(label)}.json")

    def _log_path(self):
        return os.path.join(self._plugin_dir(), "gd_poller4bookoasis.log")

    def _read_state(self, label):
        path = self._state_path(label)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_state(self, label, state):
        try:
            with open(self._state_path(label), "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except OSError:
            pass

    def _log_line(self, text):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self._log_path(), "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {text}\n")
        except OSError:
            pass

    def _log(self, label, result):
        self._log_line(
            f"[{label}] mode={result.get('mode', 'ok')} "
            f"changes={result.get('changes_found', 0)} "
            f"error={result.get('error', '-')}"
        )
        state = self._read_state(label) or {}
        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_result"] = result
        self._write_state(label, state)

    # ------------------------------------------------------------------
    # rclone RC API(config/dump)로 OAuth 토큰 읽기
    # 도커 환경에서는 컨테이너 안에 rclone.conf 파일을 두거나 경로를
    # 맞추는 게 번거로우므로, 파일을 직접 읽지 않고 이미 설정되어 있는
    # RC_ADDR로 rclone에게 직접 물어본다. rclone rc의 config/dump는
    # 토큰을 포함한 remote 설정 전체를 JSON으로 그대로 돌려준다.
    # (참고: https://rclone.org/rc/ - "config/dump ... expose them")
    # ------------------------------------------------------------------
    def _load_credentials(self, db_type, remote_name):
        import requests
        from google.oauth2.credentials import Credentials

        cfg = self.get_plugin_config(db_type, default={})
        rc_addr = cfg.get("RC_ADDR") or "http://localhost:5572"
        rc_user = cfg.get("RC_USER") or ""
        rc_pass = cfg.get("RC_PASS") or ""
        auth = (rc_user, rc_pass) if rc_user else None

        resp = requests.post(f"{rc_addr}/config/dump", auth=auth, timeout=15)
        resp.raise_for_status()
        all_remotes = resp.json()

        if remote_name not in all_remotes:
            raise RuntimeError(
                f"rclone RC({rc_addr})의 config/dump 응답에 remote '{remote_name}'가 없습니다. "
                f"WATCH_TARGETS의 REMOTE_NAME과 rclone에 등록된 이름이 일치하는지 확인하세요."
            )

        section = all_remotes[remote_name]
        client_id = section.get("client_id") or None
        client_secret = section.get("client_secret") or None
        token_raw = section.get("token")
        if not token_raw:
            raise RuntimeError(f"remote '{remote_name}'에 token 정보가 없습니다 (rclone에서 인증이 안 된 상태일 수 있음)")

        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        return Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive"],
        )

    def _collect_subtree_ids(self, drive, root_folder_id):
        folder_ids = {root_folder_id}
        all_item_ids = {root_folder_id}
        queue = [root_folder_id]
        while queue:
            parent_id = queue.pop(0)
            page_token = None
            while True:
                resp = drive.files().list(
                    q=f"'{parent_id}' in parents and trashed=false",
                    fields="nextPageToken,files(id,mimeType)",
                    pageSize=1000,
                    pageToken=page_token,
                ).execute()
                for f in resp.get("files", []):
                    all_item_ids.add(f["id"])
                    if f.get("mimeType") == "application/vnd.google-apps.folder":
                        folder_ids.add(f["id"])
                        queue.append(f["id"])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return folder_ids, all_item_ids

    def _call_rclone_rc_refresh(self, db_type):
        import requests
        cfg = self.get_plugin_config(db_type, default={})
        rc_addr = cfg.get("RC_ADDR") or "http://localhost:5572"
        rc_user = cfg.get("RC_USER") or ""
        rc_pass = cfg.get("RC_PASS") or ""
        rc_fs = cfg.get("RC_FS") or None

        params = {"recursive": "true"}
        if rc_fs:
            params["fs"] = rc_fs
        auth = (rc_user, rc_pass) if rc_user else None

        resp = requests.post(f"{rc_addr}/vfs/refresh", params=params, auth=auth, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _notify_discord(self, db_type, label, change_lines):
        import requests
        cfg = self.get_plugin_config(db_type, default={})
        webhook_url = cfg.get("DISCORD_WEBHOOK_URL") or ""
        if not webhook_url or not change_lines:
            return
        body = "\n".join(change_lines)
        if len(body) > 1900:
            shown = change_lines[:20]
            body = "\n".join(shown) + f"\n...외 {len(change_lines) - len(shown)}건"
        message = f"📁 [{label}] 구글 드라이브 변경 감지\n{body}"
        try:
            resp = requests.post(webhook_url, json={"content": message}, timeout=15)
            resp.raise_for_status()
        except requests.RequestException:
            pass  # 알림 실패는 refresh 자체를 막지 않음

    def _notify_simple(self, db_type, message):
        """변경 목록 없이 단문 메시지만 보낼 때 (시작 알림/하트비트용)"""
        import requests
        cfg = self.get_plugin_config(db_type, default={})
        webhook_url = cfg.get("DISCORD_WEBHOOK_URL") or ""
        if not webhook_url:
            return
        try:
            resp = requests.post(webhook_url, json={"content": message}, timeout=15)
            resp.raise_for_status()
        except requests.RequestException:
            pass

    def _notify_startup(self, db_type):
        cfg = self.get_plugin_config(db_type, default={})
        targets = self._parse_watch_targets(cfg)
        interval = cfg.get("POLL_INTERVAL_SECONDS") or "15"
        cron = (cfg.get("CRON_SCHEDULE") or "").strip()
        schedule_desc = f"cron: {cron}" if cron else f"{interval}초 주기"
        target_desc = "\n".join(
            (f"- {t['label']}: {t.get('remote_name')}:{t.get('folder_id')}"
             if not t.get("parse_error") else f"- {t['label']}: ⚠️ {t['parse_error']}")
            for t in targets
        ) or "(설정된 감시 대상 없음)"
        self._notify_simple(
            db_type,
            f"🟢 gd_poller4bookoasis 감시 시작 ({schedule_desc})\n{target_desc}",
        )

    # ------------------------------------------------------------------
    # 핵심 로직 — 타겟 1개 점검
    # ------------------------------------------------------------------
    def check_target(self, db_type, target):
        from googleapiclient.discovery import build

        label = target["label"]
        if target.get("parse_error"):
            return {"mode": "error", "error": target["parse_error"], "changes_found": 0}

        remote_name = target["remote_name"]
        folder_id = target["folder_id"]

        creds = self._load_credentials(db_type, remote_name)
        drive = build("drive", "v3", credentials=creds)

        state = self._read_state(label) or {}

        # 최초 실행 시에만 하위 트리 전체 인덱싱
        if "folder_ids" not in state or state.get("indexed_folder_id") != folder_id:
            folder_ids, item_ids = self._collect_subtree_ids(drive, folder_id)
            page_token = drive.changes().getStartPageToken().execute()["startPageToken"]
            state = {
                "indexed_folder_id": folder_id,
                "folder_ids": list(folder_ids),
                "item_ids": list(item_ids),
                "page_token": page_token,
            }
            self._write_state(label, state)
            return {"mode": "indexed", "changes_found": 0,
                    "note": f"초기 인덱싱 완료: 폴더 {len(folder_ids)}개, 항목 {len(item_ids)}개"}

        folder_ids = set(state["folder_ids"])
        item_ids = set(state["item_ids"])
        page_token = state["page_token"]

        change_lines = []
        request = drive.changes().list(
            pageToken=page_token,
            spaces="drive",
            fields="nextPageToken,newStartPageToken,"
                   "changes(fileId,removed,file(name,parents,mimeType))",
        )
        while request is not None:
            response = request.execute()
            for ch in response.get("changes", []):
                file_id = ch["fileId"]
                removed = ch.get("removed", False)
                file_info = ch.get("file") or {}
                parents = file_info.get("parents", [])

                if removed:
                    if file_id in item_ids:
                        item_ids.discard(file_id)
                        folder_ids.discard(file_id)
                        change_lines.append(f"🗑️ 삭제: {file_id}")
                    continue

                if any(p in folder_ids for p in parents):
                    item_ids.add(file_id)
                    if file_info.get("mimeType") == "application/vnd.google-apps.folder":
                        folder_ids.add(file_id)
                    name = file_info.get("name", file_id)
                    change_lines.append(f"✏️ 변경: {name}")

            if "newStartPageToken" in response:
                page_token = response["newStartPageToken"]
            request = drive.changes().list_next(request, response)

        # 상태 갱신 (인덱스 정보는 유지, page_token/집합만 업데이트)
        state["folder_ids"] = list(folder_ids)
        state["item_ids"] = list(item_ids)
        state["page_token"] = page_token
        run_count = int(state.get("run_count", 0)) + 1
        state["run_count"] = run_count
        self._write_state(label, state)

        if change_lines:
            self._call_rclone_rc_refresh(db_type)
            self._notify_discord(db_type, label, change_lines)
        else:
            cfg = self.get_plugin_config(db_type, default={})
            try:
                heartbeat_n = int(cfg.get("HEARTBEAT_EVERY_N_RUNS") or 0)
            except (TypeError, ValueError):
                heartbeat_n = 0
            if heartbeat_n > 0 and run_count % heartbeat_n == 0:
                self._notify_simple(db_type, f"💓 [{label}] 정상 동작 중 (누적 {run_count}회 폴링, 변경 없음)")

        return {"mode": "ok", "changes_found": len(change_lines), "changes": change_lines[:20]}

    # ------------------------------------------------------------------
    # WATCH_TARGETS 전체 순회 (APScheduler 잡 / 워치독 폴백 / 수동 실행 공용)
    # ------------------------------------------------------------------
    def check_all_targets(self, db_type):
        cfg = self.get_plugin_config(db_type, default={})
        targets = self._parse_watch_targets(cfg)

        if not targets:
            self._log_line("[전체] WATCH_TARGETS가 비어있습니다")
            return []

        results = []
        for target in targets:
            label = target["label"]
            try:
                result = self.check_target(db_type, target)
            except Exception as e:
                result = {"mode": "error", "error": str(e), "changes_found": 0}
            self._log(label, result)
            results.append({"label": label, **result})
        return results

    # ------------------------------------------------------------------
    # 상태 데이터 (설정 화면 JS가 소비)
    # ------------------------------------------------------------------
    def get_status(self, db_type):
        try:
            from services.scheduler_service import scheduler
            if not scheduler.get_job(self._job_id(db_type)):
                self._register_job(db_type)
        except Exception:
            self._register_fallback_thread(db_type)
        self._ensure_watchdog(db_type)

        cfg = self.get_plugin_config(db_type, default={})
        targets = self._parse_watch_targets(cfg)

        per_target = []
        for target in targets:
            label = target["label"]
            state = self._read_state(label) or {}
            last_result = state.get("last_result") or {}
            per_target.append({
                "label": label,
                "remote_name": target.get("remote_name"),
                "folder_id": target.get("folder_id"),
                "parse_error": target.get("parse_error"),
                "last_run": state.get("last_run", "아직 실행 안 됨"),
                "last_mode": last_result.get("mode", "-"),
                "last_changes_found": last_result.get("changes_found", 0),
                "run_count": state.get("run_count", 0),
                "indexed": bool(state.get("folder_ids")),
            })

        next_run = None
        scheduler_backend = "fallback_thread"
        try:
            from services.scheduler_service import scheduler
            job = scheduler.get_job(self._job_id(db_type))
            if job and job.next_run_time:
                next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
                scheduler_backend = "apscheduler"
        except Exception:
            pass

        return {
            "success": True,
            "targets": per_target,
            "next_run": next_run or "확인 불가 (폴백 스레드 모드)",
            "scheduler_backend": scheduler_backend,
        }

    def get_dashboard_data(self, db_type, limit=6):
        status = self.get_status(db_type)
        status["items"] = []
        return status

    # ------------------------------------------------------------------
    # 설정 화면의 "지금 즉시 확인" 버튼이 호출할 액션
    # (코어에 커스텀 액션 라우트가 있다는 전제 - cache_cleaner와 동일한 제약)
    # ------------------------------------------------------------------
    def handle_action(self, db_type, action, payload=None):
        if action == "check_now":
            results = self.check_all_targets(db_type)
            return {"success": True, "results": results}
        return {"success": False, "error": f"알 수 없는 action: {action}"}


# ------------------------------------------------------------------
# 모듈 import 시점에 자체 등록 (on_enable 훅에 의존하지 않음)
#
# 코어가 플러그인 활성화 시 on_enable을 실제로 호출해주는지 문서/코드로
# 확실히 보장되지 않는다 (cache_cleaner 원작자도 동일한 우려를 남겼다).
# 반면 "이 파일이 정상적으로 import된다"는 것은 코어의 플러그인 로드
# 성공 여부(환경설정 > 플러그인 설정 화면의 로드 실패 목록)로 바로 확인
# 가능한 확실한 지점이다. 그래서 on_enable을 기다리지 않고, 모듈이
# import되는 즉시 잡 등록을 시도한다.
#
# 설정 저장소 자체가 "general" 스코프 하나뿐이라(실제 UI가 그렇게
# 되어 있음) 그 스코프로만 잡을 등록한다. general/adult/audiobook
# 여러 db를 감시하고 싶으면 스코프를 늘리는 게 아니라, WATCH_TARGETS
# 설정 안에 "라벨:REMOTE_NAME:폴더ID" 줄을 여러 개 적으면 된다.
# ------------------------------------------------------------------
def _auto_register():
    try:
        _provider = GdPoller4BookOasisProvider()
        _provider._register_job("general")
        _provider._ensure_watchdog("general")
    except Exception:
        pass


_auto_register()
