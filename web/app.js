const state = {
  jobId: null,
  job: null,
  activeId: null,
  currentPage: 1,
  pollTimer: null,
  viewMode: "both",
  syncLock: false,
};

const els = {
  configText: document.querySelector("#configText"),
  fileInput: document.querySelector("#fileInput"),
  heroFileInput: document.querySelector("#heroFileInput"),
  autoTranslateInput: document.querySelector("#autoTranslateInput"),
  translatePageButton: document.querySelector("#translatePageButton"),
  translateAllButton: document.querySelector("#translateAllButton"),
  retryButton: document.querySelector("#retryButton"),
  exportButton: document.querySelector("#exportButton"),
  copyActiveButton: document.querySelector("#copyActiveButton"),
  emptyState: document.querySelector("#emptyState"),
  workspace: document.querySelector("#workspace"),
  fileName: document.querySelector("#fileName"),
  statusText: document.querySelector("#statusText"),
  progressBar: document.querySelector("#progressBar"),
  progressText: document.querySelector("#progressText"),
  currentPageText: document.querySelector("#currentPageText"),
  searchInput: document.querySelector("#searchInput"),
  pageList: document.querySelector("#pageList"),
  pdfView: document.querySelector("#pdfView"),
  translationList: document.querySelector("#translationList"),
  countText: document.querySelector("#countText"),
  toast: document.querySelector("#toast"),
  prevPageButton: document.querySelector("#prevPageButton"),
  nextPageButton: document.querySelector("#nextPageButton"),
  modeBoth: document.querySelector("#modeBoth"),
  modeOriginal: document.querySelector("#modeOriginal"),
  modeTranslation: document.querySelector("#modeTranslation"),
};

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => els.toast.classList.add("hidden"), 3600);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = data.detail || detail;
    } catch {
      detail = await response.text();
    }
    throw new Error(detail);
  }
  return response.json();
}

async function loadConfig() {
  try {
    const config = await api("/api/config");
    const keyText = config.has_key ? "Key 已配置" : "Key 未配置";
    els.configText.textContent = `${config.model} · ${keyText} · ${config.base_url}`;
    if (!config.has_key) showToast("请先在 .env 中配置 LLM_API_KEY");
  } catch {
    els.configText.textContent = "配置读取失败";
  }
}

async function uploadFile(file, autoTranslate = true) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    showToast("请上传 PDF 文件");
    return;
  }

  resetWorkspace(file.name);
  const form = new FormData();
  form.append("file", file);
  form.append("auto_translate", String(autoTranslate));

  try {
    const result = await api("/api/jobs", { method: "POST", body: form });
    state.jobId = result.job_id;
    window.history.replaceState(null, "", `#${state.jobId}`);
    await refreshJob();
    startPollingIfNeeded();
  } catch (error) {
    setStatus("error", 0, 1);
    showToast(error.message);
  }
}

function resetWorkspace(fileName) {
  clearPoll();
  state.job = null;
  state.activeId = null;
  state.currentPage = 1;
  setWorkspaceVisible(true);
  setStatus("queued", 0, 1);
  els.fileName.textContent = fileName;
  els.pdfView.innerHTML = "";
  els.translationList.innerHTML = "";
  els.pageList.innerHTML = "";
}

function setWorkspaceVisible(visible) {
  els.emptyState.classList.toggle("hidden", visible);
  els.workspace.classList.toggle("hidden", !visible);
}

function setStatus(status, done, total) {
  const names = {
    ready: "可阅读",
    queued: "排队中",
    translating: "翻译中",
    done: "已完成",
    error: "出错",
  };
  els.statusText.textContent = names[status] || status;
  const percent = total ? Math.round((done / total) * 100) : 0;
  els.progressBar.style.width = `${Math.min(100, percent)}%`;
  els.progressText.textContent = `${done} / ${total}`;
}

