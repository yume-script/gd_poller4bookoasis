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
싱글톤에 잡을 등록해서 실행한다. POLL_INTERVAL_SECONDS 주기의 단순
반복(IntervalTrigger)으로 동작한다.

주의: 코어의 `SchedulerService.reload_all_jobs()`는 호출될 때마다 등록된
잡을 전부 지우고 라이브러리 스캔 잡만 재등록한다. 그래서 이 플러그인의
잡도 다른 라이브러리 작업(추가/수정 등)으로 reload가 발생하면 같이
지워질 수 있다. 이를 막기 위해 5분마다 잡 생존 여부만 가볍게 확인해서
없으면 재등록하는 워치독 스레드를 별도로 둔다.

`services.scheduler_service`를 못 불러오는 환경(플러그인 샌드박스 등)이면
예전처럼 자체 스레드 루프로 폴백한다.

## db이름:REMOTE_NAME:구글폴더ID 다중 매핑 (WATCH_TARGET_1 ~ WATCH_TARGET_5)
플러그인 설정 화면이 general/adult/audiobook을 구분해서 값을 따로 넣을
수 있는 구조가 아니라(설정은 사실상 전역 하나), "db - remote - 폴더ID"
쌍을 여러 개 감시하고 싶으면 독립된 입력 필드 WATCH_TARGET_1 ~
WATCH_TARGET_5에 하나씩 나눠 적는다 (최대 5개, 안 쓰는 건 비워두면 됨).

    형식 (각 필드마다): <라벨>:<REMOTE_NAME>:<구글드라이브 폴더 ID>
    예)
    WATCH_TARGET_1 = general:gds2:1AbCdEfGhIjKlMnOpQrSt
    WATCH_TARGET_2 = adult:gds2:1XyZ9876543210AbCdEfGh

한 줄짜리 입력창에 세미콜론/줄바꿈으로 여러 항목을 구분하는 방식도
시도했었는데, 저장 과정에서 구분자가 깨지는 문제가 실제로 있었다.
필드를 아예 물리적으로 나누면 그 문제 자체가 생기지 않는다.

각 필드가 독립적으로 인덱싱/폴링/refresh/알림 처리된다. 라벨은 로그와
디스코드 메시지 구분용일 뿐, 실제 BookOasis 라이브러리 스코프와는
무관하다 (rclone VFS refresh 자체가 라이브러리 스코프와 무관한 공용
동작이기 때문).

## 첫 인덱싱 / Changes API page_token 영속화
구글 드라이브 폴더 하위 트리 전체를 매번 다시 훑으면 비효율적이므로,
타겟별로 최초 1회만 전체 인덱싱(folder_ids, item_ids)하고 이후에는
Drive Changes API의 page_token만 이어서 사용한다. 이 상태는 타겟별
상태 파일(state_<remote_name>_<폴더ID>.json)에 저장해 프로세스/서버
재시작에도 이어진다. 상태 파일 키를 라벨이 아니라 remote_name+폴더ID
조합으로 잡는 이유는, 서로 다른 REMOTE_NAME을 쓰는 두 타겟이 같은
라벨을 쓰는 경우에도 상태가 안 섞이게 하기 위함이다.

## 상태 확인 / 즉시 실행 (settings.html + settings.js)
플러그인 설정 화면은 index.html이 아니라 settings.html/settings.js로
렌더링된다 (index.html/style.css/script.js는 대시보드 카테고리 레벨
전용). 코어에 "즉시 실행" 전용 백엔드 액션 엔드포인트가 없다는 게
실제로 확인되어서 (cache_cleaner 플러그인 개발 과정에서 404 확인),
같은 우회 패턴을 쓴다:

    settings.js가 save-config 호출 시 config에 RUN_NOW_TOKEN(타임스탬프)을
    슬쩍 끼워 저장 -> 이 플러그인이 5초 주기 전용 워치 잡으로 그 값이
    바뀐 걸 감지하면 즉시 check_all_targets() 실행. 로그 지우기도
    LOG_CLEAR_TOKEN으로 동일한 방식.

상태는 get_dashboard_data()가 실제로 프론트에서 불리는 게 확인된
엔드포인트(`/api/media/dashboard/widgets/{id}/data`)라서, get_status()의
결과를 그대로 노출한다.

