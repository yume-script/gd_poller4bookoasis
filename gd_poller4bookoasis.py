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

## 변경 감지 방식: Drive Activity API v2 (halfaider/gd-poller 참고, 2024-xx 전환)
원래는 Drive Changes API(changes().list + page_token)로 변경을 감지하고,
타겟 폴더 하위 트리 전체를 직접 인덱싱(folder_ids/item_ids/item_meta)해서
그 안에서 일어난 변경인지 직접 판정했었다. halfaider/gd-poller
(gd_poller/pollers.py의 ActivityPoller, apis.py의 get_full_path 등)를
참고해서 Drive Activity API(driveactivity v2, activity().query)로
전환했다.

Changes API 방식과 비교했을 때:
  - 장점: activity().query(ancestorName=f"items/{folder_id}")로 폴더
    범위를 API 레벨에서 직접 지정할 수 있어서, 하위 트리 전체를 미리
    인덱싱해둘 필요가 없다 (folder_ids/item_ids/item_meta 전부 불필요).
    또한 create/edit/move/rename/delete/restore 같은 "액션 종류"를
    Changes API보다 훨씬 명확하게 구분해서 준다.
  - 단점: page_token처럼 서버가 이어서 알려주는 커서가 없어서, 타겟별로
    "마지막으로 조회한 시각(last_poll_time)"을 직접 상태 파일에
    저장해두고 시간 구간(time > start AND time <= end)으로 질의해야
    한다. 또한 항목의 전체 경로(폴더/하위폴더/파일명)를 Activity API
    응답이 주지 않으므로, 변경된 항목마다 Drive files.get으로 부모를
    거슬러 올라가며 조립해야 한다 (halfaider/gd-poller의 get_full_path와
    동일한 방식을 동기 버전으로 단순화해서 사용 - _activity_resolve_path).
  - 주의: Activity API는 rclone이 기본으로 요청하는 "drive" 스코프와는
    별개로 "drive.activity.readonly" 동의가 필요하다. rclone remote가
    이 스코프로 인증돼 있지 않으면 403(insufficient scopes)이 날 수
    있다 - 이 플러그인 코드로 해결할 수 있는 부분이 아니라 rclone
    쪽 재인증이 필요하다.