async function refreshJob() {
  if (!state.jobId) return;
  try {
    const job = await api(`/api/jobs/${state.jobId}`);
    state.job = job;
    renderJob();
    startPollingIfNeeded();
    if (job.status === "error") showToast(job.error || "翻译失败");
  } catch (error) {
    clearPoll();
    showToast(error.message);
  }
}

function startPollingIfNeeded() {
  if (!state.job) return;
  const shouldPoll = state.job.status === "queued" || state.job.status === "translating";
  if (shouldPoll && !state.pollTimer) {
    state.pollTimer = window.setInterval(refreshJob, 1800);
  }
  if (!shouldPoll) clearPoll();
}

function clearPoll() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function renderJob() {
  const job = state.job;
  setWorkspaceVisible(true);
  els.fileName.textContent = job.filename;
  setStatus(job.status, job.progress.done, job.progress.total);
  els.countText.textContent = `${job.blocks.length} 段`;
  els.currentPageText.textContent = `第 ${state.currentPage} 页`;
  const busy = job.status === "queued" || job.status === "translating";
  els.retryButton.disabled = busy;
  els.translateAllButton.disabled = busy;
  els.translatePageButton.disabled = busy;
  els.copyActiveButton.disabled = !state.activeId;
  els.exportButton.classList.toggle("disabled", !job.blocks.some((block) => block.translation));
  els.exportButton.setAttribute("aria-disabled", String(!job.blocks.some((block) => block.translation)));
  renderPages(job);
  renderTranslationPages(job);
  updatePageButtons();
}

function renderPages(job) {
  if (els.pdfView.dataset.jobId === job.id) {
    updatePageList();
    updateHotspotState();
    return;
  }

  els.pdfView.dataset.jobId = job.id;
  els.pdfView.innerHTML = "";
  els.pageList.innerHTML = "";

  const blocksByPage = groupBlocksByPage(job.blocks);
  for (const page of job.pages) {
    const pageEl = document.createElement("article");
    pageEl.className = "page";
    pageEl.id = `page-${page.page}`;
    pageEl.dataset.page = page.page;

    const img = document.createElement("img");
    img.src = page.image;
    img.alt = `第 ${page.page} 页`;
    pageEl.appendChild(img);

    for (const block of blocksByPage.get(page.page) || []) {
      const hotspot = document.createElement("button");
      hotspot.type = "button";
      hotspot.className = "hotspot";
      hotspot.dataset.blockId = block.id;
      hotspot.title = block.text.slice(0, 160);
      positionHotspot(hotspot, block, page);
      hotspot.addEventListener("click", () => focusBlock(block.id, "translation"));
      pageEl.appendChild(hotspot);
    }
    els.pdfView.appendChild(pageEl);
  }
  updatePageList();
}

function groupBlocksByPage(blocks) {
  const map = new Map();
  for (const block of blocks) {
    if (!map.has(block.page)) map.set(block.page, []);
    map.get(block.page).push(block);
  }
  return map;
}

function updatePageList() {
  const job = state.job;
  els.pageList.innerHTML = "";
  const pageStatus = new Map((job.page_status || []).map((item) => [item.page, item]));
  for (const page of job.pages) {
    const status = pageStatus.get(page.page) || { translated: 0, total: 0, done: false };
    const button = document.createElement("button");
    button.className = "pageChip";
    button.type = "button";
    button.dataset.page = page.page;
    button.classList.toggle("active", page.page === state.currentPage);
    button.classList.toggle("done", status.done);
    button.innerHTML = `<span>第 ${page.page} 页</span><span>${status.translated}/${status.total}</span>`;
    button.addEventListener("click", () => scrollToPage(page.page));
    els.pageList.appendChild(button);
  }
}

function positionHotspot(el, block, page) {
  const [x0, y0, x1, y1] = block.bbox;
  el.style.left = `${(x0 / page.width) * 100}%`;
  el.style.top = `${(y0 / page.height) * 100}%`;
  el.style.width = `${((x1 - x0) / page.width) * 100}%`;
  el.style.height = `${((y1 - y0) / page.height) * 100}%`;
}

