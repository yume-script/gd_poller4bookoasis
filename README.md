# gd_poller4bookoasis

BookOasis 전용 : 특정 구글 드라이브 폴더(하위 포함)의 변경사항을 주기적으로 감시하다가,
변경이 감지되면 rclone RC API(`/vfs/refresh`)를 호출해 해당 mount의 VFS 캐시를 refresh하고,
(선택) 디스코드 웹훅으로 변경 내역을 알려주는 백그라운드 플러그인입니다.

## 설치

1. 이 저장소 전체를 `plugins/metadata/gd_poller4bookoasis/` 경로로 복사
   ```
   plugins/metadata/gd_poller4bookoasis/
     __init__.py
     gd_poller4bookoasis.py
     VERSION
     README.md
   ```
2. BookOasis 서버 재시작
3. 환경설정 > 플러그인 설정에서 `gd_poller4bookoasis` 활성화
4. 아래 설정값 입력 후 저장

## 설정 항목

| 키 | 필수 | 설명 |
|---|---|---|
| `RCLONE_CONFIG_PATH` | 선택 | rclone.conf 경로 (기본값: `~/.config/rclone/rclone.conf`) |
| `REMOTE_NAME` | 필수 | rclone.conf 안의 remote 이름 (예: `gdrive`) |
| `DRIVE_FOLDER_ID` | 필수 | 감시할 구글 드라이브 폴더 ID |
| `RC_ADDR` | 필수 | rclone RC 주소 (예: `http://localhost:5572`) |
| `RC_USER` / `RC_PASS` | 선택 | rclone `--rc-user` / `--rc-pass`와 동일한 값 |
| `RC_FS` | 선택 | 여러 mount가 떠 있을 때만 지정 (예: `gdrive:`) |
| `DISCORD_WEBHOOK_URL` | 선택 | 비우면 알림 없이 refresh만 수행 |
| `POLL_INTERVAL_SECONDS` | 선택 | 폴링 주기(초), 기본 15 — `CRON_SCHEDULE`이 비어있을 때만 사용 |
| `CRON_SCHEDULE` | 선택 | crontab 표현식 (예: `*/1 * * * *`), 지정하면 주기(초) 대신 사용 |

## 사전 준비

- rclone mount가 `--rc` 옵션과 함께 떠 있어야 함
  ```
  rclone mount gdrive: /mnt/gdrive --vfs-cache-mode writes --rc --rc-addr localhost:5572
  ```
- rclone.conf에 해당 Google Drive remote가 이미 인증되어 있어야 함 (별도 구글 API 인증 불필요, rclone의 토큰을 재사용)
- 다음 파이썬 패키지가 BookOasis 실행 환경에 설치되어 있어야 함
  ```
  pip install google-auth google-api-python-client requests
  ```

## 동작 방식

- 최초 실행 시 `DRIVE_FOLDER_ID` 하위 전체를 한 번 인덱싱 (폴더/파일 ID 수집)
- 이후에는 Google Drive Changes API의 `page_token`만 이어서 사용, 변경분만 확인
- 변경된 항목의 부모가 감시 대상 폴더 트리에 속하는지 확인해서 범위 밖 변경은 무시
- 새로 생긴 하위 폴더도 감지 즉시 감시 대상에 편입
- 변경이 감지되면 `rclone rc vfs/refresh`(recursive) 호출 + (설정된 경우) 디스코드 알림
- 실행 로그: 플러그인 폴더 내 `gd_poller4bookoasis.log`
- 상태(마지막 실행 시각/결과/다음 실행 예정)는 플러그인 설정 화면에서 확인 (`get_status`)

## 참고

- 백그라운드 실행은 BookOasis 코어의 APScheduler(`services.scheduler_service.scheduler`)에 잡으로 등록되며,
  코어가 잡을 초기화하는 경우(`reload_all_jobs`)를 대비해 5분 주기 워치독 스레드가 재등록을 보장합니다.
- 코어 스케줄러 모듈을 불러올 수 없는 환경에서는 자체 스레드 루프로 자동 폴백합니다.
