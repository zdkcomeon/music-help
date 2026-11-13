const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const token = $('meta[name="csrf-token"]').content;
const fields = $$("[data-field]");
const state = {
  tracks: [],
  selected: new Set(),
  source: "all",
  dirty: new Set(),
  coverChanged: false,
  cover: null,
  busy: false,
  candidates: [],
};
let toastTimer;
let matchRequestId = 0;

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        character
      ],
  );
}
function notify(message, duration = 4500) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), duration);
}
async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== "GET")
    headers.set("X-Music-Help-Token", token);
  const response = await fetch(url, { ...options, headers });
  let body;
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (!response.ok)
    throw new Error(body.error || `请求失败 (${response.status})`);
  return body;
}
function jsonRequest(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
function duration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = String(seconds % 60).padStart(2, "0");
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}`
    : `${minutes}:${rest}`;
}
function selectedTracks() {
  return state.tracks.filter((track) => state.selected.has(track.id));
}
function dirty() {
  return state.dirty.size > 0 || state.coverChanged;
}
function canDiscard() {
  return !dirty() || window.confirm("当前修改尚未保存，确定放弃这些修改吗？");
}
function updateStatus() {
  $("#save-button").disabled = !dirty() || state.busy;
  $("#edit-status").textContent = state.busy
    ? "正在处理，请稍候…"
    : dirty()
      ? "有未保存的修改"
      : "修改后保存到文件";
  $("#download-button").disabled = state.busy || !state.selected.size;
  $("#match-button").disabled = state.busy || state.selected.size !== 1;
  $("#rename-button").disabled = state.busy || !state.selected.size;
  $("#remove-button").disabled = state.busy || !state.selected.size;
  fields.forEach((input) => {
    input.disabled = state.busy;
  });
  $("#choose-cover").disabled = state.busy;
  $("#remove-cover").disabled = state.busy;
}
function visibleTracks() {
  const query = $("#filter").value.trim().toLocaleLowerCase();
  return state.tracks.filter(
    (track) =>
      (state.source === "all" || track.source === state.source) &&
      [track.title, track.artist, track.album, track.name].some((value) =>
        value.toLocaleLowerCase().includes(query),
      ),
  );
}
function coverUrl(track) {
  return `/api/files/${encodeURIComponent(track.id)}/cover?v=${encodeURIComponent(track.version)}`;
}
function renderTable() {
  const tracks = visibleTracks();
  $("#count-all").textContent = state.tracks.length;
  $("#count-library").textContent = state.tracks.filter(
    (track) => track.source === "library",
  ).length;
  $("#count-upload").textContent = state.tracks.filter(
    (track) => track.source === "upload",
  ).length;
  $("#list-summary").textContent =
    `${tracks.length} 首歌曲${state.selected.size ? ` · 已选 ${state.selected.size} 首` : ""}`;
  $("#select-all").checked =
    tracks.length > 0 && tracks.every((track) => state.selected.has(track.id));
  $("#select-all").indeterminate =
    tracks.some((track) => state.selected.has(track.id)) &&
    !$("#select-all").checked;
  $("#track-list").innerHTML = tracks
    .map((track) => {
      const selected = state.selected.has(track.id);
      const cover = track.hasCover
        ? `<img src="${coverUrl(track)}" alt="">`
        : "♫";
      return `<tr class="track-row${selected ? " selected" : ""}" data-id="${escapeHtml(track.id)}"><td><input type="checkbox" aria-label="选择 ${escapeHtml(track.name)}" ${selected ? "checked" : ""}></td><td><div class="song-cell"><div class="track-cover">${cover}</div><div class="song-copy"><strong>${escapeHtml(track.title || track.name.replace(/\.mp3$/i, ""))}</strong><span>${escapeHtml(track.artist || "未知艺术家")}</span></div></div></td><td class="album-cell">${escapeHtml(track.album || "—")}</td><td>${duration(track.duration)}</td></tr>`;
    })
    .join("");
  $("#empty-state").hidden = tracks.length > 0;
  $("#empty-title").textContent = state.tracks.length
    ? "没有找到歌曲"
    : "从音乐开始";
  $("#empty-description").textContent = state.tracks.length
    ? "试试其他搜索条件，或切换文件来源。"
    : "添加 MP3 文件，或从已配置的服务器音乐目录中选择歌曲。";
  $("#empty-add").hidden = state.tracks.length > 0;
  $$("#track-list tr").forEach((row) => {
    row.addEventListener("click", (event) => {
      const next = new Set(state.selected);
      if (event.target.matches("input") || event.metaKey || event.ctrlKey) {
        next.has(row.dataset.id)
          ? next.delete(row.dataset.id)
          : next.add(row.dataset.id);
      } else {
        next.clear();
        next.add(row.dataset.id);
      }
      if (!changeSelection(next)) renderTable();
    });
  });
  updateStatus();
}
function showCover(track) {
  const preview = $("#cover-preview");
  const placeholder = $("#cover-placeholder");
  const src =
    state.coverChanged && state.cover
      ? `data:${state.cover.mime};base64,${state.cover.data}`
      : !state.coverChanged && track && track.hasCover
        ? coverUrl(track)
        : "";
  preview.hidden = !src;
  placeholder.hidden = !!src;
  if (src) preview.src = src;
  else preview.removeAttribute("src");
}
function renderEditor() {
  const selected = selectedTracks();
  const single = selected.length === 1 ? selected[0] : null;
  $("#editor-empty").hidden = selected.length > 0;
  $("#editor").hidden = selected.length === 0;
  $("#editor-badge").textContent = selected.length
    ? `已选 ${selected.length} 首`
    : "未选择";
  if (!selected.length) {
    $("#player").removeAttribute("src");
    return;
  }
  $("#editor-title").textContent = single
    ? single.title || single.name.replace(/\.mp3$/i, "")
    : `批量编辑 ${selected.length} 首歌曲`;
  $("#editor-filename").textContent = single
    ? single.relativePath || single.name
    : "填写需要统一修改的字段";
  $("#batch-notice").hidden = !!single;
  for (const input of fields) {
    const values = new Set(selected.map((track) => track[input.dataset.field]));
    input.value = values.size === 1 ? selected[0][input.dataset.field] : "";
    input.placeholder =
      values.size > 1
        ? "多个值，修改后将应用到所选文件"
        : input.dataset.placeholder || "";
  }
  showCover(single);
  const player = $("#player");
  player.hidden = !single;
  if (single)
    player.src = `/api/files/${encodeURIComponent(single.id)}/audio?v=${encodeURIComponent(single.version)}`;
  else {
    player.pause();
    player.removeAttribute("src");
  }
  updateStatus();
}
function changeSelection(next) {
  if (state.busy) return false;
  if (
    state.selected.size === next.size &&
    [...next].every((id) => state.selected.has(id))
  )
    return true;
  if (!canDiscard()) return false;
  state.selected = next;
  state.dirty.clear();
  state.coverChanged = false;
  state.cover = null;
  renderTable();
  renderEditor();
  return true;
}
function replaceTracks(tracks) {
  for (const track of tracks) {
    const index = state.tracks.findIndex(
      (existing) => existing.id === track.id,
    );
    if (index >= 0) state.tracks[index] = track;
    else state.tracks.push(track);
  }
}
async function loadLibrary(initial = false) {
  if (!initial && (!canDiscard() || state.busy)) return;
  try {
    $("#library-status").textContent = "正在扫描音乐目录";
    const result = await api("/api/library");
    state.tracks = state.tracks.filter((track) => track.source !== "library");
    replaceTracks(result.tracks);
    state.selected = new Set(
      [...state.selected].filter((id) =>
        state.tracks.some((track) => track.id === id),
      ),
    );
    state.dirty.clear();
    state.coverChanged = false;
    state.cover = null;
    $("#library-status").textContent = result.configured
      ? "服务器目录已连接"
      : "未配置服务器目录";
    $(".connection-dot").classList.toggle("ready", result.configured);
    renderTable();
    renderEditor();
    if (result.errors.length)
      notify(
        `${result.errors.length} 个文件无法读取：${result.errors[0].name} - ${result.errors[0].error}`,
        9000,
      );
    else if (!initial)
      notify(
        result.configured
          ? `已读取 ${result.tracks.length} 首歌曲`
          : "尚未配置服务器音乐目录",
      );
  } catch (error) {
    $("#library-status").textContent = "音乐目录读取失败";
    notify(error.message);
  }
}
async function uploadFiles(files) {
  const mp3s = [...files].filter((file) => /\.mp3$/i.test(file.name));
  if (!mp3s.length) return notify("请选择 MP3 文件");
  if (state.busy) return;
  state.busy = true;
  updateStatus();
  let completed = 0;
  const errors = [];
  for (const file of mp3s) {
    notify(
      `正在导入 ${completed + errors.length + 1} / ${mp3s.length}：${file.name}`,
      30000,
    );
    const data = new FormData();
    data.append("file", file);
    try {
      const result = await api("/api/files", { method: "POST", body: data });
      replaceTracks([result.track]);
      completed++;
    } catch (error) {
      errors.push(`${file.name}：${error.message}`);
    }
  }
  state.busy = false;
  state.source = "all";
  $$(".nav-item").forEach((item) =>
    item.classList.toggle("active", item.dataset.source === "all"),
  );
  renderTable();
  updateStatus();
  notify(
    errors.length
      ? `已导入 ${completed} 首，${errors.length} 个文件失败。${errors[0]}`
      : `已导入 ${completed} 首歌曲`,
    errors.length ? 10000 : 4500,
  );
}

for (const input of fields) {
  input.dataset.placeholder = input.placeholder;
  input.addEventListener("input", () => {
    state.dirty.add(input.dataset.field);
    updateStatus();
  });
}
$("#add-files").onclick = $("#empty-add").onclick = () =>
  $("#file-input").click();
$("#add-folder").onclick = () => $("#folder-input").click();
for (const id of ["file-input", "folder-input"])
  $(`#${id}`).addEventListener("change", (event) => {
    uploadFiles(event.target.files);
    event.target.value = "";
  });