function renderTranslationPages(job) {
  if (els.translationList.dataset.jobId === job.id) {
    updateTranslationTexts(job);
    updateHotspotState();
    return;
  }

  els.translationList.dataset.jobId = job.id;
  els.translationList.innerHTML = "";
  const blocksByPage = groupBlocksByPage(job.blocks);

  for (const page of job.pages) {
    const pageEl = document.createElement("article");
    pageEl.className = "translatedPage";
    pageEl.id = `translation-page-${page.page}`;
    pageEl.dataset.page = page.page;

    const pageLabel = document.createElement("div");
    pageLabel.className = "translatedPageLabel";
    pageLabel.textContent = `Page ${page.page}`;
    pageEl.appendChild(pageLabel);

    const paperBody = document.createElement("div");
    paperBody.className = "translatedPaperBody";
    pageEl.appendChild(paperBody);

    buildTranslatedPageLayout(paperBody, blocksByPage.get(page.page) || [], page);

    els.translationList.appendChild(pageEl);
  }

  updateTranslationTexts(job);
  updateHotspotState();
}

function buildTranslatedPageLayout(container, blocks, page) {
  const wideLimit = page.width * 0.62;
  const narrowBlocks = blocks.filter((block) => block.bbox[2] - block.bbox[0] < wideLimit);
  const leftCount = narrowBlocks.filter((block) => (block.bbox[0] + block.bbox[2]) / 2 < page.width / 2).length;
  const rightCount = narrowBlocks.length - leftCount;
  const twoColumn = leftCount >= 2 && rightCount >= 2;

  const fullFlow = document.createElement("div");
  fullFlow.className = "translatedFlow";
  container.appendChild(fullFlow);

  if (!twoColumn) {
    for (const block of blocks) fullFlow.appendChild(createTranslatedBlock(block));
    return;
  }

  const fullBlocks = [];
  const leftBlocks = [];
  const rightBlocks = [];
  for (const block of blocks) {
    const width = block.bbox[2] - block.bbox[0];
    const center = (block.bbox[0] + block.bbox[2]) / 2;
    if (width >= wideLimit) {
      fullBlocks.push(block);
    } else if (center < page.width / 2) {
      leftBlocks.push(block);
    } else {
      rightBlocks.push(block);
    }
  }

  for (const block of fullBlocks.sort(byPagePosition)) fullFlow.appendChild(createTranslatedBlock(block, true));

  const columns = document.createElement("div");
  columns.className = "translatedColumns";
  const leftColumn = document.createElement("div");
  const rightColumn = document.createElement("div");
  leftColumn.className = "translatedColumn";
  rightColumn.className = "translatedColumn";
  columns.append(leftColumn, rightColumn);
  container.appendChild(columns);

  for (const block of leftBlocks.sort(byPagePosition)) leftColumn.appendChild(createTranslatedBlock(block));
  for (const block of rightBlocks.sort(byPagePosition)) rightColumn.appendChild(createTranslatedBlock(block));
}

function byPagePosition(a, b) {
  return a.bbox[1] - b.bbox[1] || a.bbox[0] - b.bbox[0];
}

function createTranslatedBlock(block, wide = false) {
  const blockEl = document.createElement("button");
  blockEl.type = "button";
  blockEl.className = `translatedBlock${wide ? " wide" : ""}`;
  blockEl.dataset.blockId = block.id;
  blockEl.dataset.page = block.page;
  blockEl.title = block.text.slice(0, 160);
  blockEl.addEventListener("click", () => focusBlock(block.id, "pdf"));
  return blockEl;
}