1) 설정 화면(settings.html): get_dashboard_data() 참고
2) 로그 파일: <STATE_DIR>/gd_poller4bookoasis.log
   각 줄에 "라벨:REMOTE_NAME:폴더ID(앞부분)" 형태로 표시되어, REMOTE_NAME이
   여러 개일 때도 어느 타겟인지 바로 구분 가능하다.
"""

import os
import json
import time
import threading
from datetime import datetime

from plugins.metadata.base import BaseMetadataProvider


def run_gd_poller4bookoasis_job(db_type):
    """
    APScheduler가 직접 호출하는 모듈 레벨 함수 (메인 폴링 주기).
    인스턴스 상태에 의존하지 않도록 매번 새 provider를 만들어 쓴다.
    """
    provider = GdPoller4BookOasisProvider()
    provider.check_all_targets(db_type)


def run_gd_poller4bookoasis_watch_job(db_type):
    """
    RUN_NOW_TOKEN / LOG_CLEAR_TOKEN 감지 전용, 5초 주기로 도는 가벼운 잡.
    cache_cleaner와 동일한 "즉시 실행" 우회 패턴.
    """
    provider = GdPoller4BookOasisProvider()
    provider._check_tokens(db_type)


class GdPoller4BookOasisProvider(BaseMetadataProvider):
    id = "gd_poller4bookoasis"
    name = "gd_poller4bookoasis"
    is_searchable = False

    config_schema = [
        {"key": "WATCH_TARGET_1", "label": "감시 대상 1 (라벨:REMOTE_NAME:구글폴더ID)", "type": "text",
         "required": True, "default": ""},
        {"key": "WATCH_TARGET_2", "label": "감시 대상 2 (선택, 비우면 사용 안 함)", "type": "text",
         "required": False, "default": ""},
        {"key": "WATCH_TARGET_3", "label": "감시 대상 3 (선택, 비우면 사용 안 함)", "type": "text",
         "required": False, "default": ""},
        {"key": "WATCH_TARGET_4", "label": "감시 대상 4 (선택, 비우면 사용 안 함)", "type": "text",
         "required": False, "default": ""},
        {"key": "WATCH_TARGET_5", "label": "감시 대상 5 (선택, 비우면 사용 안 함)", "type": "text",
         "required": False, "default": ""},
        {"key": "RC_ADDR", "label": "rclone RC 주소", "type": "text",
         "required": True, "default": "http://localhost:5572"},
        {"key": "RC_USER", "label": "rclone RC 사용자 (선택)", "type": "text", "required": False, "default": ""},
        {"key": "RC_PASS", "label": "rclone RC 비밀번호 (선택)", "type": "password", "required": False, "default": ""},
        {"key": "RC_FS", "label": "refresh 대상 fs (union 등 마운트에 실제 쓴 이름, 여러 mount일 때 필수)",
         "type": "text", "required": False, "default": ""},
        {"key": "DISCORD_WEBHOOK_URL", "label": "디스코드 웹훅 URL (선택, 비우면 알림 없음)",
         "type": "password", "required": False, "default": ""},
        {"key": "POLL_INTERVAL_SECONDS", "label": "폴링 주기 (초)",
         "type": "text", "required": False, "default": "15"},
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
        self._register_watch_job(db_type)
        self._ensure_watchdog(db_type)
        self._notify_startup(db_type)

    def on_disable(self, db_type):
        try:
            from services.scheduler_service import scheduler
            for jid in (self._job_id(db_type), self._watch_job_id(db_type)):
                if scheduler.get_job(jid):
                    scheduler.remove_job(jid)
        except Exception:
            pass

    @staticmethod
    def _job_id(db_type):
        return f"gd_poller4bookoasis_{db_type}"

    @staticmethod
    def _watch_job_id(db_type):
        return f"gd_poller4bookoasis_watch_{db_type}"

    def _build_trigger(self, db_type):
        cfg = self.get_plugin_config(db_type, default={})
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

        trigger = self._build_trigger(db_type)

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

    def _register_watch_job(self, db_type):
        """RUN_NOW_TOKEN/LOG_CLEAR_TOKEN 감지용 5초 주기 잡 (메인 폴링과 별개)"""
        try:
            from services.scheduler_service import scheduler
            from apscheduler.triggers.interval import IntervalTrigger
        except Exception:
            return False  # 코어 스케줄러가 없으면 폴백 스레드가 이미 메인 루프에서 대체 처리

        try:
            scheduler.add_job(
                run_gd_poller4bookoasis_watch_job,
                IntervalTrigger(seconds=5),
                id=self._watch_job_id(db_type),
                args=[db_type],
                replace_existing=True,
                max_instances=1,
            )
            return True
        except Exception as e:
            self._log_line(f"[전체] 워치 잡 등록 실패: {e}")
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
                if not scheduler.get_job(self._watch_job_id(db_type)):
                    self._register_watch_job(db_type)
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
            # APScheduler가 없어 5초 워치 잡을 못 쓰는 환경이므로, 메인
            # 루프 안에서라도 토큰을 확인한다 (반응 속도는 떨어짐).
            self._check_tokens(db_type)
            time.sleep(max(5, interval_sec))

    # ------------------------------------------------------------------
    # WATCH_TARGET_1 ~ WATCH_TARGET_5 파싱 ("라벨:REMOTE_NAME:폴더ID")
    #
    # 원래는 세미콜론/줄바꿈으로 구분한 하나의 문자열 설정(WATCH_TARGETS)
    # 이었는데, 설정 화면이 한 줄짜리 입력창이라 여러 줄/구분자 입력이
    # 실수하기 쉽고 저장 과정에서 깨지는 문제가 있었다. 그래서 아예
    # 독립된 입력 필드 5개(WATCH_TARGET_1..5)로 나눴다 - 각 필드는 항목
    # 하나만 담당하므로 구분자 문제 자체가 생기지 않는다. 5개보다 많이
    # 필요하면 config_schema에 WATCH_TARGET_6, 7... 을 같은 패턴으로
    # 추가하고 아래 range()도 늘리면 된다.
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_watch_targets(cfg):
        targets = []
        for i in range(1, 6):
            entry = (cfg.get(f"WATCH_TARGET_{i}") or "").strip()
            if not entry:
                continue
            parts = entry.split(":", 2)
            if len(parts) != 3:
                targets.append({
                    "label": f"WATCH_TARGET_{i}",
                    "remote_name": None,
                    "folder_id": None,
                    "parse_error": f"형식 오류 (라벨:REMOTE_NAME:폴더ID 여야 함): '{entry}'",
                })
                continue
            label, remote_name, folder_id = (p.strip() for p in parts)
            if not label or not remote_name or not folder_id:
                targets.append({
                    "label": label or f"WATCH_TARGET_{i}",
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
    # 상태/로그 파일 경로 (플러그인 폴더 내부)
    #
    # 상태 파일 키는 라벨이 아니라 "remote_name:folder_id" 조합으로 잡는다.
    # 라벨만으로 키를 잡으면, 서로 다른 REMOTE_NAME을 쓰는 두 타겟이 같은
    # 라벨을 쓸 때 상태 파일을 공유해버려서 인덱스/page_token이 서로
    # 덮어써지는 버그가 있었다 (실사용 중 발견됨). remote_name+folder_id는
    # 실제로 감시하는 대상 자체를 가리키므로 중복될 일이 없다.
    # ------------------------------------------------------------------
    def _plugin_dir(self):
        return os.path.dirname(os.path.abspath(__file__))

    def _safe_key(self, text):
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)

    def _state_key(self, target):
        return self._safe_key(f"{target.get('remote_name')}_{target.get('folder_id')}")

    def _display_label(self, target):
        """로그/디스코드 알림에 쓰는 표시용 라벨: 라벨:REMOTE_NAME:폴더ID(앞 10자)"""
        folder_id = target.get("folder_id") or ""
        short_folder = folder_id[:10] + "......." if len(folder_id) > 10 else folder_id
        return f"{target.get('label')}:{target.get('remote_name')}:{short_folder}"

    def _state_path(self, state_key):
        return os.path.join(self._plugin_dir(), f"state_{state_key}.json")

    def _log_path(self):
        return os.path.join(self._plugin_dir(), "gd_poller4bookoasis.log")

    def _read_state(self, state_key):
        path = self._state_path(state_key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_state(self, state_key, state):
        try:
            with open(self._state_path(state_key), "w", encoding="utf-8") as f:
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

    def _log(self, state_key, display_label, result):
        self._log_line(
            f"[{display_label}] mode={result.get('mode', 'ok')} "
            f"changes={result.get('changes_found', 0)} "
            f"error={result.get('error', '-')}"
        )
        state = self._read_state(state_key) or {}
        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_result"] = result
        self._write_state(state_key, state)

    # ------------------------------------------------------------------
    # RUN_NOW_TOKEN / LOG_CLEAR_TOKEN 처리 (settings.js의 "즉시 실행"
    # 우회 패턴 - 자세한 설명은 파일 상단 docstring 참고)
    # ------------------------------------------------------------------
    def _tokens_state_path(self):
        return os.path.join(self._plugin_dir(), "tokens_state.json")

    def _read_tokens_state(self):
        path = self._tokens_state_path()
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_tokens_state(self, tstate):
        try:
            with open(self._tokens_state_path(), "w", encoding="utf-8") as f:
                json.dump(tstate, f, ensure_ascii=False)
        except OSError:
            pass

    def _check_tokens(self, db_type):
        cfg = self.get_plugin_config(db_type, default={})
        run_token = str(cfg.get("RUN_NOW_TOKEN") or "").strip()
        log_clear_token = str(cfg.get("LOG_CLEAR_TOKEN") or "").strip()

        tstate = self._read_tokens_state()
        dirty = False

        if run_token and run_token != tstate.get("last_run_now_token"):
            self.check_all_targets(db_type)
            tstate["last_run_now_token"] = run_token
            tstate["last_run_now_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            dirty = True

        if log_clear_token and log_clear_token != tstate.get("last_log_clear_token"):
            try:
                open(self._log_path(), "w", encoding="utf-8").close()
            except OSError:
                pass
            tstate["last_log_clear_token"] = log_clear_token
            tstate["last_log_clear_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            dirty = True

        if dirty:
            self._write_tokens_state(tstate)

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
                f"WATCH_TARGET_N의 REMOTE_NAME과 rclone에 등록된 이름이 일치하는지 확인하세요."
            )

        section = all_remotes[remote_name]
        client_id = section.get("client_id") or None
        client_secret = section.get("client_secret") or None
        token_raw = section.get("token")
        if not token_raw:
            if (section.get("type") or "").lower() == "union":
                upstreams_raw = section.get("upstreams") or ""
                # "gdrive1:books gdrive2:books" 형태에서 remote 이름만 추출
                upstream_names = [u.split(":", 1)[0] for u in upstreams_raw.split() if u]
                hint = (
                    f" 이 remote를 구성하는 하위 remote: {', '.join(upstream_names)} 중 하나를 "
                    f"REMOTE_NAME으로 지정하세요."
                    if upstream_names else ""
                )
                raise RuntimeError(
                    f"remote '{remote_name}'는 union 타입이라 자체 OAuth 토큰이 없습니다.{hint}"
                )
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
        """
        하위 트리 전체를 순회하며 폴더/전체 항목 ID뿐 아니라, 각 항목의
        이름과 직속 부모 ID(item_meta)도 함께 수집한다. 이걸 저장해두면
        나중에 변경 감지 시 "전체 경로(폴더/파일명)"를 추가 API 호출
        없이 로컬에서 조립할 수 있다.

        구글드라이브 "바로가기(shortcut)"는 mimeType이 폴더가 아니라
        application/vnd.google-apps.shortcut이라서, 그냥 두면 그 안쪽을
        전혀 순회하지 못하고 실제 대상 폴더의 ID도 감시 목록에 안 들어간다
        (Drive Changes API는 바로가기가 아니라 실제 파일의 진짜 위치
        ID로 변경을 알려주기 때문에, 그 ID를 모르면 변경이 조용히
        무시된다). 그래서 바로가기를 만나면 shortcutDetails로 실제
        대상(target)을 확인해서, 대상이 폴더면 그 실제 ID까지 따라
        들어가서 계속 순회한다.
        """
        folder_ids = {root_folder_id}
        all_item_ids = {root_folder_id}
        item_meta = {}  # id -> {"name": str, "parent": parent_id or None, "is_folder": bool}
        queue = [root_folder_id]
        while queue:
            parent_id = queue.pop(0)
            page_token = None
            while True:
                resp = drive.files().list(
                    q=f"'{parent_id}' in parents and trashed=false",
                    fields="nextPageToken,files(id,name,mimeType,shortcutDetails)",
                    pageSize=1000,
                    pageToken=page_token,
                ).execute()
                for f in resp.get("files", []):
                    fid = f["id"]
                    mime = f.get("mimeType")
                    name = f.get("name", fid)

                    if mime == "application/vnd.google-apps.shortcut":
                        shortcut = f.get("shortcutDetails") or {}
                        target_id = shortcut.get("targetId")
                        target_mime = shortcut.get("targetMimeType")
                        if target_id and target_mime == "application/vnd.google-apps.folder":
                            # 바로가기가 가리키는 실제 폴더 ID로 등록하고 그 안까지 순회
                            if target_id not in folder_ids:
                                all_item_ids.add(target_id)
                                item_meta[target_id] = {"name": name, "parent": parent_id, "is_folder": True}
                                folder_ids.add(target_id)
                                queue.append(target_id)
                            continue
                        # 폴더가 아닌 대상(파일)에 대한 바로가기는 그냥 일반 항목으로만 기록
                        all_item_ids.add(fid)
                        item_meta[fid] = {"name": name, "parent": parent_id, "is_folder": False}
                        continue

                    is_folder = mime == "application/vnd.google-apps.folder"
                    all_item_ids.add(fid)
                    item_meta[fid] = {"name": name, "parent": parent_id, "is_folder": is_folder}
                    if is_folder:
                        folder_ids.add(fid)
                        queue.append(fid)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return folder_ids, all_item_ids, item_meta

    def _resolve_full_path(self, name, parent_id, item_meta, root_folder_id):
        """item_meta 체인을 거슬러 올라가며 '상위폴더/하위폴더/파일명' 형태 경로를 조립"""
        parts = []
        seen = set()
        cur = parent_id
        while cur and cur != root_folder_id and cur not in seen:
            seen.add(cur)
            meta = item_meta.get(cur)
            if not meta:
                break
            parts.append(meta["name"])
            cur = meta.get("parent")
        parts.reverse()
        parts.append(name)
        return "/".join(parts)

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

    def _notify_discord(self, db_type, display_label, change_lines):
        import requests
        cfg = self.get_plugin_config(db_type, default={})
        webhook_url = cfg.get("DISCORD_WEBHOOK_URL") or ""
        if not webhook_url or not change_lines:
            return
        body = "\n".join(change_lines)
        if len(body) > 1900:
            shown = change_lines[:20]
            body = "\n".join(shown) + f"\n...외 {len(change_lines) - len(shown)}건"
        message = f"📁 [{display_label}] 구글 드라이브 변경 감지\n{body}"
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
        schedule_desc = f"{interval}초 주기"
        target_desc = "\n".join(
            (f"- {self._display_label(t)}"
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

        if target.get("parse_error"):
            return {"mode": "error", "error": target["parse_error"], "changes_found": 0}

        remote_name = target["remote_name"]
        folder_id = target["folder_id"]
        state_key = self._state_key(target)
        display_label = self._display_label(target)

        creds = self._load_credentials(db_type, remote_name)
        drive = build("drive", "v3", credentials=creds)

        state = self._read_state(state_key) or {}

        # 최초 실행 시에만 하위 트리 전체 인덱싱
        if "folder_ids" not in state or state.get("indexed_folder_id") != folder_id:
            folder_ids, item_ids, item_meta = self._collect_subtree_ids(drive, folder_id)
            page_token = drive.changes().getStartPageToken().execute()["startPageToken"]
            state = {
                "indexed_folder_id": folder_id,
                "folder_ids": list(folder_ids),
                "item_ids": list(item_ids),
                "item_meta": item_meta,
                "page_token": page_token,
            }
            self._write_state(state_key, state)
            return {"mode": "indexed", "changes_found": 0,
                    "note": f"초기 인덱싱 완료: 폴더 {len(folder_ids)}개, 항목 {len(item_ids)}개"}

        folder_ids = set(state["folder_ids"])
        item_ids = set(state["item_ids"])
        item_meta = state.get("item_meta", {})
        page_token = state["page_token"]

        # ------------------------------------------------------------
        # 1단계: 이번 폴링에서 온 변경 이벤트를 전부 모으기만 한다.
        # (바로바로 판정하지 않는 이유는 아래 2단계 설명 참고)
        # ------------------------------------------------------------
        raw_changes = []
        request = drive.changes().list(
            pageToken=page_token,
            spaces="drive",
            fields="nextPageToken,newStartPageToken,"
                   "changes(fileId,removed,file(name,parents,mimeType,shortcutDetails))",
        )
        while request is not None:
            response = request.execute()
            raw_changes.extend(response.get("changes", []))
            if "newStartPageToken" in response:
                page_token = response["newStartPageToken"]
            request = drive.changes().list_next(request, response)

        # ------------------------------------------------------------
        # 2단계: 폴더 생성 이벤트부터 folder_ids/item_meta에 반영 (fixed-point).
        #
        # Drive Changes API는 이벤트 순서를 "부모 폴더가 먼저"로 보장하지
        # 않는다. 깊은 하위 폴더 구조를 한 번에 업로드하면(예: 5단계
        # 중첩), 손자 폴더의 파일 변경 이벤트가 그 부모 폴더의 생성
        # 이벤트보다 먼저 올 수도 있다. 한 번에 다 모아놓고, 새로 생긴
        # 폴더들을 (더 이상 새로 편입되는 폴더가 없을 때까지) 반복
        # 반영한 뒤에 파일 변경을 판정해야 깊은 곳의 변경을 놓치지 않는다.
        # item_meta도 같이 채워야 나중에 전체 경로를 조립할 수 있다.
        #
        # 바로가기(shortcut)도 여기서 같이 처리한다: 새로 생긴 바로가기가
        # 폴더를 가리키면, 그 바로가기 자체의 ID가 아니라 실제 대상
        # 폴더의 ID를 folder_ids에 등록해야 한다 (자세한 이유는 초기
        # 인덱싱 함수 _collect_subtree_ids의 docstring 참고).
        # ------------------------------------------------------------
        changed = True
        while changed:
            changed = False
            for ch in raw_changes:
                if ch.get("removed"):
                    continue
                file_info = ch.get("file") or {}
                mime = file_info.get("mimeType")
                parents = file_info.get("parents", [])
                file_id = ch["fileId"]

                if mime == "application/vnd.google-apps.folder":
                    if file_id in folder_ids:
                        continue
                    if any(p in folder_ids for p in parents):
                        folder_ids.add(file_id)
                        item_meta[file_id] = {
                            "name": file_info.get("name", file_id),
                            "parent": parents[0] if parents else None,
                            "is_folder": True,
                        }
                        changed = True
                    continue

                if mime == "application/vnd.google-apps.shortcut":
                    # 바로가기가 폴더를 가리키면, 그 실제 대상 폴더 ID를
                    # folder_ids에 등록해야 그 안의 변경도 잡을 수 있다
                    # (§ 하단 3단계 주석 참고: Changes API는 대상의 진짜
                    # ID로 변경을 알려주기 때문).
                    shortcut = file_info.get("shortcutDetails") or {}
                    target_id = shortcut.get("targetId")
                    target_mime = shortcut.get("targetMimeType")
                    if (target_id and target_mime == "application/vnd.google-apps.folder"
                            and target_id not in folder_ids
                            and any(p in folder_ids for p in parents)):
                        folder_ids.add(target_id)
                        item_meta[target_id] = {
                            "name": file_info.get("name", target_id),
                            "parent": parents[0] if parents else None,
                            "is_folder": True,
                        }
                        changed = True
                    continue

        # ------------------------------------------------------------
        # 3단계: 완성된 folder_ids/item_meta 기준으로 최종 판정 + 전체 경로 조립
        # ------------------------------------------------------------
        change_lines = []
        changed_paths = []  # 가공용 원본 데이터: [{"path": ..., "removed": bool}, ...]
        for ch in raw_changes:
            file_id = ch["fileId"]
            removed = ch.get("removed", False)
            file_info = ch.get("file") or {}
            parents = file_info.get("parents", [])

            if removed:
                if file_id in item_ids:
                    item_ids.discard(file_id)
                    folder_ids.discard(file_id)
                    cached = item_meta.pop(file_id, None)
                    if cached:
                        full_path = self._resolve_full_path(
                            cached["name"], cached.get("parent"), item_meta, folder_id
                        )
                    else:
                        full_path = file_id  # 캐시에도 없으면 ID로 대체
                    change_lines.append(f"🗑️ 삭제: {full_path}")
                    changed_paths.append({"path": full_path, "removed": True})
                continue

            if any(p in folder_ids for p in parents):
                item_ids.add(file_id)
                name = file_info.get("name", file_id)
                parent_id = parents[0] if parents else None
                item_meta[file_id] = {
                    "name": name,
                    "parent": parent_id,
                    "is_folder": file_info.get("mimeType") == "application/vnd.google-apps.folder",
                }
                full_path = self._resolve_full_path(name, parent_id, item_meta, folder_id)
                change_lines.append(f"✏️ 변경: {full_path}")
                changed_paths.append({"path": full_path, "removed": False})

        # 상태 갱신 (인덱스 정보는 유지, page_token/집합/메타 업데이트)
        state["folder_ids"] = list(folder_ids)
        state["item_ids"] = list(item_ids)
        state["item_meta"] = item_meta
        state["page_token"] = page_token
        run_count = int(state.get("run_count", 0)) + 1
        state["run_count"] = run_count
        self._write_state(state_key, state)

        if change_lines:
            self._call_rclone_rc_refresh(db_type)
            self._notify_discord(db_type, display_label, change_lines)
        else:
            cfg = self.get_plugin_config(db_type, default={})
            try:
                heartbeat_n = int(cfg.get("HEARTBEAT_EVERY_N_RUNS") or 0)
            except (TypeError, ValueError):
                heartbeat_n = 0
            if heartbeat_n > 0 and run_count % heartbeat_n == 0:
                self._notify_simple(db_type, f"💓 [{display_label}] 정상 동작 중 (누적 {run_count}회 폴링, 변경 없음)")

        return {
            "mode": "ok",
            "changes_found": len(change_lines),
            "changes": change_lines[:20],
            "changed_paths": changed_paths[:50],
        }

    # ------------------------------------------------------------------
    # WATCH_TARGET_1~5 전체 순회 (APScheduler 잡 / 워치독 폴백 / 수동 실행 공용)
    #
    # 여러 타겟을 한 번에 몰아서 호출하면 구글 드라이브 API의 분당 요청
    # 한도(rateLimitExceeded)에 걸리기 쉽다. 그래서 타겟들을 동시에
    # 처리하지 않고, "폴링 주기 ÷ 타겟 수" 만큼 시차를 두고 하나씩
    # 순차 호출한다. 예를 들어 주기가 15초고 타겟이 3개면 약 5초
    # 간격으로 하나씩 처리 - 다음 폴링 틱이 오기 전에 전체 순회가
    # 끝나면서도, 순간적으로 API가 몰리는 상황 자체를 피할 수 있다.
    # ------------------------------------------------------------------
    def check_all_targets(self, db_type):
        cfg = self.get_plugin_config(db_type, default={})
        targets = self._parse_watch_targets(cfg)

        if not targets:
            self._log_line("[전체] WATCH_TARGET_1~5가 모두 비어있습니다")
            return []

        try:
            interval_sec = int(cfg.get("POLL_INTERVAL_SECONDS") or 15)
        except (TypeError, ValueError):
            interval_sec = 15
        # cron 스케줄을 쓰는 경우 등 interval을 알 수 없을 때의 기본 시차.
        # 전체 순회가 다음 폴링 틱 전에 여유 있게 끝나도록 80%만 사용.
        stagger_sec = max(0.5, (interval_sec * 0.8) / max(1, len(targets)))

        results = []
        for i, target in enumerate(targets):
            if i > 0:
                time.sleep(stagger_sec)
            display_label = self._display_label(target) if not target.get("parse_error") else target["label"]
            state_key = self._state_key(target) if not target.get("parse_error") else self._safe_key(target["label"])
            try:
                result = self.check_target(db_type, target)
            except Exception as e:
                result = {"mode": "error", "error": str(e), "changes_found": 0}
            self._log(state_key, display_label, result)
            results.append({"label": display_label, **result})
        return results

    # ------------------------------------------------------------------
    # 상태 데이터 (설정 화면 JS가 소비)
    # ------------------------------------------------------------------
    def get_status(self, db_type):
        try:
            from services.scheduler_service import scheduler
            if not scheduler.get_job(self._job_id(db_type)):
                self._register_job(db_type)
            if not scheduler.get_job(self._watch_job_id(db_type)):
                self._register_watch_job(db_type)
        except Exception:
            self._register_fallback_thread(db_type)
        self._ensure_watchdog(db_type)

        cfg = self.get_plugin_config(db_type, default={})
        targets = self._parse_watch_targets(cfg)

        per_target = []
        for target in targets:
            state_key = self._state_key(target) if not target.get("parse_error") else self._safe_key(target["label"])
            display_label = self._display_label(target) if not target.get("parse_error") else target["label"]
            state = self._read_state(state_key) or {}
            last_result = state.get("last_result") or {}
            per_target.append({
                "label": display_label,
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

        try:
            log_size_bytes = os.path.getsize(self._log_path())
        except OSError:
            log_size_bytes = 0

        tstate = self._read_tokens_state()

        return {
            "success": True,
            "targets": per_target,
            "next_run": next_run or "확인 불가 (폴백 스레드 모드)",
            "scheduler_backend": scheduler_backend,
            "log_size_bytes": log_size_bytes,
            # settings.js가 RUN_NOW_TOKEN/LOG_CLEAR_TOKEN 처리 완료 여부를
            # 폴링으로 확인할 때 쓰는 필드 (cache_cleaner의
            # last_run_now_token 패턴과 동일)
            "last_run_now_token": tstate.get("last_run_now_token"),
            "last_run_now_at": tstate.get("last_run_now_at"),
            "last_log_clear_token": tstate.get("last_log_clear_token"),
            "last_log_clear_at": tstate.get("last_log_clear_at"),
        }

    def get_dashboard_data(self, db_type, limit=6):
        status = self.get_status(db_type)
        status["items"] = []
        return status

    # 참고: handle_action 같은 "즉시 실행 전용" 백엔드 액션 엔드포인트는
    # 코어에 존재하지 않는다 (cache_cleaner 개발 과정에서 404로 확인됨).
    # 즉시 실행/로그 지우기는 RUN_NOW_TOKEN/LOG_CLEAR_TOKEN 방식으로
    # 처리한다 (_check_tokens 참고).


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
# 여러 db를 감시하고 싶으면 스코프를 늘리는 게 아니라, WATCH_TARGET_1~5
# 설정 안에 "라벨:REMOTE_NAME:폴더ID" 줄을 여러 개 적으면 된다.
# ------------------------------------------------------------------
def _auto_register():
    try:
        _provider = GdPoller4BookOasisProvider()
        _provider._register_job("general")
        _provider._register_watch_job("general")
        _provider._ensure_watchdog("general")
    except Exception:
        pass

    # 예전 버전에서 만들어진 유령 잡 정리. 스케줄러가 영속 저장소를 쓰는
    # 경우 재시작해도 이 잡들이 안 지워지고 계속 실행되면서 낡은 에러를
    # 반복 출력하는 문제가 있어 명시적으로 제거한다. 실제 스코프
    # 식별자가 "adult"/"audiobook"이 아니라 core가 쓰는 다른 이름
    # (예: "media_adult")일 수도 있으므로, 이름을 짐작하지 않고
    # "gd_poller4bookoasis_"로 시작하는 잡 중 우리가 지금 쓰는
    # general 메인/워치 잡이 아닌 건 전부 제거한다.
    try:
        from services.scheduler_service import scheduler
        _keep_ids = {
            GdPoller4BookOasisProvider._job_id("general"),
            GdPoller4BookOasisProvider._watch_job_id("general"),
        }
        for _job in list(scheduler.get_jobs()):
            if _job.id.startswith("gd_poller4bookoasis_") and _job.id not in _keep_ids:
                try:
                    scheduler.remove_job(_job.id)
                except Exception:
                    pass
    except Exception:
        pass


_auto_register()