$("#filter").addEventListener("input", renderTable);
$$(".nav-item").forEach((item) =>
  item.addEventListener("click", () => {
    state.source = item.dataset.source;
    $$(".nav-item").forEach((other) =>
      other.classList.toggle("active", other === item),
    );
    renderTable();
  }),
);
function selectVisible() {
  const visible = visibleTracks();
  const allSelected =
    visible.length && visible.every((track) => state.selected.has(track.id));
  const next = new Set(state.selected);
  for (const track of visible)
    allSelected ? next.delete(track.id) : next.add(track.id);
  if (!changeSelection(next)) renderTable();
}
$("#select-all").onchange = selectVisible;
$("#select-visible").onclick = selectVisible;
$("#refresh-library").onclick = () => loadLibrary();
$("#refresh-selected").onclick = async () => {
  if (!state.selected.size) return loadLibrary();
  if (!canDiscard() || state.busy) return;
  try {
    const result = await Promise.all(
      selectedTracks().map((track) =>
        api(`/api/files/${encodeURIComponent(track.id)}`),
      ),
    );
    replaceTracks(result.map((item) => item.track));
    state.dirty.clear();
    state.coverChanged = false;
    state.cover = null;
    renderTable();
    renderEditor();
    notify("已重新读取选中的文件");
  } catch (error) {
    notify(error.message);
  }
};
$("#choose-cover").onclick = () => $("#cover-input").click();
$("#cover-input").onchange = async (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  if (
    !["image/jpeg", "image/png"].includes(file.type) ||
    file.size > 10 * 1024 * 1024
  )
    return notify("请选择不超过 10 MB 的 JPEG 或 PNG 图片");
  const reader = new FileReader();
  reader.onload = () => {
    state.cover = { mime: file.type, data: reader.result.split(",")[1] };
    state.coverChanged = true;
    showCover(null);
    updateStatus();
  };
  reader.readAsDataURL(file);
};
$("#remove-cover").onclick = () => {
  state.cover = null;
  state.coverChanged = true;
  showCover(null);
  updateStatus();
};
$("#save-button").onclick = async () => {
  if (!dirty() || state.busy) return;
  const selected = selectedTracks();
  const payload = {
    files: selected.map(({ id, version }) => ({ id, version })),
    changes: {},
  };
  fields.forEach((input) => {
    if (state.dirty.has(input.dataset.field))
      payload.changes[input.dataset.field] = input.value;
  });
  if (state.coverChanged) payload.cover = state.cover;
  $("#player").pause();
  $("#player").removeAttribute("src");
  $("#player").load();
  state.busy = true;
  updateStatus();
  try {
    const result = await api("/api/tags", jsonRequest("POST", payload));
    replaceTracks(result.tracks);
    if (result.errors.length) {
      state.selected = new Set(result.errors.map((item) => item.id));
      $("#editor-badge").textContent = `已选 ${state.selected.size} 首`;
      $("#editor-title").textContent = `${state.selected.size} 首歌曲保存失败`;
      $("#editor-filename").textContent = "修改仍保留在编辑区";
      notify(
        `已保存 ${result.tracks.length} 首，${result.errors.length} 首失败。${result.errors[0].error}`,
        10000,
      );
      renderTable();
    } else {
      state.dirty.clear();
      state.coverChanged = false;
      state.cover = null;
      renderTable();
      renderEditor();
      notify(`已保存 ${result.tracks.length} 首歌曲`);
    }
  } catch (error) {
    notify(error.message);
  } finally {
    state.busy = false;
    updateStatus();
  }
};
$("#download-button").onclick = () => {
  if (dirty()) return notify("请先保存修改，再下载文件");
  const query = new URLSearchParams();
  selectedTracks().forEach((track) => query.append("id", track.id));
  window.location.href = `/api/download?${query}`;
};
$("#remove-button").onclick = async () => {
  if (!state.selected.size || state.busy || !canDiscard()) return;
  const selected = selectedTracks();
  if (!window.confirm(`将 ${selected.length} 首歌曲移出当前列表？`)) return;
  state.busy = true;
  updateStatus();
  const errors = [];
  for (const track of selected) {
    try {
      await api(`/api/files/${encodeURIComponent(track.id)}`, {
        method: "DELETE",
      });
      state.tracks = state.tracks.filter((item) => item.id !== track.id);
      state.selected.delete(track.id);
    } catch (error) {
      errors.push(error.message);
    }
  }
  state.dirty.clear();
  state.coverChanged = false;
  state.cover = null;
  state.busy = false;
  renderTable();
  renderEditor();
  notify(errors.length ? `部分文件未移出：${errors[0]}` : "已移出列表");
};

