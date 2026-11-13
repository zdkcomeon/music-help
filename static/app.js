const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const token = $('meta[name="csrf-token"]').content;
const audioExtensions = new Set($('meta[name="audio-extensions"]').content.split(","));
const LONG_AUDIO_SECONDS = 1800;
const songSources = ["netease_song", "netease_album", "musicbrainz", "musicbrainz_release", "lrclib"];
const longAudioSources = ["subtitle", ...songSources];
const languageNames = {
  en: "英语",
  zh: "中文",
  zt: "中文（繁体）",
  ja: "日语",
  ko: "韩语",
  es: "西班牙语",
  fr: "法语",
  de: "德语",
  pt: "葡萄牙语",
  ru: "俄语",
};
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
let matchApplying = false;

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
      const format = `${track.format}${track.extensionMismatch ? " · 后缀不匹配" : ""}`;
      return `<tr class="track-row${selected ? " selected" : ""}" data-id="${escapeHtml(track.id)}"><td><input type="checkbox" aria-label="选择 ${escapeHtml(track.name)}" ${selected ? "checked" : ""}></td><td><div class="song-cell"><div class="track-cover">${cover}</div><div class="song-copy"><strong>${escapeHtml(track.title || track.baseName)}</strong><span>${escapeHtml(track.artist || "未知艺术家")} · ${escapeHtml(format)}</span></div></div></td><td class="album-cell">${escapeHtml(track.album || "—")}</td><td>${duration(track.duration)}</td></tr>`;
    })
    .join("");
  $("#empty-state").hidden = tracks.length > 0;
  $("#empty-title").textContent = state.tracks.length
    ? "没有找到歌曲"
    : "从音乐开始";
  $("#empty-description").textContent = state.tracks.length
    ? "试试其他搜索条件，或切换文件来源。"
    : "添加音频文件，或从已配置的服务器音乐目录中选择歌曲。";
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
    ? single.title || single.baseName
    : `批量编辑 ${selected.length} 首歌曲`;
  $("#editor-filename").textContent = single
    ? single.relativePath || single.name
    : "填写需要统一修改的字段";
  $("#editor-format").textContent = single
    ? `${single.format}${single.extensionMismatch ? ` · 建议后缀 ${single.detectedExtension}` : ""}`
    : "";
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
async function likelyAudio(file) {
  const suffix = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (audioExtensions.has(suffix) || file.type.startsWith("audio/")) return true;
  const header = new Uint8Array(await file.slice(0, 16).arrayBuffer());
  const text = String.fromCharCode(...header);
  return ["ID3", "fLaC", "OggS", "RIFF", "FORM", "TTA1", "wvpk", "MAC ", "tBaK"].some((value) => text.startsWith(value)) ||
    text.slice(4, 8) === "ftyp" ||
    (header[0] === 0xff && (header[1] & 0xe0) === 0xe0) ||
    (header[0] === 0x30 && header[1] === 0x26 && header[2] === 0xb2 && header[3] === 0x75);
}

