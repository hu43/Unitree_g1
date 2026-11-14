/* Bridge: drive the original card UI from the G1 backend. */
(function () {
  var H = window.__robotCardHooks;
  if (!H) return;
  var real = window.ROBOT_CARD_REMOTE === true;
  if (!real) return;

  var root = document.getElementById('diy-robot-tool');
  var runBtn = document.getElementById('diy-run');
  var stopBtn = document.getElementById('diy-stop');
  var resetBtn = document.getElementById('diy-reset');
  var exampleBtn = document.getElementById('diy-example');
  var clearBtn = document.getElementById('diy-clear');
  var speedIn = document.getElementById('diy-speed');
  var rateField = document.getElementById('diy-rate');
  if (rateField) {
    var rf = rateField.closest('.field');
    if (rf) rf.style.display = 'none';
  }

  var badge = document.createElement('span');
  badge.className = 'viz-badge';
  badge.textContent = 'G1 真机';
  var hdr = root.querySelector('.hdr');
  if (hdr) hdr.insertBefore(badge, hdr.querySelector('.spacer'));

  var sub = root.querySelector('.hdr .sub');
  if (sub) sub.textContent = '拖拽积木拼接动作，点击运行直接下发到 G1 机器人';

  var style = document.createElement('style');
  style.textContent =
    '#diy-robot-tool .wfbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;}' +
    '#diy-robot-tool .wfbar .wf-label{display:inline-flex;align-items:center;gap:6px;color:var(--muted-foreground);}' +
    '#diy-robot-tool .wfbar .form-select{min-width:150px;width:auto;}' +
    '#diy-robot-tool .wfbar .chk{display:inline-flex;align-items:center;gap:6px;color:var(--muted-foreground);}' +
    '#diy-robot-tool .wfbar .voice-state{color:var(--muted-foreground);min-width:160px;}';
  document.head.appendChild(style);

  var wfbar = document.createElement('div');
  wfbar.className = 'wfbar';
  wfbar.innerHTML =
    '<label class="wf-label text-small">工作流' +
    '<select class="form-select" id="diy-wf-select"><option value="">（未保存）</option></select></label>' +
    '<button type="button" class="btn btn-ghost" id="diy-wf-save">保存工作流</button>' +
    '<button type="button" class="btn btn-ghost" id="diy-wf-del">删除</button>' +
    '<label class="chk text-small"><input type="checkbox" id="diy-voice-toggle" checked>语音启动</label>' +
    '<span class="voice-state text-small" id="diy-voice-state">语音待命</span>';
  var toolbar = root.querySelector('.toolbar');
  if (toolbar && toolbar.parentNode) toolbar.parentNode.insertBefore(wfbar, toolbar.nextSibling);

  var wfSelect = document.getElementById('diy-wf-select');
  var wfSave = document.getElementById('diy-wf-save');
  var wfDel = document.getElementById('diy-wf-del');
  var voiceToggle = document.getElementById('diy-voice-toggle');
  var voiceState = document.getElementById('diy-voice-state');

  var pollTimer = null;
  var posting = false;
  var resultUntil = 0;
  var lastSyncKey = '';
  var blockSeq = 0;

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    return fetch(path, opts).then(function (r) {
      return r.text().then(function (text) {
        var data;
        try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { ok: false, error: text || ('HTTP ' + r.status) }; }
        if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
        return data;
      });
    });
  }

  function clampSpeed() {
    var v = parseFloat(speedIn.value);
    if (!isFinite(v) || v <= 0) v = 0.5;
    v = Math.max(0.2, Math.min(0.6, v));
    if (Math.abs(v - parseFloat(speedIn.value)) > 1e-6) {
      speedIn.value = v;
      speedIn.dispatchEvent(new Event('input', { bubbles: true }));
      speedIn.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  clampSpeed();
  speedIn.setAttribute('min', '0.2');
  speedIn.setAttribute('max', '0.6');

  function enterRun() {
    root.classList.add('running');
    runBtn.disabled = true;
    stopBtn.disabled = false;
    resetBtn.disabled = true;
    exampleBtn.disabled = true;
    clearBtn.disabled = true;
  }

  function exitRun() {
    root.classList.remove('running');
    runBtn.disabled = false;
    stopBtn.disabled = true;
    resetBtn.disabled = false;
    exampleBtn.disabled = false;
    clearBtn.disabled = false;
  }

  function applyState(st) {
    if (st.pose) {
      H.robot.x = Number(st.pose.x) || 0;
      H.robot.y = Number(st.pose.y) || 0;
      H.robot.heading = Number(st.pose.heading) || 0;
      H.drawStage();
    }
    if (st.arm_mode) {
      H.arm.mode = st.arm_mode;
      H.drawPose();
    }
    if (st.speech) H.showSpeech(st.speech); else H.hideSpeech();
    H.setActive(typeof st.index === 'number' ? st.index : -1);
    H.setProgress(typeof st.progress === 'number' ? st.progress : 0);
    if (st.now) H.setNow(st.now);
  }

  function pollState() {
    api('/api/state').then(function (st) {
      applyState(st);
      if (!st.running) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        exitRun();
        H.setStatus(st.status || '已完成');
        if (st.error) {
          H.setNow('执行失败：' + st.error);
          resultUntil = Date.now() + 8000;
        } else {
          H.setNow(st.now || (st.status === '已停止' ? '已停止' : '流程执行完成'));
          resultUntil = Date.now() + 6000;
        }
        syncCurrent();
      }
    }).catch(function (e) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      exitRun();
      H.setStatus('连接中断');
      H.setNow('与机器人后端失联：' + (e.message || e));
    });
  }

  function startPolling() {
    if (pollTimer) return;
    enterRun();
    H.setStatus('运行中');
    pollState();
    pollTimer = setInterval(pollState, 300);
  }

  function realRun() {
    if (posting || pollTimer) return;
    var st = H.getState();
    if (!st.blocks.length) { H.setStatus('暂无指令'); return; }
    var ok = confirm('即将向真机下发 ' + st.blocks.length + ' 条指令。\n\n请确认：机器人周围 2 米内无人、无障碍；机器人已站稳；遥控器在手边可随时急停。\n\n确定开始执行？');
    if (!ok) return;
    posting = true;
    resultUntil = 0;
    H.resetStage();
    H.setProgress(0);
    H.setStatus('下发中…');
    H.setNow('正在校验机器人状态…');
    api('/api/run', {
      method: 'POST',
      body: JSON.stringify({
        confirm: true,
        speed: st.speed,
        blocks: st.blocks.map(function (b) { return { type: b.type, value: b.value }; })
      })
    }).then(function () {
      posting = false;
      startPolling();
    }).catch(function (e) {
      posting = false;
      exitRun();
      H.setStatus('无法运行');
      H.setNow(e.message || String(e));
      resultUntil = Date.now() + 8000;
    });
  }

  function realStop() {
    if (!pollTimer) return;
    H.setStatus('停止中…');
    api('/api/stop', { method: 'POST', body: '{}' }).catch(function (e) {
      H.setNow('停止请求失败：' + (e.message || e));
    });
  }

  /* -------- saved workflows -------- */
  function refreshWorkflows(preserve) {
    var cur = preserve ? wfSelect.value : '';
    return api('/api/workflows').then(function (res) {
      var html = '<option value="">（未保存）</option>';
      (res.items || []).forEach(function (it) {
        html += '<option value="' + it.name.replace(/"/g, '&quot;') + '">' +
          it.name + ' · ' + it.blocks + ' 条</option>';
      });
      wfSelect.innerHTML = html;
      if (cur) wfSelect.value = cur;
    }).catch(function () {});
  }

  function loadWorkflow(name) {
    if (!name) return;
    api('/api/workflows/get?name=' + encodeURIComponent(name)).then(function (res) {
      var wf = res.workflow || {};
      var st = H.getState();
      st.blocks = (wf.blocks || []).map(function (b) {
        return { id: 'w' + (++blockSeq) + '_' + Math.random().toString(36).slice(2, 6), type: b.type, value: b.value };
      });
      st.speed = Number(wf.speed) || 0.5;
      speedIn.value = st.speed;
      H.renderScript();
      H.updateRoute();
      H.resetStage();
      H.setStatus('已载入');
      H.setNow('工作流：' + name + '（' + st.blocks.length + ' 条）');
      resultUntil = Date.now() + 6000;
      syncCurrent();
    }).catch(function (e) {
      H.setStatus('载入失败');
      H.setNow(e.message || String(e));
      resultUntil = Date.now() + 6000;
    });
  }

  function saveCurrentWorkflow() {
    var st = H.getState();
    if (!st.blocks.length) { alert('没有可保存的指令'); return; }
    var name = prompt('工作流名称', wfSelect.value || '移动作业');
    if (!name) return;
    api('/api/workflows', {
      method: 'POST',
      body: JSON.stringify({
        name: name,
        speed: st.speed,
        blocks: st.blocks.map(function (b) { return { type: b.type, value: b.value }; })
      })
    }).then(function () {
      return refreshWorkflows();
    }).then(function () {
      wfSelect.value = name;
      syncCurrent();
      H.setStatus('已保存');
      H.setNow('工作流已保存：' + name);
      resultUntil = Date.now() + 6000;
    }).catch(function (e) {
      H.setStatus('保存失败');
      H.setNow(e.message || String(e));
      resultUntil = Date.now() + 6000;
    });
  }

  function deleteWorkflow() {
    var name = wfSelect.value;
    if (!name) { alert('请先选择一个已保存的工作流'); return; }
    if (!confirm('删除工作流「' + name + '」？')) return;
    api('/api/workflows?name=' + encodeURIComponent(name), { method: 'DELETE' }).then(function () {
      return refreshWorkflows();
    }).then(function () {
      H.setStatus('已删除');
      H.setNow('工作流已删除：' + name);
      resultUntil = Date.now() + 5000;
    }).catch(function (e) {
      H.setStatus('删除失败');
      H.setNow(e.message || String(e));
      resultUntil = Date.now() + 5000;
    });
  }

  function syncCurrent() {
    var st = H.getState();
    var payload = {
      blocks: st.blocks.map(function (b) { return { type: b.type, value: b.value }; }),
      speed: st.speed,
      name: wfSelect.value || ''
    };
    var key = JSON.stringify(payload);
    if (key === lastSyncKey) return;
    api('/api/current', { method: 'POST', body: key }).then(function () {
      lastSyncKey = key;
    }).catch(function () {
      lastSyncKey = '';
    });
  }

  /* -------- voice -------- */
  function pollVoice() {
    api('/api/voice').then(function (v) {
      voiceToggle.checked = !!v.enabled;
      var st = H.getState();
      if (!v.enabled) {
        voiceState.textContent = '语音已关闭';
      } else if (v.last_text && (Date.now() / 1000 - v.last_t) < 6) {
        voiceState.textContent = '听到：“' + v.last_text + '”';
      } else if (v.last_trigger_t) {
        var d = new Date(v.last_trigger_t * 1000);
        voiceState.textContent = '语音待命 · 上次启动 ' + ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
      } else {
        voiceState.textContent = '语音待命（唤醒模式后说“开始执行”）';
      }
      if (!v.enabled && st.blocks.length && !pollTimer) return;
    }).catch(function () {
      voiceState.textContent = '语音状态不可用';
    });
  }

  /* -------- status / wiring -------- */
  function pollStatus() {
    if (pollTimer) return;
    if (Date.now() < resultUntil) return;
    api('/api/status').then(function (s) {
      if (pollTimer) return;
      if (s.running) { startPolling(); return; }
      if (!s.ok) {
        H.setStatus('机器人异常');
        H.setNow('SDK 异常：' + (s.sdk_error || s.error || '未知'));
        return;
      }
      if (s.dry_run) {
        H.setStatus('试运行');
        H.setNow('DRY-RUN 模式 · 指令不会发送到机器人');
        return;
      }
      var ready = (s.fsm_id === 801 || s.fsm_id === 802) && s.fsm_mode === 0;
      H.setStatus(ready ? '真机就绪' : '未就绪');
      var bits = [];
      bits.push('FSM ' + (s.fsm_id == null ? '-' : s.fsm_id));
      bits.push('模式 ' + (s.fsm_mode === 0 ? '站立' : s.fsm_mode === 1 ? '动态' : (s.fsm_mode == null ? '-' : s.fsm_mode)));
      if (s.volume != null) bits.push('音量 ' + s.volume);
      bits.push('里程计 ' + (s.odom ? 'OK' : '无'));
      bits.push('ASR ' + (s.asr ? 'OK' : '无'));
      if (!ready) bits.push('请用遥控器切到走跑/站立模式');
      H.setNow(bits.join(' · '));
    }).catch(function () {
      if (pollTimer) return;
      H.setStatus('后端未连接');
      H.setNow('无法访问 /api/status');
    });
  }

  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('button') : null;
    if (!t) return;
    if (t.id === 'diy-run') {
      e.preventDefault(); e.stopPropagation(); realRun();
    } else if (t.id === 'diy-stop') {
      e.preventDefault(); e.stopPropagation(); realStop();
    } else if (t.id === 'diy-reset') {
      if (pollTimer) { e.preventDefault(); e.stopPropagation(); realStop(); return; }
      e.preventDefault(); e.stopPropagation();
      H.resetStage();
      H.setStatus('真机就绪');
      resultUntil = 0;
      pollStatus();
    } else if (t.id === 'diy-wf-save') {
      e.preventDefault(); e.stopPropagation(); saveCurrentWorkflow();
    } else if (t.id === 'diy-wf-del') {
      e.preventDefault(); e.stopPropagation(); deleteWorkflow();
    }
  }, true);

  wfSelect.addEventListener('change', function () { loadWorkflow(wfSelect.value); });
  voiceToggle.addEventListener('change', function () {
    api('/api/voice', { method: 'POST', body: JSON.stringify({ enabled: voiceToggle.checked }) })
      .then(function () { pollVoice(); })
      .catch(function () {});
  });

  window.addEventListener('beforeunload', function (e) {
    if (pollTimer) { e.preventDefault(); e.returnValue = ''; }
  });

  refreshWorkflows();
  pollVoice();
  setInterval(pollVoice, 2000);
  setInterval(syncCurrent, 1200);
  setInterval(pollStatus, 2500);
  syncCurrent();
  pollStatus();
})();
