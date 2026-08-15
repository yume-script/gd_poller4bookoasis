(function () {
  "use strict";

  // NOTE: 호스트가 현재 plugin_id / db_type을 전역변수로 주입해준다는
  // 보장이 없어서, cache_cleaner의 settings.js와 동일하게 합리적인
  // 기본값 + URL 쿼리 폴백으로 처리한다.
  var PLUGIN_ID = window.CURRENT_PLUGIN_ID || "gd_poller4bookoasis";
  var DB_TYPE =
    window.CURRENT_DB_TYPE ||
    new URLSearchParams(window.location.search).get("type") ||
    "general";

  var statusEl = document.getElementById("gp-status");
  var refreshBtn = document.getElementById("gp-refresh-btn");
  var runNowBtn = document.getElementById("gp-run-now-btn");
  var runNowResultEl = document.getElementById("gp-run-now-result");
  var clearLogBtn = document.getElementById("gp-clear-log-btn");
  var clearLogResultEl = document.getElementById("gp-clear-log-result");
  var saveBtn = document.getElementById("gp-save-btn");
  var saveResultEl = document.getElementById("gp-save-result");

  function statusItem(label, value, warn) {
    return (
      '<div class="gp-status-item' +
      (warn ? " gp-status-warn" : "") +
      '">' +
      '<span class="gp-status-label">' +
      label +
      "</span>" +
      '<span class="gp-status-value">' +
      value +
      "</span>" +
      "</div>"
    );
  }

  function formatBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  function renderStatus(data) {
    var html = "";
    html += statusItem(
      "스케줄러",
      data.scheduler_backend === "apscheduler"
        ? "코어 스케줄러(APScheduler)"
        : "자체 스레드(폴백)"
    );
    html += statusItem("다음 실행 예정", data.next_run);
    html += statusItem("로그 파일 크기", formatBytes(data.log_size_bytes));

    var targets = data.targets || [];
    if (targets.length === 0) {
      html += statusItem("감시 대상", "설정된 항목 없음", true);
    }
    targets.forEach(function (t) {
      var warn = !!t.parse_error;
      var value = warn
        ? "⚠ " + t.parse_error
        : t.last_mode +
          " / 변경 " +
          t.last_changes_found +
          "건 / 누적 " +
          t.run_count +
          "회 / " +
          t.last_run;
      html += statusItem(t.label, value, warn);
    });

    statusEl.innerHTML = html;
  }

  function loadStatus() {
    statusEl.innerHTML = '<div class="gp-status-loading">불러오는 중...</div>';
    // 실제 동작이 확인된 대시보드 위젯 데이터 엔드포인트를 재사용한다.
    fetch(
      "/api/media/dashboard/widgets/" +
        PLUGIN_ID +
        "/data?type=" +
        encodeURIComponent(DB_TYPE)
    )
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data && data.success) {
          renderStatus(data);
        } else {
          statusEl.innerHTML =
            '<div class="gp-status-item gp-status-warn">상태를 불러오지 못했습니다.</div>';
        }
      })
      .catch(function () {
        statusEl.innerHTML =
          '<div class="gp-status-item gp-status-warn">상태 조회 중 오류가 발생했습니다.</div>';
      });
  }

  function currentFormConfig() {
    var inputs = document.querySelectorAll(".gp-settings-field input");
    var config = {};
    inputs.forEach(function (input) {
      config[input.name] = input.value;
    });
    return config;
  }

  function saveConfigPayload(config, onDone) {
    fetch("/api/media/metadata/plugins/save-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plugin_id: PLUGIN_ID,
        type: DB_TYPE,
        config: config,
      }),
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        onDone(null, data);
      })
      .catch(function (err) {
        onDone(err, null);
      });
  }

  // NOTE: 백엔드에 "즉시 실행" 전용 액션 엔드포인트가 없어서(cache_cleaner
  // 개발 중 404로 확인됨), save-config에 토큰(RUN_NOW_TOKEN)을 실어
  // 저장하는 우회 방식을 쓴다. 플러그인 내부의 5초 주기 워치 잡이 토큰
  // 변경을 감지해서 실행하므로, 버튼을 눌러도 완전 즉시는 아니고 최대
  // 수 초 지연이 있다.
  function runNow() {
    runNowBtn.disabled = true;
    runNowResultEl.textContent = "실행 요청 중...";
    runNowResultEl.className = "gp-run-result";

    var config = currentFormConfig();
    var token = String(Date.now());
    config.RUN_NOW_TOKEN = token;

    saveConfigPayload(config, function (err, data) {
      if (err || !data || !data.success) {
        runNowBtn.disabled = false;
        runNowResultEl.textContent =
          "실행 요청 저장에 실패했습니다: " +
          (err ? err.message : (data && data.error) || "알 수 없는 오류");
        runNowResultEl.className = "gp-run-result gp-result-error";
        return;
      }
      runNowResultEl.textContent = "실행 대기 중... (최대 수 초 소요)";
      pollForToken("last_run_now_token", token, runNowBtn, runNowResultEl, "확인 완료");
    });
  }

  function clearLog() {
    clearLogBtn.disabled = true;
    clearLogResultEl.textContent = "요청 중...";
    clearLogResultEl.className = "gp-run-result";

    var config = currentFormConfig();
    var token = String(Date.now());
    config.LOG_CLEAR_TOKEN = token;

    saveConfigPayload(config, function (err, data) {
      if (err || !data || !data.success) {
        clearLogBtn.disabled = false;
        clearLogResultEl.textContent =
          "요청 저장에 실패했습니다: " +
          (err ? err.message : (data && data.error) || "알 수 없는 오류");
        clearLogResultEl.className = "gp-run-result gp-result-error";
        return;
      }
      clearLogResultEl.textContent = "처리 대기 중... (최대 수 초 소요)";
      pollForToken("last_log_clear_token", token, clearLogBtn, clearLogResultEl, "로그를 비웠습니다");
    });
  }

  function pollForToken(fieldName, token, btnEl, resultEl, doneMessage) {
    var MAX_ATTEMPTS = 12; // 12 * 2초 = 최대 24초 대기

    function attempt(n) {
      fetch(
        "/api/media/dashboard/widgets/" +
          PLUGIN_ID +
          "/data?type=" +
          encodeURIComponent(DB_TYPE)
      )
        .then(function (res) {
          return res.json();
        })
        .then(function (data) {
          if (data && data.success && data[fieldName] === token) {
            btnEl.disabled = false;
            resultEl.textContent = doneMessage;
            resultEl.className = "gp-run-result gp-result-ok";
            renderStatus(data);
            return;
          }
          if (n >= MAX_ATTEMPTS) {
            btnEl.disabled = false;
            resultEl.textContent =
              "응답이 지연되고 있습니다. 새로고침 버튼으로 다시 확인해주세요.";
            resultEl.className = "gp-run-result gp-result-error";
            return;
          }
          setTimeout(function () {
            attempt(n + 1);
          }, 2000);
        })
        .catch(function () {
          btnEl.disabled = false;
          resultEl.textContent = "상태 확인 중 오류가 발생했습니다.";
          resultEl.className = "gp-run-result gp-result-error";
        });
    }

    attempt(0);
  }

  function saveConfig() {
    var config = currentFormConfig();

    saveBtn.disabled = true;
    saveResultEl.textContent = "저장 중...";
    saveResultEl.className = "gp-save-result";

    saveConfigPayload(config, function (err, data) {
      saveBtn.disabled = false;
      if (!err && data && data.success) {
        saveResultEl.textContent = "저장되었습니다.";
        saveResultEl.className = "gp-save-result gp-result-ok";
        loadStatus();
      } else {
        saveResultEl.textContent =
          "저장 실패: " + (err ? err.message : (data && data.error) || "알 수 없는 오류");
        saveResultEl.className = "gp-save-result gp-result-error";
      }
    });
  }

  refreshBtn.addEventListener("click", loadStatus);
  runNowBtn.addEventListener("click", runNow);
  clearLogBtn.addEventListener("click", clearLog);
  saveBtn.addEventListener("click", saveConfig);

  loadStatus();
})();