$("#match-button").onclick = () => {
  const track = selectedTracks()[0];
  if (!track || state.selected.size !== 1) return;
  let title = $('[data-field="title"]').value;
  let artist = $('[data-field="artist"]').value;
  if (!title) {
    const filename = track.name.replace(/\.mp3$/i, "");
    const parts = filename.split(" - ");
    if (!artist && parts.length > 1) {
      artist = parts.shift();
      title = parts.join(" - ");
    } else title = filename;
  }
  $("#match-title").value = title;
  $("#match-artist").value = artist;
  $("#match-status").textContent = "搜索 MusicBrainz 和 LRCLIB";
  $("#match-results").innerHTML = "";
  $("#match-dialog").showModal();
  $("#match-form").requestSubmit();
};
$("#match-form").onsubmit = async (event) => {
  event.preventDefault();
  const title = $("#match-title").value.trim();
  const artist = $("#match-artist").value.trim();
  if (!title) return;
  const requestId = ++matchRequestId;
  $("#match-status").textContent = "正在搜索，请稍候…";
  $("#match-results").innerHTML = "";
  const query = new URLSearchParams({ title, artist });
  try {
    const result = await api(`/api/online/search?${query}`);
    if (requestId !== matchRequestId || !$("#match-dialog").open) return;
    state.candidates = result.candidates;
    $("#match-status").textContent =
      `${result.candidates.length ? `找到 ${result.candidates.length} 个候选结果` : "未找到候选结果"}${result.warnings.length ? ` · ${result.warnings.join("、")}` : ""}`;
    $("#match-results").innerHTML = result.candidates
      .map(
        (candidate, index) =>
          `<div class="match-result"><div><strong>${escapeHtml(candidate.title || "未知歌曲")}</strong><span>${escapeHtml(candidate.artist || "未知艺术家")} · ${escapeHtml(candidate.album || "未知专辑")} · ${duration(candidate.duration)}<br>${escapeHtml(candidate.source)}${candidate.year ? ` · ${escapeHtml(candidate.year)}` : ""}</span></div><button class="button subtle" data-candidate="${index}">选用</button></div>`,
      )
      .join("");
    $$("[data-candidate]").forEach((button) =>
      button.addEventListener("click", () =>
        applyCandidate(state.candidates[Number(button.dataset.candidate)]),
      ),
    );
  } catch (error) {
    if (requestId === matchRequestId && $("#match-dialog").open)
      $("#match-status").textContent = `搜索失败：${error.message}`;
  }
};
async function applyCandidate(candidate) {
  const selectedId = selectedTracks()[0]?.id;
  const requestId = matchRequestId;
  $("#match-status").textContent = "正在获取封面和歌词…";
  $$("#match-results button").forEach((button) => {
    button.disabled = true;
  });
  try {
    const result = await api(
      "/api/online/details",
      jsonRequest("POST", candidate),
    );
    if (
      !$("#match-dialog").open ||
      requestId !== matchRequestId ||
      selectedTracks()[0]?.id !== selectedId
    )
      return;
    for (const field of ["title", "artist", "album", "year"]) {
      if (candidate[field]) {
        $(`[data-field="${field}"]`).value = candidate[field];
        state.dirty.add(field);
      }
    }
    if (result.lyrics) {
      $('[data-field="lyrics"]').value = result.lyrics;
      state.dirty.add("lyrics");
    }
    if (result.cover) {
      state.cover = result.cover;
      state.coverChanged = true;
      showCover(null);
    }
    updateStatus();
    $("#match-dialog").close();
    const missing = [!result.cover && "封面", !result.lyrics && "歌词"].filter(
      Boolean,
    );
    notify(
      `已填入匹配结果，请检查后保存${missing.length ? `。未获取到${missing.join("和")}` : ""}`,
      7000,
    );
  } catch (error) {
    if (requestId === matchRequestId && $("#match-dialog").open) {
      $("#match-status").textContent = `获取失败：${error.message}`;
      $$("#match-results button").forEach((button) => {
        button.disabled = false;
      });
    }
  }
}
$("#match-dialog").addEventListener("close", () => {
  matchRequestId++;
});