## 첫 실행 / 폴링 기준 시각(last_poll_time) 영속화
타겟별로 최초 1회는 폴더 트리를 훑는 대신, "지금부터 감시 시작"이라는
의미로 기준 시각(last_poll_time)만 상태 파일에 저장한다. 이후에는
매 폴링마다 [last_poll_time, now - ACTIVITY_POLL_DELAY_SECONDS] 구간을
Activity API에 질의하고, 성공하면 그 구간의 끝 시각으로 last_poll_time을
갱신한다. Activity API 응답이 서버에 즉시 반영되지 않고 약간 지연될 수
있어서 지금 시각을 그대로 쓰지 않고 ACTIVITY_POLL_DELAY_SECONDS만큼
여유를 둔다 (halfaider/gd-poller의 polling_delay와 동일한 목적).
이 상태는 타겟별 상태 파일(state_<remote_name>_<폴더ID>.json)에
저장해 프로세스/서버 재시작에도 이어진다. 상태 파일 키를 라벨이 아니라
remote_name+폴더ID 조합으로 잡는 이유는, 서로 다른 REMOTE_NAME을 쓰는
두 타겟이 같은 라벨을 쓰는 경우에도 상태가 안 섞이게 하기 위함이다.

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
        {"key": "ACTIVITY_POLL_DELAY_SECONDS",
         "label": "Activity API 반영 지연 여유 (초, halfaider/gd-poller의 polling_delay와 동일 목적)",
         "type": "text", "required": False, "default": "60"},
        {"key": "ACTIVITY_ACTIONS",
         "label": "감시할 액션 (쉼표 구분, 비우면 기본값 전부: create,edit,move,rename,delete,restore)",
         "type": "text", "required": False, "default": ""},
        {"key": "HEARTBEAT_EVERY_N_RUNS", "label": "하트비트 알림 주기 (N번 폴링마다 1번, 0=끔)",
         "type": "text", "required": False, "default": "0"},
        {"key": "WEBHOOK_BASE_URL", "label": "BookOasis 주소 (예: http://localhost:5930)",
         "type": "text", "required": False, "default": "http://localhost:5930"},
        {"key": "WEBHOOK_TOKEN", "label": "BookOasis WEBHOOK_TOKEN (.env와 동일한 값)",
         "type": "password", "required": False, "default": ""},
        {"key": "WEBHOOK_ADMIN_USERNAME", "label": "관리자 계정 (라이브러리 ID/타입 자동탐지용, 선택)",
         "type": "text", "required": False, "default": ""},
        {"key": "WEBHOOK_ADMIN_PASSWORD", "label": "관리자 비밀번호 (자동탐지용, 선택)",
         "type": "password", "required": False, "default": ""},
        {"key": "WEBHOOK_LIBRARY_ID", "label": "대상 라이브러리 ID (수동 지정, 비우면 자동탐지 사용)",
         "type": "text", "required": False, "default": ""},
        {"key": "WEBHOOK_DB_TYPE", "label": "라이브러리 DB 스코프 (수동 지정 시에만 사용)",
         "type": "text", "required": False, "default": "general"},
        {"key": "WEBHOOK_PATH_PREFIX",
         "label": "도서 위치 접두 경로 (구글드라이브 감시 루트에 대응하는 실제 마운트 절대경로)",
         "type": "text", "required": False, "default": "/mnt/gds2/GDRIVE/READING"},
    ]

    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/yume-script/gd_poller4bookoasis/refs/heads/main/",
        "files": [
            "gd_poller4bookoasis.py",
            "__init__.py",
            "VERSION",
            "index.html",
            "style.css",
            "script.js",
            "settings.html",
            "settings.js",
        ],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }

    # 대시보드 카드 렌더러가 "도서 카드" 틀에 고정돼 있어 안 맞으므로
    # 대시보드(플러그인 데스크 탭)에는 노출하지 않는다.
    dashboard_widget = None

    # 좌측 사이드바에 독립 카테고리 메뉴로 등록 (scan_scheduler, jikji_sf와
    # 동일한 방식). 이 계약이 있어야 index.html/style.css/script.js가 실제로
    # 로드되어 커스텀 풀페이지가 렌더링된다. 상태 확인 + 전체 설정 폼을
    # 여기(풀페이지)로 옮기고, settings.html(환경설정 탭 모달)은 짧은
    # 안내문만 남긴다 (모달 폭이 좁아 항목이 많으면 잘려 보이는 문제 때문).
    category_tab = {
        "title": "구글드라이브 감시",
        "icon": "fa-solid fa-cloud-arrow-down",
        "order": 96,
        "sessions": "all",
    }

    _watchdog_threads = {}
    _scheduler_lock = threading.Lock()
    # RUN_NOW_TOKEN으로 트리거되는 수동 실행을 5초 watch job과 분리해서
    # 돌리기 위한 스레드/락 (자세한 이유는 _check_tokens 참고)
    _manual_run_threads = {}
    _manual_run_lock = threading.Lock()

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
        line = f"[{ts}] [gd_poller4bookoasis] {text}"
        # 파일 로그(<STATE_DIR>/gd_poller4bookoasis.log)와 별개로, 도커
        # 컨테이너 로그(docker logs)에서도 바로 확인할 수 있도록 콘솔에도
        # 동일하게 출력한다. flush=True로 즉시 내보내야 버퍼링 때문에
        # 컨테이너 로그에 지연 없이 찍힌다.
        try:
            print(line, flush=True)
        except Exception:
            pass
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
            # check_all_targets()를 여기서 바로(동기) 돌리면, 그 안의
            # 구글 API 호출이 오래 걸리거나(네트워크 지연) 멈출 때
            # (방화벽 차단 등) 이 5초 주기 watch job 자체가 막혀버린다.
            # watch job은 max_instances=1이라, 한 번 막히면 이후 모든
            # 틱이 "maximum number of running instances reached"로
            # 영구히 스킵되는 사고로 이어진다(실사용 중 발견됨). 그래서
            # 실제 작업은 별도 스레드로 던지고 watch job 자체는 항상
            # 즉시 반환하게 한다. 이미 돌고 있는 수동 실행이 있으면
            # 새로 또 띄우지 않고 조용히 스킵한다.
            if self._start_manual_run(db_type):
                tstate["last_run_now_token"] = run_token
                tstate["last_run_now_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                dirty = True
            else:
                self._log_line("[전체] 즉시 실행 요청 스킵: 이전 수동 실행이 아직 진행 중")

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

    def _start_manual_run(self, db_type):
        """
        RUN_NOW_TOKEN 감지로 트리거된 check_all_targets()를 별도 스레드에서
        실행한다. 이미 같은 db_type에 대해 실행 중인 스레드가 있으면(직전
        수동 실행이 아직 안 끝났으면) 새로 띄우지 않고 False를 돌려준다.
        """
        with GdPoller4BookOasisProvider._manual_run_lock:
            existing = GdPoller4BookOasisProvider._manual_run_threads.get(db_type)
            if existing and existing.is_alive():
                return False
            t = threading.Thread(
                target=self._manual_run_worker,
                args=(db_type,),
                daemon=True,
                name=f"gd_poller4bookoasis_manual_run_{db_type}",
            )
            GdPoller4BookOasisProvider._manual_run_threads[db_type] = t
            t.start()
            return True

    def _manual_run_worker(self, db_type):
        try:
            self.check_all_targets(db_type)
        except Exception as e:
            self._log_line(f"[전체] 즉시 실행 중 오류: {e}")

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
            # drive: 경로 조립용 files.get 호출에 필요
            # drive.activity.readonly: Activity API 질의에 필요
            # 주의: 여기 적는 scopes는 클라이언트 라이브러리에 "이 정도는
            # 필요하다"고 알려주는 값일 뿐, 실제 권한은 rclone이 최초
            # 인증할 때 사용자가 동의한 스코프로 이미 고정돼 있다.
            # rclone remote가 drive.activity.readonly로 동의된 적이
            # 없다면 Activity API 호출은 403(insufficient scopes)이 날
            # 수 있다 - 그 경우 rclone 쪽 재인증이 필요하다.
            scopes=[
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.activity.readonly",
            ],
        )

    def _build_google_clients(self, creds, timeout=30):
        """
        Drive/Activity API 클라이언트를 명시적 소켓 타임아웃과 함께 생성한다.

        build(..., credentials=creds)만 쓰면 내부 HTTP 전송 계층에
        타임아웃이 전혀 걸리지 않는다. 이 상태에서 네트워크가 멈추면
        (방화벽 차단, DNS 지연, 순간적인 구글 API 응답 지연 등)
        execute() 호출이 영원히 반환하지 않을 수 있다. 이게 실제로
        RUN_NOW_TOKEN 감지용 5초 주기 watch job 안에서 터지면, 그 job은
        max_instances=1이라 이후 모든 틱이 영구히 "maximum number of
        running instances reached"로 스킵되는 사고로 이어진다(실사용
        중 발견됨). httplib2.Http(timeout=...)로 감싼 AuthorizedHttp를
        명시적으로 넘겨서 이 문제를 막는다.

        AuthorizedHttp/Http 인스턴스는 스레드 안전하지 않으므로 build()
        호출마다 새로 만들어서 쓴다 (여러 build를 위해 하나를 재사용하지
        않음).
        """
        from httplib2 import Http
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build

        drive = build(
            "drive", "v3",
            http=AuthorizedHttp(creds, http=Http(timeout=timeout)),
        )
        activity = build(
            "driveactivity", "v2",
            http=AuthorizedHttp(creds, http=Http(timeout=timeout)),
        )
        return drive, activity

    # ------------------------------------------------------------------
    # Drive Activity API v2 헬퍼 (halfaider/gd-poller의
    # ActivityPoller.get_action_info / get_target_info / apis.get_full_path를
    # 동기 버전으로 단순화해서 이식)
    # ------------------------------------------------------------------
    @staticmethod
    def _activity_action_info(action_detail):
        """
        activity["primaryActionDetail"] 딕셔너리에서 (action, action_detail)을
        뽑아낸다. action은 "create"/"edit"/"move"/"rename"/"delete"/
        "restore"/"permissionChange" 등 Activity API가 주는 키 이름 그대로.
        action_detail은 action 종류에 따라 부가 정보(예: rename이면 이전
        제목, delete/restore면 TRASH/PERMANENT_DELETE 등)를 담는다.
        """
        for key in action_detail:
            detail = action_detail[key] or {}
            if key == "move":
                removed_parents = detail.get("removedParents") or [{}]
                action_extra = GdPoller4BookOasisProvider._activity_target_info(removed_parents[0])
            elif key == "rename":
                action_extra = detail.get("oldTitle")
            elif key in ("delete", "restore"):
                action_extra = detail.get("type")
            elif key == "permissionChange":
                action_extra = detail.get("addedPermissions")
            else:
                action_extra = None
            return key, action_extra
        return "unknown", None

    @staticmethod
    def _activity_target_info(target):
        """
        activity["targets"][i] (또는 removedParents[0]) 하나에서
        (title, item_id, mimeType)을 뽑아낸다. driveItem/drive 형태가
        아니면 item_id가 빈 문자열로 온다 (호출부에서 스킵 처리).
        """
        info = target.get("driveItem") or target.get("drive")
        if not info:
            return "unknown", "", ""
        title = info.get("title") or "unknown"
        # name은 "items/<id>" 형태로 오므로 마지막 조각만 취한다
        item_id = (info.get("name") or "").rpartition("/")[-1]
        mime = info.get("mimeType") or ""
        return title, item_id, mime

    @staticmethod
    def _activity_watch_actions(cfg):
        """ACTIVITY_ACTIONS 설정값을 파싱. 비어있으면 기본 액션 전체."""
        default_actions = {"create", "edit", "move", "rename", "delete", "restore"}
        raw = (cfg.get("ACTIVITY_ACTIONS") or "").strip()
        if not raw:
            return default_actions
        return {a.strip() for a in raw.split(",") if a.strip()}

    def _activity_resolve_path(self, drive, item_id, ancestor_id, max_depth=100):
        """
        item_id부터 ancestor_id(감시 루트 폴더)까지 files.get으로 부모를
        거슬러 올라가며 "상위폴더/하위폴더/파일명" 형태 경로를 조립한다.
        halfaider/gd-poller의 apis.GoogleDrive.get_full_path를 동기 버전
        으로 단순화한 것 - 트리를 미리 인덱싱해두지 않으므로 매 변경
        이벤트마다 API를 호출한다 (그 대신 폴더 전체를 훑을 필요는
        없어짐). 대상이 이미 영구 삭제되었거나 권한이 없어 조회가
        실패하면 None을 돌려주고, 호출부는 title 등으로 대체한다.
        """
        parts = []
        current_id = item_id
        depth = 0
        try:
            while current_id and depth < max_depth:
                depth += 1
                if current_id == ancestor_id:
                    break
                file = drive.files().get(
                    fileId=current_id,
                    fields="id,name,parents",
                    supportsAllDrives=True,
                ).execute()
                parts.append(file.get("name") or current_id)
                parents = file.get("parents") or []
                current_id = parents[0] if parents else None
            parts.reverse()
            return "/".join(parts) if parts else None
        except Exception:
            return None

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

    _library_map_cache = {}  # base_url -> {"fetched_at": float, "entries": [...]}
    _library_map_lock = threading.Lock()

    def _fetch_library_map(self, base_url, username, password):
        """
        관리자 세션으로 로그인해서 general/adult 스코프의 라이브러리
        목록(id, type, physical_path 루트들)을 가져온다.
        /api/media/libraries/schedules는 세션 쿠키(@admin_required) 인증이라
        WEBHOOK_TOKEN과는 별개로 관리자 계정이 필요하다.
        """
        import requests

        session = requests.Session()
        resp = session.post(
            f"{base_url}/login",
            data={"username": username, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        login_data = resp.json()
        if not login_data.get("success"):
            raise RuntimeError(f"관리자 로그인 실패: {login_data.get('error', '알 수 없는 오류')}")

        entries = []
        for scope in ("general", "adult"):
            resp = session.get(
                f"{base_url}/api/media/libraries/schedules",
                params={"type": scope},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for lib in data.get("libraries", []):
                roots = [r.strip() for r in (lib.get("physical_path") or "").splitlines() if r.strip()]
                entries.append({
                    "library_id": lib.get("id"),
                    "type": scope,
                    "roots": roots,
                })
        return entries

    def _get_library_map(self, db_type, base_url):
        """10분 캐시로 라이브러리 맵을 재사용 (매 폴링마다 로그인하지 않도록)"""
        cfg = self.get_plugin_config(db_type, default={})
        username = cfg.get("WEBHOOK_ADMIN_USERNAME") or ""
        password = cfg.get("WEBHOOK_ADMIN_PASSWORD") or ""
        if not username or not password:
            return None

        with GdPoller4BookOasisProvider._library_map_lock:
            cached = GdPoller4BookOasisProvider._library_map_cache.get(base_url)
            if cached and (time.time() - cached["fetched_at"]) < 600:
                return cached["entries"]

        try:
            entries = self._fetch_library_map(base_url, username, password)
        except Exception as e:
            self._log_line(f"[전체] 라이브러리 자동탐지 실패: {e}")
            return None

        with GdPoller4BookOasisProvider._library_map_lock:
            GdPoller4BookOasisProvider._library_map_cache[base_url] = {
                "fetched_at": time.time(),
                "entries": entries,
            }
        return entries

    @staticmethod
    def _match_library(full_path, entries):
        """full_path(절대경로)와 가장 길게 일치하는 라이브러리 루트를 찾아
        (library_id, type, 그 라이브러리 기준 상대경로)를 반환. 없으면 None."""
        best = None  # (root_len, library_id, type, relative_path)
        for entry in entries:
            for root in entry["roots"]:
                root_norm = root.rstrip("/")
                if full_path == root_norm:
                    relative = ""
                elif full_path.startswith(root_norm + "/"):
                    relative = full_path[len(root_norm) + 1:]
                else:
                    continue
                if best is None or len(root_norm) > best[0]:
                    best = (len(root_norm), entry["library_id"], entry["type"], relative)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def _notify_bookoasis_scan(self, db_type, changed_paths):
        """
        신규/변경/삭제된 항목들의 상위 폴더를 모아 BookOasis의
        /api/webhook/scan (path 지정 시 폴더 단위 즉시 동기 스캔)을
        호출한다.

        라이브러리 ID/타입 결정 순서:
          1) WEBHOOK_LIBRARY_ID를 수동으로 설정해뒀으면 그 값을 그대로 사용
          2) 아니면 WEBHOOK_ADMIN_USERNAME/PASSWORD로 관리자 세션 로그인 후
             /api/media/libraries/schedules에서 physical_path 목록을 가져와,
             변경된 파일의 절대경로와 가장 길게 일치하는 라이브러리를 자동
             탐지 (이 경우 path도 그 라이브러리 기준 상대경로로 정확히 계산됨)
          3) 어느 쪽도 안 되면 조용히 스킵 (선택 기능)

        주의: path는 반드시 "폴더" 경로여야 한다 (파일 경로를 넘기면
        os.walk()가 아무것도 못 찾아 조용히 무동작한다). 그래서 바뀐 항목이
        파일이든 폴더든 항상 그 "상위 폴더" 경로를 쓴다. 같은 폴더에 여러
        변경이 몰리면 한 번만 호출한다 (중복 제거).
        """
        import requests

        cfg = self.get_plugin_config(db_type, default={})
        base_url = (cfg.get("WEBHOOK_BASE_URL") or "").rstrip("/")
        token = cfg.get("WEBHOOK_TOKEN") or ""
        prefix = (cfg.get("WEBHOOK_PATH_PREFIX") or "").rstrip("/")
        manual_library_id = cfg.get("WEBHOOK_LIBRARY_ID") or ""
        manual_db_type = cfg.get("WEBHOOK_DB_TYPE") or "general"

        if not base_url or not token:
            return  # 웹훅 설정이 비어있으면 조용히 스킵 (선택 기능)

        library_map = None if manual_library_id else self._get_library_map(db_type, base_url)
        if not manual_library_id and not library_map:
            return  # 수동 지정도 없고 자동탐지도 안 되면 스킵

        # 각 변경 항목의 "상위 폴더" 절대경로만 뽑아서 중복 제거
        full_parent_paths = set()
        for item in changed_paths:
            path = item.get("path") or ""
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            full_path = f"{prefix}/{parent}".rstrip("/") if parent else prefix
            full_parent_paths.add(full_path)

        for full_path in full_parent_paths:
            if manual_library_id:
                library_id, webhook_db_type, relative_path = manual_library_id, manual_db_type, full_path
            else:
                matched = self._match_library(full_path, library_map)
                if not matched:
                    self._log_line(f"[전체] BookOasis scan-path 스킵 (일치하는 라이브러리 없음): {full_path}")
                    continue
                library_id, webhook_db_type, relative_path = matched

            try:
                resp = requests.get(
                    f"{base_url}/api/webhook/scan",
                    params={
                        "token": token,
                        "library_id": library_id,
                        "type": webhook_db_type,
                        "path": relative_path,
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                self._log_line(
                    f"[전체] BookOasis scan-path 요청: lib={library_id}({webhook_db_type}) "
                    f"path={relative_path} -> {resp.json()}"
                )
            except requests.RequestException as e:
                self._log_line(f"[전체] BookOasis scan-path 요청 실패({full_path}): {e}")

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
        import datetime

        if target.get("parse_error"):
            return {"mode": "error", "error": target["parse_error"], "changes_found": 0}

        remote_name = target["remote_name"]
        folder_id = target["folder_id"]
        state_key = self._state_key(target)
        display_label = self._display_label(target)

        creds = self._load_credentials(db_type, remote_name)
        drive, activity = self._build_google_clients(creds)

        cfg = self.get_plugin_config(db_type, default={})
        try:
            poll_delay_sec = int(cfg.get("ACTIVITY_POLL_DELAY_SECONDS") or 60)
        except (TypeError, ValueError):
            poll_delay_sec = 60

        now = datetime.datetime.now(datetime.timezone.utc)
        end_time = now - datetime.timedelta(seconds=max(0, poll_delay_sec))

        state = self._read_state(state_key) or {}

        # 최초 실행 시에는 트리를 훑지 않고 "지금부터 감시 시작" 기준
        # 시각만 기록한다 (Activity API는 ancestorName으로 범위를 API
        # 레벨에서 잡아주므로 사전 인덱싱이 필요 없다).
        if "last_poll_time" not in state or state.get("indexed_folder_id") != folder_id:
            state = {
                "indexed_folder_id": folder_id,
                "last_poll_time": end_time.isoformat(),
            }
            self._write_state(state_key, state)
            return {"mode": "indexed", "changes_found": 0,
                    "note": "Drive Activity API 감시 시작 (초기 기준 시각 설정 완료)"}

        try:
            start_time = datetime.datetime.fromisoformat(state["last_poll_time"])
        except (TypeError, ValueError):
            start_time = end_time
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=datetime.timezone.utc)

        if start_time >= end_time:
            # ACTIVITY_POLL_DELAY_SECONDS가 폴링 주기보다 길면 아직 조회할
            # 새 구간이 없을 수 있다. 상태는 그대로 두고 다음 틱을 기다린다.
            return {"mode": "ok", "changes_found": 0, "changes": [], "changed_paths": []}

        watch_actions = self._activity_watch_actions(cfg)

        # ------------------------------------------------------------
        # 1단계: [start_time, end_time] 구간의 활동을 전부 모은다.
        # halfaider/gd-poller의 ActivityPoller._poll과 동일한 필터 형식
        # (time > ... AND time <= ...)을 사용하되, 여기서는 동기적으로
        # 한 타겟씩 페이지네이션까지 다 끝낸 뒤 다음 타겟으로 넘어간다.
        # ------------------------------------------------------------
        raw_activities = []
        page_token = None
        while True:
            body = {
                "pageSize": 100,
                "ancestorName": f"items/{folder_id}",
                "filter": (
                    f"time > {int(start_time.timestamp() * 1000)} "
                    f"AND time <= {int(end_time.timestamp() * 1000)}"
                ),
            }
            if page_token:
                body["pageToken"] = page_token
            response = activity.activity().query(body=body).execute()
            raw_activities.extend(response.get("activities", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        # ------------------------------------------------------------
        # 2단계: 액션/대상 파싱 + 경로 조립 + 최종 판정
        #
        # Activity API는 Changes API와 달리 "이 액티비티가 어느 폴더
        # 하위에서 일어났는지"를 ancestorName 질의 자체가 보장해주므로,
        # 폴더 트리를 미리 인덱싱해둘 필요가 없다. 그 대신 응답이 전체
        # 경로를 안 주기 때문에, 대상 하나마다 _activity_resolve_path로
        # Drive files.get을 거슬러 올라가며 조립해야 한다 (건수가 많지
        # 않은 변경 감지 용도라 매 이벤트 API 호출 비용은 감수한다).
        # ------------------------------------------------------------
        change_lines = []
        changed_paths = []  # 가공용 원본 데이터: [{"path": ..., "removed": bool, "action": ...}, ...]
        seen = set()  # (item_id, action) 중복 제거 - 한 구간에 같은 대상이 여러 번 찍힐 수 있음
        icons = {
            "create": "➕", "edit": "✏️", "move": "🚚", "rename": "📝",
            "delete": "🗑️", "restore": "♻️", "permissionChange": "🔑",
        }

        for act in raw_activities:
            primary = act.get("primaryActionDetail") or {}
            if not primary:
                continue
            action, action_extra = self._activity_action_info(primary)
            if watch_actions and action not in watch_actions:
                continue

            targets = act.get("targets") or []
            title, item_id, mime = ("unknown", "", "")
            for t in targets:
                title, item_id, mime = self._activity_target_info(t)
                if item_id:
                    break
            if not item_id:
                continue

            dedup_key = (item_id, action)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            if action == "delete" and action_extra != "TRASH":
                # 영구 삭제된 대상은 files.get으로 더 이상 조회가 안 되므로
                # 전체 경로 대신 활동 로그에 남은 제목만 사용한다.
                full_path = title
            else:
                full_path = self._activity_resolve_path(drive, item_id, folder_id) or title

            removed = action == "delete"
            icon = icons.get(action, "🔔")
            change_lines.append(f"{icon} {action}: {full_path}")
            changed_paths.append({"path": full_path, "removed": removed, "action": action})

        # 상태 갱신: 다음 폴링은 이번에 조회한 구간의 끝 시각부터 이어간다.
        state["last_poll_time"] = end_time.isoformat()
        run_count = int(state.get("run_count", 0)) + 1
        state["run_count"] = run_count
        self._write_state(state_key, state)

        if change_lines:
            self._call_rclone_rc_refresh(db_type)
            self._notify_discord(db_type, display_label, change_lines)
            self._notify_bookoasis_scan(db_type, changed_paths)
        else:
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
    def _config_with_defaults(self, cfg):
        """config_schema 기본값으로 채운 현재 설정값.

        index.html(풀페이지)이 별도의 "설정 조회" 엔드포인트 없이도 이미
        쓰고 있는 대시보드 데이터 엔드포인트만으로 입력 필드를 채울 수
        있도록 get_status() 응답에 포함시킨다 (기존 settings.html/js는
        host가 모달을 열 때 name 속성 기준으로 값을 미리 채워줬지만,
        풀페이지는 그 자동 채움 대상이 아니라서 직접 넣어줘야 한다).
        """
        cfg = cfg or {}
        result = {}
        for field in self.config_schema:
            key = field["key"]
            result[key] = cfg.get(key, field.get("default", ""))
        return result

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
            "config": self._config_with_defaults(cfg),
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
