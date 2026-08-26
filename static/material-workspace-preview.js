// Material Workspace V2：接入现有 PDF 预处理、按模块提取、导出与知识库归档。
(() => {
  const PARTS = {
    part1: { label: "摘要与研究结论", exportTitle: "一、摘要与研究结论" },
    part2: { label: "材料与性能信息", exportTitle: "二、材料与性能信息" },
    part3: { label: "综合结论与未来建议", exportTitle: "三、综合结论与未来建议" },
  };
  const PART_ORDER = ["part2", "part1", "part3"];

  function initialize() {
    const root = document.getElementById("material-workspace-v2");
    const bridge = window.materialWorkspaceBridge;
    if (!root || !bridge) {
      setTimeout(initialize, 50);
      return;
    }

    const el = {
      filesBody: root.querySelector("#mw-files-body"),
      filesEmpty: root.querySelector("#mw-files-empty"), addFile: root.querySelector("#mw-add-file"),
      addFileInput: root.querySelector("#mw-add-file-input"),
      start: root.querySelector("#mw-start"), model: root.querySelector("#mw-model"),
      headerStatus: root.querySelector("#mw-header-status-text"), resultsCount: root.querySelector("#mw-results-count"),
      resultList: root.querySelector("#mw-result-list"), detailEmpty: root.querySelector("#mw-detail-empty"),
      detailContent: root.querySelector("#mw-detail-content"), detailName: root.querySelector("#mw-detail-name"),
      tabRow: root.querySelector("#mw-tab-row"),
      report: root.querySelector("#mw-report"), exportBtn: root.querySelector("#mw-export"),
      archiveBtn: root.querySelector("#mw-archive"),
    };

    // 选择状态必须以“文件 + 部分”为粒度保存，不能再用全局 parts 套用到所有文件。
    const selectedDocuments = new Set();
    const selectedPartsByDocument = new Map(); // documentId -> Set(part)
    const pendingAutoSelect = new Set();
    const documentResults = new Map();
    const modelLabels = {};
    let activeDocumentId = "";
    let activePart = "part2";
    const activeTasks = new Map();
    const launchingTargets = new Set(); // 请求已发出、尚未拿到 task_id 的 documentId::part
    let startPending = false;
    let lastTaskMessage = "暂无提取任务";
    let lastFileSignature = "";

    const files = () => bridge.getFiles() || [];
    const fileById = (id) => files().find((item) => item.id === id) || null;
    const icon = (name) => `<i data-lucide="${name}" aria-hidden="true"></i>`;

    function refreshIcons() {
      if (window.lucide) window.lucide.createIcons({ attrs: { width: 16, height: 16 } });
    }

    function escapeHtml(value) {
      return String(value == null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
    }

    function formatDuration(seconds) {
      const total = Math.max(0, Math.round(Number(seconds || 0)));
      const minutes = Math.floor(total / 60);
      const secs = total % 60;
      return `${String(minutes).padStart(2, "0")}m${String(secs).padStart(2, "0")}s`;
    }

    function formatTime(value) {
      if (!value) return "";
      const text = String(value);
      const match = text.match(/(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})/);
      return match ? `${match[1]} ${match[2]}` : text;
    }

    function modelLabel(state) {
      if (!state) return "";
      return state.model_label || modelLabels[state.model] || state.model || "";
    }

    function targetKey(documentId, part) {
      return `${documentId}::${part}`;
    }

    function partsForDocument(documentId) {
      let parts = selectedPartsByDocument.get(documentId);
      if (!parts) {
        parts = new Set();
        selectedPartsByDocument.set(documentId, parts);
      }
      return parts;
    }

    function isTargetBusy(documentId, part) {
      const key = targetKey(documentId, part);
      return launchingTargets.has(key) || [...activeTasks.values()].some((task) =>
        (task.targets || []).some((target) => target.documentId === documentId && target.part === part)
      );
    }

    function requestedTargets() {
      return [...selectedDocuments].map(fileById).filter((file) => file && file.ready).flatMap((file) =>
        [...partsForDocument(file.id)].map((part) => ({ documentId: file.id, part }))
      );
    }

    function runnableTargets() {
      return requestedTargets().filter((target) => !isTargetBusy(target.documentId, target.part));
    }

    function ensureDocumentResult(documentId, name = "") {
      let result = documentResults.get(documentId);
      if (!result) {
        result = {
          id: documentId, name: name || "未命名论文",
          parts: {
            part1: { effective: null, latestAttempt: null },
            part2: { effective: null, latestAttempt: null },
            part3: { effective: null, latestAttempt: null },
          },
          runs: new Map(), updatedAt: 0,
        };
        documentResults.set(documentId, result);
      }
      if (name) result.name = name;
      return result;
    }

    function mergePaperState(paper, run) {
      if (!paper) return;
      const documentId = String(paper.document_id || paper.id || "");
      if (!documentId) return;
      const result = ensureDocumentResult(documentId, paper.name);
      const runId = String(paper.run_id || (run && run.runId) || "");
      result.runs.set(runId, {
        runId, provider: (run && run.provider) || paper.model || "",
        parts: (run && run.parts) || [], status: paper.status || "running", updatedAt: Date.now(),
      });
      Object.keys(PARTS).forEach((part) => {
        const incoming = paper.parts && paper.parts[part];
        if (!incoming || incoming.status === "not_selected") return;
        const normalized = {
          ...incoming, run_id: incoming.run_id || runId,
          model: incoming.model || paper.model || (run && run.provider) || "",
          model_label: incoming.model_label || paper.model_label || "",
        };
        result.parts[part].latestAttempt = normalized;
        if (normalized.status === "completed" && normalized.content) {
          result.parts[part].effective = { ...normalized };
        }
      });
      result.updatedAt = Date.now();
    }

    function completedCount(result) {
      if (!result) return 0;
      return Object.keys(PARTS).filter((part) => result.parts[part].effective).length;
    }

    function latestAttemptFailed(result) {
      if (!result) return false;
      return Object.keys(PARTS).some((part) => {
        const attempt = result.parts[part].latestAttempt;
        return attempt && attempt.status === "failed";
      });
    }

    function documentOverallStatus(documentId) {
      const file = fileById(documentId);
      const result = documentResults.get(documentId);
      const count = completedCount(result);
      const busy = [...activeTasks.values()].some((task) =>
        (task.targets || []).some((target) => target.documentId === documentId)
      ) || [...launchingTargets].some((key) => key.startsWith(`${documentId}::`));
      if (busy) {
        return { text: `提取中 · ${count} / 3 已完成`, className: "is-good" };
      }
      if (count > 0) return { text: `${count} / 3 已完成`, className: "is-good" };
      if (latestAttemptFailed(result)) return { text: "处理失败", className: "is-error" };
      if (file && file.prepareStatus === "failed") return { text: "解析失败", className: "is-error" };
      if (file && ["queued", "processing", "cancelling"].includes(file.prepareStatus)) return { text: "解析中", className: "" };
      if (file && file.prepareStatus === "cancelled") return { text: "已取消", className: "" };
      return { text: "未开始", className: "" };
    }

    function allResultDocuments() {
      const list = [];
      const seen = new Set();
      files().forEach((file) => { seen.add(file.id); list.push({ id: file.id, name: file.name }); });
      documentResults.forEach((result, id) => { if (!seen.has(id)) list.push({ id, name: result.name }); });
      return list;
    }

    function parseStatusMarkup(file) {
      const status = file.prepareStatus || (file.ready ? "completed" : "processing");
      const elapsed = formatDuration(file.prepareElapsed || 0);
      if (file.ready || status === "completed") return `<span class="mw-parse is-complete">${icon("check")}已完成 · ${elapsed}</span>`;
      if (status === "failed") return `<span class="mw-parse is-failed" title="${escapeHtml(file.prepareError || "解析失败")}">${icon("circle-alert")}解析失败 · ${elapsed}</span>`;
      if (status === "cancelled") return `<span class="mw-parse is-cancelled">${icon("circle-stop")}已取消 · ${elapsed}</span>`;
      const percent = Math.max(0, Math.min(100, Math.round(Number(file.preparePercent || 0))));
      return `<span class="mw-progress"><span class="mw-progress-track" role="progressbar" aria-label="${escapeHtml(file.name)} PDF 解析进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span class="mw-progress-fill" style="width:${percent}%"></span></span><span>${percent}%</span></span>`;
    }

    function renderFiles() {
      const currentFiles = files();
      const validIds = new Set(currentFiles.map((file) => file.id));
      [...selectedDocuments].forEach((id) => { if (!validIds.has(id)) selectedDocuments.delete(id); });
      [...selectedPartsByDocument.keys()].forEach((id) => { if (!validIds.has(id)) selectedPartsByDocument.delete(id); });
      el.filesBody.innerHTML = "";
      el.filesEmpty.classList.toggle("is-hidden", currentFiles.length > 0);
      const addFileLabel = el.addFile && el.addFile.querySelector("span");
      if (addFileLabel) addFileLabel.textContent = currentFiles.length ? "继续添加 PDF" : "添加 PDF";
      currentFiles.forEach((file) => {
        if (!file.ready) selectedDocuments.delete(file.id);
        if (file.ready && pendingAutoSelect.has(file.id)) {
          selectedDocuments.add(file.id);
          pendingAutoSelect.delete(file.id);
        }
        if (["failed", "cancelled"].includes(file.prepareStatus)) pendingAutoSelect.delete(file.id);
        const row = document.createElement("tr");
        const selectable = Boolean(file.ready);
        const checked = selectedDocuments.has(file.id);
        const selectedParts = partsForDocument(file.id);
        const partButtons = PART_ORDER.map((part) => {
          const isSelected = selectedParts.has(part);
          const busy = isTargetBusy(file.id, part);
          const state = documentResults.get(file.id)?.parts?.[part];
          const label = PARTS[part].label;
          return `<button class="mw-file-part-option${isSelected ? " is-selected" : ""}${busy ? " is-running" : ""}" type="button" data-file-part="${part}" aria-pressed="${isSelected}" ${selectable && !busy ? "" : "disabled"} title="${escapeHtml(label)}">${escapeHtml(label)}${busy ? "（提取中）" : ""}</button>`;
        }).join("");
        row.innerHTML = `
          <td><input class="mw-check mw-file-select" type="checkbox" ${checked ? "checked" : ""} ${selectable ? "" : "disabled"} aria-label="选择 ${escapeHtml(file.name)}"></td>
          <td><div class="mw-file-name-wrap">${icon("file-text")}<span class="mw-file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span></div></td>
          <td><div class="mw-file-part-options" aria-label="${escapeHtml(file.name)} 的提取部分">${partButtons}</div></td>
          <td>${parseStatusMarkup(file)}</td><td><div class="mw-actions"></div></td>`;
        const checkbox = row.querySelector(".mw-file-select");
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) selectedDocuments.add(file.id); else selectedDocuments.delete(file.id);
          renderAll();
        });
        row.querySelectorAll("[data-file-part]").forEach((button) => {
          button.addEventListener("click", () => {
            const part = button.dataset.filePart;
            const parts = partsForDocument(file.id);
            if (parts.has(part)) parts.delete(part); else parts.add(part);
            // 选择某个文件的部分即明确将它纳入本轮任务；不会影响其它文件。
            selectedDocuments.add(file.id);
            renderAll();
          });
        });
        const actions = row.querySelector(".mw-actions");
        if (!file.ready && ["queued", "processing", "cancelling"].includes(file.prepareStatus || "processing")) {
          const cancel = document.createElement("button");
          cancel.type = "button"; cancel.className = "mw-action-button mw-primary";
          cancel.innerHTML = `${icon("circle-stop")}<span>取消解析</span>`;
          cancel.addEventListener("click", async () => {
            if (!file.prepareTaskId) return;
            file.prepareStatus = "cancelling"; file.prepareStage = "正在取消…"; renderFiles();
            await fetch("/api/stop_paper_prepare/" + file.prepareTaskId, { method: "POST" }).catch(() => {});
          });
          actions.appendChild(cancel);
        }
        if (!file.ready && ["failed", "cancelled"].includes(file.prepareStatus)) {
          const retry = document.createElement("button");
          retry.type = "button"; retry.className = "mw-action-button mw-primary";
          retry.innerHTML = `${icon("rotate-cw")}<span>重新解析</span>`;
          retry.addEventListener("click", () => retryFile(file)); actions.appendChild(retry);
        }
        const remove = document.createElement("button");
        remove.type = "button"; remove.className = "mw-action-button mw-danger";
        remove.innerHTML = `${icon("trash-2")}<span>删除</span>`;
        remove.addEventListener("click", async () => {
          selectedDocuments.delete(file.id);
          selectedPartsByDocument.delete(file.id);
          await bridge.removeFile(file.id);
          renderAll();
        });
        actions.appendChild(remove); el.filesBody.appendChild(row);
      });
      refreshIcons();
    }

    async function retryFile(file) {
      if (!(file.sourcePdf instanceof File)) { alert("找不到原始 PDF，请删除后重新添加该文件。"); return; }
      try {
        pendingAutoSelect.add(file.id);
        await bridge.preparePaperForMaterial(file.name, file.content || "", file.sourcePdf, { origin: file.prepareOrigin || "local", retry: true });
      } catch (error) {
        if (file.prepareStatus !== "cancelled") alert(`PDF 重新解析失败（${file.name}）：${error.message || error}`);
      } finally { renderAll(); }
    }

    function renderResults() {
      const documents = allResultDocuments();
      el.resultsCount.textContent = `${documents.length} 个文件`; el.resultList.innerHTML = "";
      if (!documents.length) { el.resultList.innerHTML = '<div class="mw-empty-note">暂无结果文件</div>'; activeDocumentId = ""; }
      else if (!documents.some((item) => item.id === activeDocumentId)) activeDocumentId = documents[0].id;
      documents.forEach((docEntry) => {
        const button = document.createElement("button");
        button.type = "button"; button.className = "mw-result-item" + (docEntry.id === activeDocumentId ? " is-active" : "");
        button.setAttribute("role", "option"); button.setAttribute("aria-selected", String(docEntry.id === activeDocumentId));
        button.innerHTML = `<span class="mw-result-name">${escapeHtml(docEntry.name)}</span>`;
        button.addEventListener("click", () => { activeDocumentId = docEntry.id; renderResults(); renderDetail(); });
        el.resultList.appendChild(button);
      });
    }

    function partDisplay(partState) {
      const effective = partState && partState.effective;
      const attempt = partState && partState.latestAttempt;
      if (effective) {
        let text = `${modelLabel(effective) || "未记录模型"} · ${formatDuration(effective.elapsed)}`;
        if (attempt && attempt.run_id !== effective.run_id && attempt.status === "failed") text += " · 更新失败";
        if (attempt && attempt.run_id !== effective.run_id && ["queued", "running"].includes(attempt.status)) text += " · 正在更新";
        return { text, status: "completed" };
      }
      if (attempt && ["queued", "running"].includes(attempt.status)) return { text: `${modelLabel(attempt) || "未记录模型"} · 提取中`, status: "running" };
      if (attempt && attempt.status === "failed") return { text: `${modelLabel(attempt) || "未记录模型"} · 提取失败`, status: "failed" };
      if (attempt && attempt.status === "cancelled") return { text: `${modelLabel(attempt) || "未记录模型"} · 已取消`, status: "cancelled" };
      return { text: "未提取", status: "empty" };
    }

    function taskForPart(documentId, part) {
      return [...activeTasks.values()].find((task) =>
        (task.targets || []).some((target) => target.documentId === documentId && target.part === part)
      ) || null;
    }

    async function stopTask(task) {
      if (!task || task.stopping) return;
      task.stopping = true;
      renderAll();
      await fetch("/api/stop_material/" + task.taskId, { method: "POST" }).catch(() => {});
    }

    function cancelTaskButton(task) {
      if (!task) return null;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "mw-stop-task-button";
      button.disabled = Boolean(task.stopping);
      button.innerHTML = `${icon(task.stopping ? "loader-circle" : "circle-stop")}<span>${task.stopping ? "正在停止…" : "停止本次任务"}</span>`;
      if (task.stopping) button.querySelector("i")?.classList.add("mw-spin");
      button.addEventListener("click", () => { void stopTask(task); });
      return button;
    }

    function stripLeadingName(markdown, name) {
      if (!markdown) return "";
      const escaped = String(name || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return markdown.replace(new RegExp("^###\\s+" + escaped + "\\s*\\n+"), "");
    }

    function renderDetail() {
      if (!activeDocumentId) { el.detailEmpty.classList.remove("is-hidden"); el.detailContent.classList.add("is-hidden"); return; }
      const file = fileById(activeDocumentId);
      const result = documentResults.get(activeDocumentId) || ensureDocumentResult(activeDocumentId, file && file.name);
      el.detailEmpty.classList.add("is-hidden"); el.detailContent.classList.remove("is-hidden");
      el.detailName.textContent = result.name || (file && file.name) || "未命名论文";
      el.archiveBtn.classList.toggle("is-archived", bridge.isArchived(`${activeDocumentId}::v2`));
      const visibleParts = PART_ORDER.filter((part) => {
        const state = result.parts[part];
        return Boolean(state && (state.effective || (state.latestAttempt && state.latestAttempt.status !== "not_selected")));
      });
      if (!visibleParts.includes(activePart)) activePart = visibleParts[0] || "";
      el.tabRow.innerHTML = "";
      visibleParts.forEach((part) => {
        const display = partDisplay(result.parts[part]); const button = document.createElement("button");
        button.type = "button"; button.className = `mw-tab mw-tab-${display.status}` + (part === activePart ? " is-active" : "");
        button.dataset.part = part; button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(part === activePart));
        button.innerHTML = `<span class="mw-tab-name">${PARTS[part].label}</span><span class="mw-tab-meta">${escapeHtml(display.text)}</span>`;
        button.addEventListener("click", () => { activePart = part; renderDetail(); }); el.tabRow.appendChild(button);
      });
      if (!activePart) {
        el.report.innerHTML = '<div class="mw-empty-note">选择上方的提取部分并开始提取后，这里会显示对应结果。</div>';
        refreshIcons();
        return;
      }
      const partState = result.parts[activePart]; const effective = partState.effective; const attempt = partState.latestAttempt;
      const runningTask = taskForPart(activeDocumentId, activePart);
      const partIsRunning = Boolean(runningTask && attempt && ["queued", "running"].includes(attempt.status));
      el.report.innerHTML = "";
      if (effective) {
        if (attempt && attempt.run_id !== effective.run_id && attempt.status === "failed") {
          const notice = document.createElement("div"); notice.className = "mw-report-notice is-warning";
          notice.textContent = `最近一次更新失败：${attempt.error || "未返回具体原因"}。当前继续显示上一份有效结果。`; el.report.appendChild(notice);
        } else if (attempt && attempt.run_id !== effective.run_id && attempt.status === "cancelled") {
          const notice = document.createElement("div"); notice.className = "mw-report-notice is-warning";
          notice.textContent = "本次更新已停止，当前继续显示上一份有效结果。"; el.report.appendChild(notice);
        } else if (attempt && attempt.run_id !== effective.run_id && ["queued", "running"].includes(attempt.status)) {
          const notice = document.createElement("div"); notice.className = "mw-report-notice";
          notice.textContent = "该部分正在重新提取，当前继续显示上一份有效结果。"; el.report.appendChild(notice);
        }
        if (partIsRunning) {
          const action = cancelTaskButton(runningTask);
          if (action) el.report.appendChild(action);
        }
        const content = document.createElement("div"); content.className = "mw-report-content";
        const body = document.createElement("div"); body.className = "mw-report-markdown preview-md";
        const markdown = activePart === "part2" ? effective.content : stripLeadingName(effective.content, result.name);
        const figureMap = bridge.figureMapForDocument(activeDocumentId);
        // 沿用报告预览的交互：点击正文图号后，右侧预览图会根据引用位置对齐。
        // 图片双击放大、图注渲染和导出内容仍由共享预览组件统一处理。
        const figurePreview = bridge.createFigurePreviewPanel(figureMap, { hideWhenEmpty: true });
        bridge.renderMarkdownWithFigureReferences(body, markdown, figureMap, figurePreview);
        content.append(body, figurePreview.panel); el.report.appendChild(content);
      } else if (attempt && ["queued", "running"].includes(attempt.status)) {
        const state = document.createElement("div"); state.className = "mw-report-state";
        state.innerHTML = `<i data-lucide="loader-circle" class="mw-spin" aria-hidden="true"></i><strong>正在提取</strong><span>${escapeHtml(modelLabel(attempt) || "模型处理中")}</span>`;
        const action = partIsRunning ? cancelTaskButton(runningTask) : null;
        if (action) state.appendChild(action);
        el.report.appendChild(state);
      } else if (attempt && attempt.status === "failed") {
        const state = document.createElement("div"); state.className = "mw-report-state is-error";
        const title = document.createElement("strong"); title.textContent = "提取失败";
        const error = document.createElement("span"); error.textContent = attempt.error || "未返回具体原因";
        state.append(title, error); el.report.appendChild(state);
      } else if (attempt && attempt.status === "cancelled") {
        el.report.innerHTML = '<div class="mw-report-state"><strong>已取消</strong><span>该部分尚无有效结果，可以重新选择后提取。</span></div>';
      } else {
        el.report.innerHTML = `<div class="mw-empty-note">该部分尚未提取。请在上方选择“${PARTS[activePart].label}”后开始提取。</div>`;
      }
      refreshIcons();
    }

    function refreshStartButton() {
      const chosenFiles = [...selectedDocuments].map(fileById).filter(Boolean);
      const ready = chosenFiles.length > 0 && chosenFiles.every((file) => file.ready);
      const configuredModel = Boolean(el.model.value) && !el.model.selectedOptions[0]?.disabled;
      const requested = requestedTargets();
      const runnable = runnableTargets();
      // 上方按钮始终表示“启动新的提取”；不同文件或不同部分不会互相占用。
      el.start.classList.toggle("is-busy", startPending);
      if (startPending) {
        el.start.disabled = true;
        el.start.textContent = "正在启动…";
      } else if (!ready || !configuredModel) {
        el.start.disabled = true;
        el.start.textContent = "请选择";
      } else if (!requested.length) {
        el.start.disabled = true;
        el.start.textContent = "选择文件部分";
      } else if (!runnable.length) {
        el.start.disabled = true;
        el.start.textContent = "已在提取";
      } else {
        el.start.disabled = false;
        el.start.textContent = `提取 ${runnable.length} 项`;
      }
    }

    function refreshHeaderStatus() {
      if (startPending) {
        el.headerStatus.textContent = "正在启动提取任务…";
        return;
      }
      if (activeTasks.size) {
        el.headerStatus.textContent = `${activeTasks.size} 个提取任务进行中`;
        return;
      }
      el.headerStatus.textContent = lastTaskMessage;
    }

    function fileSignature() {
      return JSON.stringify(files().map((file) => [file.id, file.ready, file.prepareStatus, file.preparePercent, file.prepareError, file.prepareElapsed, file.prepareTaskId]));
    }

    function renderAll() {
      renderFiles(); renderResults(); renderDetail(); refreshStartButton(); refreshHeaderStatus();
      lastFileSignature = fileSignature();
    }

    async function loadModels() {
      try {
        const response = await fetch("/api/models"); const data = await response.json(); el.model.innerHTML = "";
        (data.models || []).forEach((model) => {
          modelLabels[model.name] = model.label || model.name;
          const option = document.createElement("option"); option.value = model.name; option.textContent = model.label || model.name;
          option.disabled = !model.configured; option.selected = Boolean(model.default); el.model.appendChild(option);
        });
        if (!el.model.value) { const first = [...el.model.options].find((option) => !option.disabled); if (first) first.selected = true; }
      } catch (error) { el.model.innerHTML = '<option value="">模型列表加载失败</option>'; }
      refreshStartButton();
    }

    async function startSingleTarget(target, provider) {
      const file = fileById(target.documentId);
      if (!file || !file.ready) throw new Error("选中的文件尚未完成 PDF 解析");
      const key = targetKey(target.documentId, target.part);
      const result = ensureDocumentResult(file.id, file.name);
      launchingTargets.add(key);
      result.parts[target.part].latestAttempt = {
        status: "queued", content: "", error: "", model: provider,
        model_label: modelLabels[provider] || provider, elapsed: 0, generated_at: "", run_id: "",
      };
      renderAll();
      try {
        // 每次请求只携带一个“文件 + 部分”，后端任务、轮询和取消均可精确隔离。
        const payload = {
          papers: [{
            id: file.id,
            document_id: file.serverDocumentId || file.id,
            name: file.name,
          }],
          provider,
          parts: [target.part],
        };
        const response = await fetch("/api/material_extract", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.task_id) throw new Error(data.error || "提取任务创建失败");
        const task = {
          taskId: data.task_id,
          runId: data.run_id || data.task_id,
          targets: [{ documentId: target.documentId, part: target.part }],
          provider,
          percent: 0,
          stage: "排队中",
          stopping: false,
        };
        result.parts[target.part].latestAttempt = {
          ...result.parts[target.part].latestAttempt,
          run_id: task.runId,
        };
        activeTasks.set(task.taskId, task);
        void pollTask(task.taskId);
      } catch (error) {
        result.parts[target.part].latestAttempt = {
          ...result.parts[target.part].latestAttempt,
          status: "failed",
          error: error.message || String(error),
          generated_at: new Date().toISOString(),
        };
        throw error;
      } finally {
        launchingTargets.delete(key);
      }
    }

    async function startExtraction() {
      const chosen = [...selectedDocuments].map(fileById).filter(Boolean);
      const targets = runnableTargets();
      if (!chosen.length || !targets.length) return;
      if (chosen.some((file) => !file.ready)) { alert("请等待所选 PDF 完成解析后再开始提取。"); return; }
      const provider = el.model.value;
      startPending = true;
      renderAll();
      try {
        const outcomes = await Promise.allSettled(targets.map((target) => startSingleTarget(target, provider)));
        const failures = outcomes.filter((outcome) => outcome.status === "rejected");
        if (failures.length) {
          lastTaskMessage = `${failures.length} 个独立提取任务启动失败`;
          alert(failures.map((outcome) => outcome.reason?.message || String(outcome.reason)).join("\n"));
        }
      } finally {
        startPending = false;
        renderAll();
      }
    }

    async function pollTask(taskId) {
      while (activeTasks.has(taskId)) {
        await new Promise((resolve) => setTimeout(resolve, 700));
        const task = activeTasks.get(taskId);
        if (!task) return;
        let data;
        try {
          const response = await fetch("/api/material_progress/" + taskId); data = await response.json();
          if (!response.ok) throw new Error(data.error || "提取进度查询失败");
        } catch (error) {
          lastTaskMessage = `进度查询失败：${error.message || error}`;
          activeTasks.delete(taskId); renderAll(); return;
        }
        task.percent = Number(data.percent || 0); task.stage = data.stage || "提取中";
        const runInfo = {
          runId: data.run_id || taskId,
          provider: data.provider || task.provider,
          parts: data.parts || (task.targets || []).map((target) => target.part),
        };
        ((data.result && data.result.papers) || []).forEach((paper) => mergePaperState(paper, runInfo));
        if (data.done) {
          lastTaskMessage = data.cancelled ? "上次提取已取消" : data.status === "completed" ? "上次提取已完成" : data.status === "partial" ? "上次提取部分完成" : "上次提取失败";
          activeTasks.delete(taskId); renderAll(); return;
        }
        renderAll();
      }
    }

    function exportCurrent() {
      const result = documentResults.get(activeDocumentId);
      if (!result || completedCount(result) === 0) { alert("当前文件暂无可导出的提取结果。"); return; }
      const now = new Date(); const pad = (number) => String(number).padStart(2, "0");
      const timestamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
      let markdown = `# 材料信息分析报告\n\n论文：${result.name}\n导出时间：${timestamp}\n\n`;
      ["part1", "part2", "part3"].forEach((part) => {
        const effective = result.parts[part].effective; markdown += `## ${PARTS[part].exportTitle}\n\n`;
        if (!effective) { markdown += "（未提取）\n\n"; return; }
        markdown += `> 模型：${modelLabel(effective) || "未记录"}　|　耗时：${formatDuration(effective.elapsed)}　|　生成时间：${formatTime(effective.generated_at) || "未记录"}\n\n`;
        markdown += (part === "part2" ? effective.content : stripLeadingName(effective.content, result.name)) + "\n\n";
      });
      const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" }); const url = URL.createObjectURL(blob); const link = document.createElement("a");
      const base = result.name.replace(/\.[^.]+$/, "").replace(/[\\/:*?"<>|]/g, "_") || "未命名论文";
      link.href = url; link.download = `${base}_材料信息分析报告.md`; document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    }

    function archiveCurrent() {
      const result = documentResults.get(activeDocumentId);
      if (!result || completedCount(result) === 0) { alert("当前文件暂无可归档的提取结果。"); return; }
      const models = new Set(); let elapsed = 0; let generatedAt = "";
      const paper = { name: result.name, part1: "", part2: "", part3: "", model: "", model_label: "" };
      Object.keys(PARTS).forEach((part) => {
        const effective = result.parts[part].effective; if (!effective) return;
        paper[part] = effective.content; const label = modelLabel(effective); if (label) models.add(label);
        elapsed += Number(effective.elapsed || 0); if ((effective.generated_at || "") > generatedAt) generatedAt = effective.generated_at;
      });
      paper.model_label = models.size === 1 ? [...models][0] : "多模型组合"; paper.elapsed = elapsed; paper.generated_at = generatedAt;
      bridge.openArchive(`${activeDocumentId}::v2`, paper);
    }

    // 每行的部分按钮直接维护 selectedPartsByDocument；没有全局部分选择器。
    el.addFile.addEventListener("click", () => el.addFileInput.click());
    el.addFileInput.addEventListener("change", () => {
      [...el.addFileInput.files].forEach((file) => {
        if (!file.name.toLowerCase().endsWith(".pdf")) { alert(`不支持的文件类型：${file.name}（仅支持 PDF）`); return; }
        if (files().some((item) => item.name === file.name)) { alert(`文件列表中已存在同名文件：${file.name}`); return; }
        const promise = bridge.preparePaperForMaterial(file.name, "", file, { origin: "local" });
        const record = files().find((item) => item.name === file.name); if (record) pendingAutoSelect.add(record.id);
        promise.catch((error) => { const latest = files().find((item) => item.name === file.name); if (!latest || latest.prepareStatus !== "cancelled") console.error("PDF 预处理失败", error); }).finally(renderAll);
      });
      el.addFileInput.value = ""; renderAll();
    });
    el.start.addEventListener("click", startExtraction); el.model.addEventListener("change", refreshStartButton);
    el.exportBtn.addEventListener("click", exportCurrent); el.archiveBtn.addEventListener("click", archiveCurrent);

    loadModels(); renderAll();
    setInterval(() => {
      if (fileSignature() !== lastFileSignature) renderAll();
      else refreshStartButton();
    }, 700);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
})();