function updateRenamePreview() {
  const template = $("#rename-template").value;
  $("#rename-preview").innerHTML = selectedTracks()
    .map((track) => {
      const values = { ...track, filename: track.name.replace(/\.mp3$/i, "") };
      const name = template
        .replace(/%([a-zA-Z]+)%/g, (_, field) => values[field] || "")
        .replace(/[\x00-\x1f<>:"/\\|?*]/g, "_")
        .replace(/^[ .]+|[ .]+$/g, "");
      return `<div>${escapeHtml(track.name)} <span>→</span> <b>${escapeHtml(name.replace(/\.mp3$/i, "") || "…")}.mp3</b></div>`;
    })
    .join("");
}
$("#rename-button").onclick = () => {
  if (dirty()) return notify("请先保存标签修改，再重命名");
  updateRenamePreview();
  $("#rename-dialog").showModal();
};
$("#rename-template").oninput = updateRenamePreview;
$("#apply-rename").onclick = async () => {
  if (state.busy) return;
  $("#player").pause();
  $("#player").removeAttribute("src");
  $("#player").load();
  state.busy = true;
  updateStatus();
  $("#apply-rename").disabled = true;
  try {
    const result = await api(
      "/api/rename",
      jsonRequest("POST", {
        files: selectedTracks().map(({ id, version }) => ({ id, version })),
        template: $("#rename-template").value,
      }),
    );
    replaceTracks(result.tracks);
    renderTable();
    renderEditor();
    $("#rename-dialog").close();
    notify(
      result.errors.length
        ? `已重命名 ${result.tracks.length} 首，${result.errors.length} 首失败。${result.errors[0].error}`
        : `已重命名 ${result.tracks.length} 首歌曲`,
      result.errors.length ? 10000 : 4500,
    );
  } catch (error) {
    notify(error.message);
  } finally {
    state.busy = false;
    $("#apply-rename").disabled = false;
    updateStatus();
  }
};
$$("[data-close]").forEach((button) =>
  button.addEventListener("click", () => $(`#${button.dataset.close}`).close()),
);
window.addEventListener("beforeunload", (event) => {
  if (dirty()) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("keydown", (event) => {
  if (
    event.key === "/" &&
    !event.target.matches("input, textarea") &&
    !document.querySelector("dialog[open]")
  ) {
    event.preventDefault();
    $("#filter").focus();
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    if (!$("#save-button").disabled) $("#save-button").click();
  }
});
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => {
  event.preventDefault();
  if (event.dataTransfer.files.length) uploadFiles(event.dataTransfer.files);
});
renderTable();
loadLibrary(true);