async function uploadFiles(files, folder = false) {
  const selected = [...files];
  if (!selected.length) return notify("请选择音频文件");
  if (state.busy) return;
  state.busy = true;
  updateStatus();
  let completed = 0;
  const errors = [];
  const candidates = [];
  for (const file of selected) {
    try {
      if (!folder || await likelyAudio(file)) candidates.push(file);
    } catch (error) {
      errors.push(`${file.name}：${error.message}`);
    }
  }
  for (const [index, file] of candidates.entries()) {
    notify(
      `正在导入 ${index + 1} / ${candidates.length}：${file.name}`,
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
  if (!candidates.length && !errors.length) return notify("没有找到可识别的音频文件");
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
    uploadFiles(event.target.files, id === "folder-input");
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
    const filename = track.baseName;
    const parts = filename.split(" - ");
    if (!artist && parts.length > 1) {
      artist = parts.shift();
      title = parts.join(" - ");
    } else title = filename;
  }
  $("#match-title").value = title;
  $("#match-artist").value = artist;
  $("#match-status").textContent = "请填写名称或艺术家，然后点击搜索。";
  $("#match-results").innerHTML = "";
  const longTrack = track.duration >= LONG_AUDIO_SECONDS;
  $("#match-hint").hidden = !longTrack;
  $("#match-hint").textContent =
    "这是一段较长的音频，可能包含多首歌曲或完整演出。字幕检索按影片名进行（例如 Taylor Swift The Eras Tour），结果会带上时间轴；歌曲和专辑结果仍可选用，但不会自动填入单曲歌词。";
  $("#match-dialog").showModal();
};
$("#match-form").onsubmit = async (event) => {
  event.preventDefault();
  const title = $("#match-title").value.trim();
  const artist = $("#match-artist").value.trim();
  if (!title && !artist) {
    $("#match-status").textContent = "请填写名称或艺术家。";
    return;
  }
  const requestId = ++matchRequestId;
  matchApplying = false;
  $("#match-results").innerHTML = "";
  const sourceNames = {
    netease_song: "网易云歌曲",
    netease_album: "网易云专辑",
    musicbrainz: "MusicBrainz 歌曲",
    musicbrainz_release: "MusicBrainz 专辑",
    lrclib: "LRCLIB",
    subtitle: "TheSubtitleDB 字幕",
  };
  const sourceResults = {};
  const operationId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const trackDuration = selectedTracks()[0]?.duration || 0;
  const sourceOrder = trackDuration >= LONG_AUDIO_SECONDS ? longAudioSources : songSources;
  const current = () => requestId === matchRequestId && $("#match-dialog").open;
  function renderMatches() {
    if (!current() || matchApplying) return;
    state.candidates = sourceOrder.flatMap((key) => sourceResults[key]?.candidates || []);
    const pending = sourceOrder.some((key) => !sourceResults[key]);
    const statuses = sourceOrder.map((key) => {
      const result = sourceResults[key];
      return `${sourceNames[key]} ${!result ? "搜索中" : result.warnings.length ? "连接失败" : `${result.candidates.length} 条`}`;
    });
    $("#match-status").textContent = `${state.candidates.length ? `找到 ${state.candidates.length} 个候选结果` : pending ? "正在搜索" : "未找到候选结果，可尝试修改名称或只填写艺术家"} · ${statuses.join(" / ")}`;
    $("#match-results").innerHTML = state.candidates
      .map(
        (candidate, index) => {
          const difference = candidate.duration > 0 && trackDuration > 0 ? Math.abs(trackDuration - candidate.duration) : 0;
          const durationDiffers = difference > Math.max(90, trackDuration * 0.15);
          const meta =
            candidate.kind === "subtitle"
              ? [
                  candidate.year,
                  languageNames[candidate.language] || candidate.language,
                  candidate.duration ? duration(candidate.duration) : "",
                  candidate.cues ? `${candidate.cues} 条字幕` : "",
                  difference <= 2 && candidate.duration > 0 && trackDuration > 0 ? "与音频时长一致" : durationDiffers ? "时长相差较大" : "",
                  candidate.source,
                ]
                  .filter(Boolean)
                  .map((part) => escapeHtml(part))
                  .join(" · ")
              : `${escapeHtml(candidate.artist || "未知艺术家")} · ${escapeHtml(candidate.album || "未知专辑")}${candidate.duration ? ` · ${duration(candidate.duration)}` : ""}${durationDiffers ? " · 时长相差较大" : ""}<br>${escapeHtml(candidate.source)}${candidate.year ? ` · ${escapeHtml(candidate.year)}` : ""}`;
          return `<div class="match-result"><div><strong>${escapeHtml(candidate.title || "未知名称")}</strong><span>${meta}</span></div><button class="button subtle" data-candidate="${index}">选用</button></div>`;
        },
      )
      .join("");
    $$("[data-candidate]").forEach((button) =>
      button.addEventListener("click", () =>
        applyCandidate(state.candidates[Number(button.dataset.candidate)], renderMatches),
      ),
    );
  }
  renderMatches();
  await Promise.all(sourceOrder.map(async (source) => {
    const query = new URLSearchParams({ title, artist, source, requestId: operationId });
    try {
      sourceResults[source] = await api(`/api/online/search?${query}`);
    } catch {
      sourceResults[source] = { candidates: [], warnings: ["请求失败"] };
    }
    renderMatches();
  }));
};
async function applyCandidate(candidate, renderMatches) {
  const selectedId = selectedTracks()[0]?.id;
  const subtitle = candidate.kind === "subtitle";
  const skipLyrics = !subtitle && (selectedTracks()[0]?.duration || 0) >= LONG_AUDIO_SECONDS;
  const requestId = matchRequestId;
  matchApplying = true;
  $("#match-status").textContent = subtitle ? "正在下载字幕并转换为歌词…" : "正在获取封面和歌词…";
  $$("#match-results button").forEach((button) => {
    button.disabled = true;
  });
  try {
    const result = await api(
      "/api/online/details",
      jsonRequest("POST", { ...candidate, skipLyrics }),
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
    const missing = [
      !subtitle && !result.cover && "封面",
      !skipLyrics && candidate.kind !== "release" && !result.lyrics && "歌词",
    ].filter(Boolean);
    notify(
      subtitle
        ? `已用字幕填入歌词，时间轴来自影片字幕，请检查后再保存${missing.length ? `。未获取到${missing.join("和")}` : ""}${result.warnings.length ? `。${result.warnings.join("、")}` : ""}`
        : `已填入匹配结果，请检查后保存${missing.length ? `。未获取到${missing.join("和")}` : ""}${skipLyrics ? "。长音频未自动填入单曲歌词" : ""}${result.warnings.length ? `。${result.warnings.join("、")}` : ""}`,
      7000,
    );
  } catch (error) {
    if (requestId === matchRequestId && $("#match-dialog").open) {
      matchApplying = false;
      renderMatches();
      $("#match-status").textContent = `获取失败：${error.message}`;
    }
  }
}
$("#match-dialog").addEventListener("close", () => {
  matchRequestId++;
  matchApplying = false;
});

function updateRenamePreview() {
  const template = $("#rename-template").value;
  $("#rename-preview").innerHTML = selectedTracks()
    .map((track) => {
      const values = { ...track, filename: track.baseName };
      const extension = $("#fix-extension").checked ? track.detectedExtension : track.fileExtension;
      let name = template
        .replace(/%([a-zA-Z]+)%/g, (_, field) => values[field] || "")
        .replace(/[\x00-\x1f<>:"/\\|?*]/g, "_")
        .replace(/^[ .]+|[ .]+$/g, "");
      for (const suffix of [track.fileExtension, extension]) {
        if (suffix && name.toLowerCase().endsWith(suffix.toLowerCase())) {
          name = name.slice(0, -suffix.length).replace(/[ .]+$/g, "");
          break;
        }
      }
      return `<div>${escapeHtml(track.name)} <span>→</span> <b>${escapeHtml(name || "…")}${escapeHtml(extension)}</b></div>`;
    })
    .join("");
}
$("#rename-button").onclick = () => {
  if (dirty()) return notify("请先保存标签修改，再重命名");
  updateRenamePreview();
  $("#rename-dialog").showModal();
};
$("#rename-template").oninput = updateRenamePreview;
$("#fix-extension").onchange = updateRenamePreview;
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
        useDetectedExtension: $("#fix-extension").checked,
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