function updateTranslationTexts(job) {
  const query = els.searchInput.value.trim().toLowerCase();
  for (const block of job.blocks) {
    const blockEl = els.translationList.querySelector(`.translatedBlock[data-block-id="${CSS.escape(block.id)}"]`);
    if (!blockEl) continue;
    const combined = `${block.text} ${block.translation}`.toLowerCase();
    blockEl.hidden = Boolean(query && !combined.includes(query));
    blockEl.classList.toggle("pending", !block.translation);
    blockEl.innerHTML = `
      <span class="translatedBlockMeta">P${block.page} · ${block.index + 1}</span>
      <span class="translatedBlockText">${escapeHtml(block.translation || "等待翻译")}</span>
    `;
  }
  updateHotspotState();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function focusBlock(blockId, target) {
  state.activeId = blockId;
  const block = state.job?.blocks.find((item) => item.id === blockId);
  if (block) state.currentPage = block.page;
  els.currentPageText.textContent = `第 ${state.currentPage} 页`;
  updatePageList();
  updateHotspotState();

  const translation = els.translationList.querySelector(`.translatedBlock[data-block-id="${CSS.escape(blockId)}"]`);
  const hotspot = els.pdfView.querySelector(`.hotspot[data-block-id="${CSS.escape(blockId)}"]`);
  if (target === "translation" && translation) {
    scrollElementIntoPane(els.translationList, translation);
  }
  if (target === "pdf" && hotspot) {
    scrollElementIntoPane(els.pdfView, hotspot);
  }
}

function updateHotspotState() {
  for (const hotspot of els.pdfView.querySelectorAll(".hotspot")) {
    hotspot.classList.toggle("active", hotspot.dataset.blockId === state.activeId);
  }
  for (const item of els.translationList.querySelectorAll(".translationBlock")) {
    item.classList.toggle("active", item.dataset.blockId === state.activeId);
  }
  for (const item of els.translationList.querySelectorAll(".translatedBlock")) {
    item.classList.toggle("active", item.dataset.blockId === state.activeId);
  }
  els.copyActiveButton.disabled = !state.activeId;
}

function scrollToPage(page) {
  state.currentPage = Number(page);
  const sourcePage = document.querySelector(`#page-${page}`);
  const translatedPage = document.querySelector(`#translation-page-${page}`);
  if (sourcePage) scrollElementIntoPane(els.pdfView, sourcePage, "start");
  if (translatedPage) scrollElementIntoPane(els.translationList, translatedPage, "start");
  els.currentPageText.textContent = `第 ${state.currentPage} 页`;
  updatePageList();
  updatePageButtons();
}

function scrollElementIntoPane(pane, element, block = "center") {
  const paneRect = pane.getBoundingClientRect();
  const elementRect = element.getBoundingClientRect();
  const offset = elementRect.top - paneRect.top;
  const target =
    block === "start"
      ? pane.scrollTop + offset - 16
      : pane.scrollTop + offset - pane.clientHeight / 2 + elementRect.height / 2;
  pane.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
}

function getPagePosition(pane, selector) {
  const paneRect = pane.getBoundingClientRect();
  const pages = [...pane.querySelectorAll(selector)];
  let best = null;
  for (const page of pages) {
    const rect = page.getBoundingClientRect();
    const distance = Math.abs(rect.top - paneRect.top - 16);
    if (!best || distance < best.distance) {
      const scrollInside = Math.max(0, paneRect.top + pane.scrollTop - rect.top);
      const maxInside = Math.max(1, page.offsetHeight - pane.clientHeight);
      best = {
        page: Number(page.dataset.page),
        ratio: Math.max(0, Math.min(1, scrollInside / maxInside)),
        distance,
      };
    }
  }
  return best;
}

function syncScroll(sourcePane, targetPane, sourceSelector, targetSelector) {
  if (state.syncLock || state.viewMode !== "both") return;
  const pos = getPagePosition(sourcePane, sourceSelector);
  if (!pos) return;
  const targetPage = targetPane.querySelector(`${targetSelector}[data-page="${pos.page}"]`);
  if (!targetPage) return;

  state.syncLock = true;
  const maxInside = Math.max(1, targetPage.offsetHeight - targetPane.clientHeight);
  const targetTop = targetPage.offsetTop + maxInside * pos.ratio;
  targetPane.scrollTop = Math.max(0, targetTop);
  state.currentPage = pos.page;
  els.currentPageText.textContent = `第 ${state.currentPage} 页`;
  updatePageList();
  updatePageButtons();
  window.setTimeout(() => {
    state.syncLock = false;
  }, 80);
}

function syncPaneHeights() {
  // Translation pages use natural height so longer Chinese text never bleeds into the next page.
}

function updatePageButtons() {
  const maxPage = state.job?.pages.length || 1;
  els.prevPageButton.disabled = state.currentPage <= 1;
  els.nextPageButton.disabled = state.currentPage >= maxPage;
}

async function translatePages(pages = null) {
  if (!state.jobId) return;
  try {
    await api(`/api/jobs/${state.jobId}/translate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pages ? { pages } : {}),
    });
    await refreshJob();
  } catch (error) {
    showToast(error.message);
  }
}

async function retryJob() {
  if (!state.jobId) return;
  try {
    await api(`/api/jobs/${state.jobId}/retry`, { method: "POST" });
    await refreshJob();
  } catch (error) {
    showToast(error.message);
  }
}

function setViewMode(mode) {
  state.viewMode = mode;
  document.body.dataset.viewMode = mode;
  els.modeBoth.classList.toggle("active", mode === "both");
  els.modeOriginal.classList.toggle("active", mode === "original");
  els.modeTranslation.classList.toggle("active", mode === "translation");
}

async function copyBlock(blockId) {
  const block = state.job?.blocks.find((item) => item.id === blockId);
  if (!block) return;
  const text = block.translation || block.text;
  await navigator.clipboard.writeText(text);
  showToast("已复制");
}

function exportMarkdown() {
  if (!state.job) return;
  const lines = [`# ${state.job.filename}`, ""];
  for (const block of state.job.blocks) {
    if (!block.translation) continue;
    lines.push(`## Page ${block.page} · ${block.id}`, "", block.text, "", block.translation, "");
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.job.filename.replace(/\.pdf$/i, "")}.translated.md`;
  link.click();
  URL.revokeObjectURL(url);
}

function bindEvents() {
  els.fileInput.addEventListener("change", (event) => uploadFile(event.target.files[0], true));
  els.heroFileInput.addEventListener("change", (event) => uploadFile(event.target.files[0], els.autoTranslateInput.checked));
  els.translatePageButton.addEventListener("click", () => translatePages([state.currentPage]));
  els.translateAllButton.addEventListener("click", () => translatePages());
  els.retryButton.addEventListener("click", retryJob);
  els.copyActiveButton.addEventListener("click", () => copyBlock(state.activeId));
  els.exportButton.addEventListener("click", (event) => {
    event.preventDefault();
    if (!els.exportButton.classList.contains("disabled")) exportMarkdown();
  });
  els.searchInput.addEventListener("input", () => state.job && updateTranslationTexts(state.job));
  els.prevPageButton.addEventListener("click", () => scrollToPage(Math.max(1, state.currentPage - 1)));
  els.nextPageButton.addEventListener("click", () => scrollToPage(Math.min(state.job?.pages.length || 1, state.currentPage + 1)));
  els.pdfView.addEventListener("scroll", () => syncScroll(els.pdfView, els.translationList, ".page", ".translatedPage"));
  els.translationList.addEventListener("scroll", () =>
    syncScroll(els.translationList, els.pdfView, ".translatedPage", ".page")
  );
  window.addEventListener("resize", syncPaneHeights);
  els.modeBoth.addEventListener("click", () => setViewMode("both"));
  els.modeOriginal.addEventListener("click", () => setViewMode("original"));
  els.modeTranslation.addEventListener("click", () => setViewMode("translation"));
}

async function restoreFromHash() {
  const jobId = window.location.hash.replace("#", "").trim();
  if (!jobId) return;
  state.jobId = jobId;
  try {
    await refreshJob();
  } catch {
    state.jobId = null;
  }
}

bindEvents();
setViewMode("both");
loadConfig();
restoreFromHash();
