import "./style.css";
import { sha256 } from "@noble/hashes/sha2.js";
import {
  createIcons,
  Cloud,
  Folder,
  FolderOpen,
  Clock3,
  ArrowUpFromLine,
  ShieldCheck,
  ArrowUpRight,
  LockKeyhole,
  RefreshCw,
  Plus,
  HardDrive,
  Files,
  CloudUpload,
  Search,
  X,
  Download,
  Upload,
  KeyRound,
  FileText,
  Image,
  FileArchive,
  File,
  Trash2,
  Pause,
  Play,
  Info,
  RotateCcw,
  Check,
} from "lucide";

const icons = {
  Cloud,
  Folder,
  FolderOpen,
  Clock3,
  ArrowUpFromLine,
  ShieldCheck,
  ArrowUpRight,
  LockKeyhole,
  RefreshCw,
  Plus,
  HardDrive,
  Files,
  CloudUpload,
  Search,
  X,
  Download,
  Upload,
  KeyRound,
  FileText,
  Image,
  FileArchive,
  File,
  Trash2,
  Pause,
  Play,
  Info,
  RotateCcw,
  Check,
};
const $ = <T extends HTMLElement = HTMLElement>(selector: string) =>
  document.querySelector<T>(selector)!;
const drawIcons = () => createIcons({ icons });
const esc = (value: string) =>
  value.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]!,
  );
const icon = (name: string) => `<i data-lucide="${name}"></i>`;
const bytes = (value: number) => {
  const unit =
    value < 1024
      ? 0
      : Math.min(3, Math.floor(Math.log(value) / Math.log(1024)));
  return `${Number((value / 1024 ** unit).toFixed(unit ? 1 : 0))} ${["B", "KB", "MB", "GB"][unit]}`;
};
const date = (value: string) =>
  new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(value));

interface StoredFile {
  id: string;
  name: string;
  size: number;
  media_type: string;
  status:
    "UPLOADING" | "FINALIZING" | "PROCESSING" | "READY" | "FAILED" | "EXPIRED";
  expected_sha256: string;
  sha256: string | null;
  created_at: string;
  updated_at: string;
  part_size: number;
  part_count: number;
  parts: { number: number; checksum: string; etag: string | null }[];
  result: { preview: string | null; preview_truncated?: boolean } | null;
  error: string | null;
}
interface Transfer {
  key: string;
  name: string;
  id?: string;
  paused: boolean;
  state: string;
  progress: number;
  controllers: Set<XMLHttpRequest>;
  workspace: string;
  fingerprint?: string;
}
const transfers = new Map<string, Transfer>();
let token = localStorage.getItem("clouddrive.access") || "";
let files: StoredFile[] = [];
let stats = { total: 0, ready: 0, bytes: 0, pending: 0 };
let connected = false;
let workspaceLabel = "";
let workspaceId = "";
let pollTimer: ReturnType<typeof setInterval> | undefined;
let view = "all";
let limit = 256 * 1024 * 1024;
let resumeTarget: string | undefined;
let refreshInFlight: string | undefined;
let selectedDetail: string | undefined;

class ApiError extends Error {
  constructor(
    message: string,
    readonly code?: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

function requestFingerprint(
  file: {
    sha256?: string | null;
    expected_sha256?: string | null;
    size: number;
    name: string;
  },
  workspace = workspaceId,
) {
  return `clouddrive.request.${workspace}.${file.expected_sha256 || file.sha256}.${file.size}.${file.name}`;
}

function forgetRequest(file: StoredFile, workspace: string) {
  localStorage.removeItem(requestFingerprint(file, workspace));
  // Retire fingerprints from the original unscoped browser implementation too.
  localStorage.removeItem(
    `clouddrive.request.${file.expected_sha256}.${file.size}.${file.name}`,
  );
}

async function api<T = any>(
  path: string,
  method = "GET",
  body?: unknown,
  accessKey = token,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    headers: {
      ...(accessKey ? { Authorization: `Bearer ${accessKey}` } : {}),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new ApiError(
      typeof error.detail === "string"
        ? error.detail
        : error.detail?.message || `Request failed (${response.status})`,
      error.detail?.code,
      response.status,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

let noticeTimer: ReturnType<typeof setTimeout>;
function notice(message: string, success = false) {
  clearTimeout(noticeTimer);
  $("#notice").textContent = message;
  $("#notice").className = success ? "success" : "";
  $("#notice").hidden = false;
  if (success)
    noticeTimer = setTimeout(() => ($("#notice").hidden = true), 7000);
}

function fileType(name: string) {
  const extension = name.split(".").pop()?.toLowerCase() || "";
  if (["jpg", "jpeg", "png", "gif", "webp", "svg"].includes(extension))
    return ["image", "image", extension.toUpperCase()];
  if (extension === "pdf") return ["pdf", "file-text", "PDF document"];
  if (["zip", "gz", "tar", "7z"].includes(extension))
    return ["archive", "file-archive", "Archive"];
  return [
    "text",
    "file-text",
    extension ? `${extension.toUpperCase()} file` : "File",
  ];
}

function action(id: string, name: string, label: string, iconName: string) {
  return `<button class="icon-button" data-action="${name}" data-id="${id}" aria-label="${esc(label)}" title="${esc(label)}">${icon(iconName)}</button>`;
}

function render() {
  $("#nav-count").textContent = String(stats.total);
  $("#pending-count").textContent = String(stats.pending);
  $("#stat-storage").textContent = bytes(stats.bytes);
  $("#stat-files").textContent =
    `${stats.ready} ${stats.ready === 1 ? "file" : "files"}`;
  $("#stat-pending").textContent =
    `${stats.pending} ${stats.pending === 1 ? "file" : "files"}`;
  const term = $<HTMLInputElement>("#search").value.toLocaleLowerCase();
  const selected = files.filter(
    (file) =>
      file.name.toLocaleLowerCase().includes(term) &&
      (view !== "uploads" || file.status !== "READY") &&
      (view !== "recent" ||
        Date.now() - new Date(file.created_at).getTime() < 7 * 86400000),
  );
  const sort = $<HTMLSelectElement>("#sort").value;
  selected.sort((a, b) =>
    sort === "name"
      ? a.name.localeCompare(b.name)
      : sort === "size"
        ? b.size - a.size
        : new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
  $("#list-count").textContent = String(selected.length);
  $("#file-list").innerHTML = selected
    .map((file) => {
      const [type, typeIcon, typeName] = fileType(file.name);
      const status = {
        READY: "Verified",
        UPLOADING: "Uploading",
        FINALIZING: "Finalizing",
        PROCESSING: "Verifying",
        FAILED: "Needs attention",
        EXPIRED: "Upload expired",
      }[file.status];
      return `<tr><td><div class="file-name"><span class="file-type ${type}">${icon(typeIcon)}</span><button data-action="detail" data-id="${file.id}" title="${esc(file.name)}"><strong>${esc(file.name)}</strong><small>${typeName}</small></button></div></td><td>${bytes(file.size)}</td><td>${date(file.created_at)}</td><td><span class="badge ${file.status.toLowerCase()}">${status}</span></td><td><div class="row-actions">${file.status === "READY" ? action(file.id, "download", `Download ${file.name}`, "download") : file.status === "UPLOADING" ? action(file.id, "resume", `Resume ${file.name}`, "play") : file.status === "EXPIRED" ? action(file.id, "restart", `Restart ${file.name}`, "rotate-ccw") : file.status === "FAILED" ? action(file.id, "retry", `Retry ${file.name}`, "rotate-ccw") : action(file.id, "detail", `Details for ${file.name}`, "info")}${action(file.id, "delete", `Delete ${file.name}`, "trash-2")}</div></td></tr>`;
    })
    .join("");
  $("#empty").hidden = selected.length > 0;
  $("#empty h3").textContent = files.length
    ? "Nothing here just yet"
    : "A fresh start for your files";
  $("#empty p").textContent = term
    ? "Try another file name or clear your search."
    : view === "uploads"
      ? "Files being uploaded or verified will appear here."
      : view === "recent"
        ? "Files added in the last seven days will appear here."
        : "Upload your first file. We’ll keep every byte in check.";
  $("#first-upload").hidden = files.length > 0;
  $("#file-summary").textContent =
    `${selected.length} of ${stats.total} files shown${stats.total > 200 ? " · Browsing the latest 200" : ""}`;
  renderTransfers();
  drawIcons();
}

function renderTransfers() {
  $("#transfer-panel").hidden = transfers.size === 0;
  $("#transfer-panel").innerHTML = [...transfers.values()]
    .map(
      (transfer) =>
        `<div class="transfer"><div class="transfer-top"><strong>${esc(transfer.name)}</strong><span class="transfer-status">${esc(transfer.state)}${!transfer.paused ? `<button data-pause="${esc(transfer.key)}" aria-label="Pause ${esc(transfer.name)}">Pause</button>` : ""}</span></div><div class="transfer-track"><span style="width:${transfer.progress}%"></span></div></div>`,
    )
    .join("");
}

async function refresh() {
  const accessKey = token;
  if (refreshInFlight === accessKey || !accessKey) return;
  refreshInFlight = accessKey;
  try {
    const result = await api("/files", "GET", undefined, accessKey);
    if (token !== accessKey) return;
    files = result.files;
    stats = result.stats;
    $("#workspace-id").textContent = workspaceLabel;
    render();
  } finally {
    if (refreshInFlight === accessKey) refreshInFlight = undefined;
  }
}

function assertActive(transfer: Transfer) {
  if (transfer.paused || transfer.workspace !== workspaceId)
    throw new DOMException("Paused", "AbortError");
}
function hex(value: Uint8Array) {
  return [...value].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
function base64(value: Uint8Array) {
  return btoa(String.fromCharCode(...value));
}

async function hashFile(file: File, transfer: Transfer) {
  const hash = sha256.create();
  for (let offset = 0; offset < file.size; offset += 8 * 1024 * 1024) {
    assertActive(transfer);
    hash.update(
      new Uint8Array(
        await file.slice(offset, offset + 8 * 1024 * 1024).arrayBuffer(),
      ),
    );
    transfer.state = `Checking file · ${Math.min(100, Math.round(((offset + 8 * 1024 * 1024) / file.size) * 100))}%`;
    renderTransfers();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assertActive(transfer);
  return hex(hash.digest());
}

function putPart(
  url: string,
  headers: Record<string, string>,
  body: Blob,
  transfer: Transfer,
  progress: (bytes: number) => void,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    transfer.controllers.add(xhr);
    xhr.open("PUT", url);
    xhr.timeout = 120000;
    Object.entries(headers).forEach(([key, value]) =>
      xhr.setRequestHeader(key, value),
    );
    xhr.upload.onprogress = (event) => progress(event.loaded);
    xhr.onload = () => {
      transfer.controllers.delete(xhr);
      if (xhr.status < 200 || xhr.status >= 300)
        return reject(
          new Error(
            `Storage upload failed (${xhr.status}). Select the file again to resume.`,
          ),
        );
      const etag = xhr.getResponseHeader("ETag");
      if (!etag)
        return reject(
          new Error(
            "Storage did not expose its part receipt. Check storage CORS settings.",
          ),
        );
      resolve(etag);
    };
    xhr.onerror = xhr.ontimeout = () => {
      transfer.controllers.delete(xhr);
      reject(
        new Error(
          "Upload connection interrupted. Select the same file to resume.",
        ),
      );
    };
    xhr.onabort = () => {
      transfer.controllers.delete(xhr);
      reject(new DOMException("Paused", "AbortError"));
    };
    assertActive(transfer);
    xhr.send(body);
  });
}

async function upload(file: File, target?: string) {
  if (!file.size) {
    notice(
      "Empty files are not supported. Choose a file with at least one byte.",
    );
    return;
  }
  if (file.size > limit) {
    notice(
      `${file.name} exceeds this workspace’s ${bytes(limit)} per-file limit.`,
    );
    return;
  }
  const accessKey = token;
  const uploadWorkspace = workspaceId;
  const uploadApi = (path: string, method = "GET", body?: unknown) =>
    api(path, method, body, accessKey);
  const transfer: Transfer = {
    workspace: uploadWorkspace,
    key: crypto.randomUUID(),
    name: file.name,
    paused: false,
    state: "Checking file…",
    progress: 0,
    controllers: new Set(),
  };
  transfers.set(transfer.key, transfer);
  renderTransfers();
  try {
    const checksum = await hashFile(file, transfer);
    const existing = target
      ? files.find((item) => item.id === target)
      : files.find(
          (item) =>
            (item.status === "UPLOADING" || item.status === "EXPIRED") &&
            item.name === file.name &&
            item.size === file.size &&
            item.expected_sha256 === checksum,
        );
    if (
      target &&
      (!existing ||
        existing.size !== file.size ||
        existing.expected_sha256 !== checksum)
    )
      throw new Error(
        "This is a different file. To resume, select the original file with matching contents.",
      );
    const fingerprint = requestFingerprint(
      { sha256: checksum, size: file.size, name: file.name },
      uploadWorkspace,
    );
    transfer.fingerprint = fingerprint;
    if (
      [...transfers.values()].some(
        (other) =>
          other.key !== transfer.key &&
          other.workspace === uploadWorkspace &&
          other.fingerprint === fingerprint &&
          !other.paused,
      )
    )
      throw new Error("This file already has an active upload.");
    assertActive(transfer);
    let requestId = localStorage.getItem(fingerprint) || crypto.randomUUID();
    localStorage.setItem(fingerprint, requestId);
    const initiate = () =>
      uploadApi("/uploads", "POST", {
        request_id: requestId,
        name: file.name,
        size: file.size,
        media_type: file.type || "application/octet-stream",
        sha256: checksum,
      });
    let session: StoredFile;
    try {
      session = existing
        ? await uploadApi(`/uploads/${existing.id}`)
        : await initiate();
    } catch (error) {
      if (
        !(error instanceof ApiError) ||
        !(error.code === "upload_deleted" || (existing && error.status === 404))
      )
        throw error;
      assertActive(transfer);
      requestId = crypto.randomUUID();
      localStorage.setItem(fingerprint, requestId);
      session = await initiate();
    }
    assertActive(transfer);
    if (session.status === "EXPIRED") {
      // The user reselected content whose full hash matches the expired session.
      // Expired bytes cannot be resumed; delete the old session and start afresh.
      await uploadApi(`/files/${session.id}`, "DELETE");
      assertActive(transfer);
      forgetRequest(session, uploadWorkspace);
      requestId = crypto.randomUUID();
      localStorage.setItem(fingerprint, requestId);
      session = await initiate();
      assertActive(transfer);
    }
    transfer.id = session.id;
    if (
      [...transfers.values()].some(
        (other) =>
          other.key !== transfer.key &&
          other.id === session.id &&
          !other.paused,
      )
    )
      throw new Error("This file already has an active upload.");
    if (session.status === "UPLOADING") {
      const partProgress = new Map<number, number>();
      session.parts
        .filter((part) => part.etag)
        .forEach((part) =>
          partProgress.set(
            part.number,
            Math.min(
              session.part_size,
              file.size - (part.number - 1) * session.part_size,
            ),
          ),
        );
      const update = () => {
        transfer.progress = Math.min(
          100,
          ([...partProgress.values()].reduce((sum, value) => sum + value, 0) /
            file.size) *
            100,
        );
        transfer.state = `Uploading · ${Math.round(transfer.progress)}%`;
        renderTransfers();
      };
      update();
      await refresh();
      const queue = Array.from(
        { length: session.part_count },
        (_, index) => index + 1,
      ).filter(
        (number) =>
          !session.parts.some((part) => part.number === number && part.etag),
      );
      const send = async () => {
        while (queue.length) {
          assertActive(transfer);
          const number = queue.shift()!;
          const chunk = file.slice(
            (number - 1) * session.part_size,
            Math.min(number * session.part_size, file.size),
          );
          const partChecksum = base64(
            sha256(new Uint8Array(await chunk.arrayBuffer())),
          );
          assertActive(transfer);
          const signed = await uploadApi(
            `/uploads/${session.id}/parts/${number}/sign`,
            "POST",
            { checksum: partChecksum },
          );
          if (!signed.uploaded) {
            const etag = await putPart(
              signed.url,
              signed.headers,
              chunk,
              transfer,
              (loaded) => {
                partProgress.set(number, loaded);
                update();
              },
            );
            assertActive(transfer);
            await uploadApi(`/uploads/${session.id}/parts/${number}`, "PUT", {
              checksum: partChecksum,
              etag,
            });
          }
          partProgress.set(number, chunk.size);
          update();
        }
      };
      const workers = Array.from({ length: Math.min(3, queue.length) }, send);
      // Wait for all part loops before permitting another transfer of this file.
      const results = await Promise.allSettled(workers);
      const failed = results.find((result) => result.status === "rejected");
      if (failed?.status === "rejected") throw failed.reason;
    }
    assertActive(transfer);
    transfer.state = "Finishing upload…";
    renderTransfers();
    session = await uploadApi(`/uploads/${session.id}/complete`, "POST");
    assertActive(transfer);
    if (session.status === "EXPIRED")
      throw new Error(
        "Upload expired in storage. Select the original file to restart the upload.",
      );
    localStorage.removeItem(fingerprint);
    transfers.delete(transfer.key);
    notice(
      `${file.name} uploaded. Background verification is ${session.status === "READY" ? "complete" : "in progress"}.`,
      true,
    );
    await refresh();
  } catch (error) {
    transfer.controllers.forEach((controller) => controller.abort());
    transfer.paused = true;
    transfer.state =
      error instanceof DOMException && error.name === "AbortError"
        ? "Paused · Select file to resume"
        : "Interrupted · Select file to resume";
    renderTransfers();
    if (!(error instanceof DOMException && error.name === "AbortError"))
      notice(
        error instanceof Error
          ? error.message
          : "Upload interrupted. Select the same file to resume.",
      );
    setTimeout(() => {
      transfers.delete(transfer.key);
      renderTransfers();
    }, 12000);
    await refresh().catch(() => {});
  }
}

function openDetail(file: StoredFile) {
  selectedDetail = file.id;
  const [type, typeIcon] = fileType(file.name);
  $("#detail-content").innerHTML =
    `<div class="dialog-heading"><span class="file-type ${type}">${icon(typeIcon)}</span><button class="icon-button" data-close aria-label="Close file details">${icon("x")}</button></div><h2>${esc(file.name)}</h2><div class="detail-meta"><div><span>FILE SIZE</span><strong>${bytes(file.size)}</strong></div><div><span>STATUS</span><strong>${{ READY: "Verified & ready", UPLOADING: "Waiting for upload", FINALIZING: "Finalizing upload", PROCESSING: "Verifying integrity", FAILED: "Verification failed", EXPIRED: "Upload expired" }[file.status]}</strong></div><div><span>ADDED</span><strong>${date(file.created_at)}</strong></div><div><span>UPLOAD PARTS</span><strong>${file.parts.filter((part) => part.etag).length} of ${file.part_count} confirmed</strong></div></div>${file.error ? `<p class="danger">${esc(file.error)}</p>` : ""}<div class="eyebrow">${file.status === "READY" ? "VERIFIED SHA-256" : file.sha256 ? "COMPUTED SHA-256" : "EXPECTED SHA-256"}</div><p class="hash">${esc(file.sha256 || file.expected_sha256)}</p>${file.status === "READY" && file.result?.preview !== null && file.result?.preview !== undefined ? `<div class="eyebrow">TEXT PREVIEW${file.result.preview_truncated ? " · FIRST 2,000 CHARACTERS" : ""}</div><pre class="preview">${esc(file.result.preview)}</pre>` : file.status === "UPLOADING" ? "<p>Choose Resume and select the same file. Confirmed parts will be kept.</p>" : ""}<div class="detail-actions">${file.status === "READY" ? `<button class="primary" data-action="download" data-id="${file.id}">${icon("download")}Download file</button>` : file.status === "UPLOADING" ? `<button class="primary" data-action="resume" data-id="${file.id}">${icon("play")}Resume upload</button>` : file.status === "FINALIZING" ? `<button class="primary" data-action="complete" data-id="${file.id}">Retry finalization</button>` : file.status === "EXPIRED" ? `<button class="primary" data-action="restart" data-id="${file.id}">Restart upload</button>` : file.status === "FAILED" ? `<button class="primary" data-action="retry" data-id="${file.id}">Retry verification</button>` : ""}<button class="secondary danger" data-action="delete" data-id="${file.id}">${icon("trash-2")}Delete</button></div>`;
  drawIcons();
  if (!$<HTMLDialogElement>("#detail-dialog").open)
    $<HTMLDialogElement>("#detail-dialog").showModal();
}

function chooseFiles(target?: string) {
  resumeTarget = target;
  $<HTMLInputElement>("#file-input").multiple = !target;
  $<HTMLInputElement>("#file-input").click();
}
for (const selector of ["#upload-button", "#browse-button", "#first-upload"])
  $(selector).addEventListener("click", () => chooseFiles());
$("#file-input").addEventListener("change", async (event) => {
  const input = event.target as HTMLInputElement;
  const chosen = [...(input.files || [])];
  const target = resumeTarget;
  resumeTarget = undefined;
  input.value = "";
  const batchWorkspace = workspaceId;
  for (const file of chosen) {
    if (workspaceId !== batchWorkspace) break;
    await upload(file, target);
  }
});
$("#search").addEventListener("input", render);
$("#sort").addEventListener("change", render);
$("#refresh").addEventListener("click", () =>
  (connected ? refresh() : initialize()).catch((error) =>
    notice(error.message),
  ),
);
document.querySelectorAll<HTMLElement>("[data-view]").forEach((button) =>
  button.addEventListener("click", () => {
    view = button.dataset.view!;
    document
      .querySelectorAll("[data-view]")
      .forEach((item) => item.classList.toggle("selected", item === button));
    const title = { all: "My files", recent: "Recent", uploads: "Uploads" }[
      view
    ]!;
    $("#breadcrumb-view").textContent = title;
    $("#list-title").textContent = {
      all: "All files",
      recent: "Added this week",
      uploads: "Uploads & processing",
    }[view]!;
    render();
  }),
);
$("#transfer-panel").addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLElement>(
    "[data-pause]",
  );
  const transfer = button && transfers.get(button.dataset.pause!);
  if (transfer) {
    transfer.paused = true;
    transfer.controllers.forEach((controller) => controller.abort());
    transfer.state = "Pausing…";
    renderTransfers();
  }
});
document.addEventListener("click", async (event) => {
  const target = event.target as HTMLElement;
  if (target.closest("[data-close]")) {
    target.closest("dialog")?.close();
    return;
  }
  const button = target.closest<HTMLButtonElement>("[data-action]");
  if (!button) return;
  const file = files.find((file) => file.id === button.dataset.id);
  if (!file) return;
  const action = button.dataset.action;
  const actionKey = token;
  const actionWorkspace = workspaceId;
  const fileApi = (path: string, method = "GET") =>
    api(path, method, undefined, actionKey);
  try {
    if (action === "detail") return openDetail(file);
    if (action === "resume" || action === "restart") {
      $<HTMLDialogElement>("#detail-dialog").close();
      chooseFiles(file.id);
      return;
    }
    if (action === "download") {
      const result = await fileApi(`/files/${file.id}/download`);
      if (token !== actionKey) return;
      const anchor = document.createElement("a");
      anchor.href = result.url;
      anchor.rel = "noreferrer";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      return;
    }
    if (action === "delete") {
      if (
        !confirm(
          `Delete “${file.name}”? This removes the stored file and any unfinished upload.`,
        )
      )
        return;
      for (const transfer of transfers.values())
        if (transfer.id === file.id) {
          transfer.paused = true;
          transfer.controllers.forEach((controller) => controller.abort());
        }
      await fileApi(`/files/${file.id}`, "DELETE");
      forgetRequest(file, actionWorkspace);
      if (token !== actionKey) return;
      $<HTMLDialogElement>("#detail-dialog").close();
      notice(`${file.name} deleted.`, true);
    }
    if (action === "retry") await fileApi(`/files/${file.id}/retry`, "POST");
    if (action === "complete")
      await fileApi(`/uploads/${file.id}/complete`, "POST");
    if (token !== actionKey) return;
    await refresh();
    if (
      $<HTMLDialogElement>("#detail-dialog").open &&
      selectedDetail === file.id
    )
      openDetail(files.find((item) => item.id === file.id)!);
  } catch (error) {
    if (token !== actionKey) return;
    await refresh().catch(() => {});
    notice((error as Error).message);
  }
});

const drop = $("#drop-zone");
for (const type of ["dragenter", "dragover"])
  drop.addEventListener(type, (event) => {
    event.preventDefault();
    drop.classList.add("dragging");
  });
drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
drop.addEventListener("drop", async (event) => {
  event.preventDefault();
  drop.classList.remove("dragging");
  const batchWorkspace = workspaceId;
  for (const file of [...((event as DragEvent).dataTransfer?.files || [])]) {
    if (workspaceId !== batchWorkspace) break;
    await upload(file);
  }
});
for (const selector of ["#workspace-settings", "#workspace-settings-mobile"])
  $(selector).addEventListener("click", () =>
    $<HTMLDialogElement>("#access-dialog").showModal(),
  );
$("#export-key").addEventListener("click", () => {
  const url = URL.createObjectURL(
    new Blob(
      [
        JSON.stringify(
          { application: "clouddrive", version: 1, access_key: token },
          null,
          2,
        ),
      ],
      { type: "application/json" },
    ),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "clouddrive-access-key.json";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
$("#import-key").addEventListener("click", () =>
  $<HTMLInputElement>("#key-input").click(),
);
$("#key-input").addEventListener("change", async (event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  try {
    if ([...transfers.values()].some((transfer) => !transfer.paused))
      throw new Error("Pause active uploads before switching workspaces.");
    if (file.size > 4096)
      throw new Error("This is not a workspace access file.");
    const data = JSON.parse(await file.text());
    if (
      data.application !== "clouddrive" ||
      typeof data.access_key !== "string" ||
      data.access_key.length > 200
    )
      throw new Error("Invalid CloudDrive access file.");
    const workspace = await api(
      "/workspace",
      "GET",
      undefined,
      data.access_key,
    );
    transfers.clear();
    files = [];
    resumeTarget = undefined;
    selectedDetail = undefined;
    $<HTMLDialogElement>("#detail-dialog").close();
    token = data.access_key;
    localStorage.setItem("clouddrive.access", token);
    workspaceId = workspace.id;
    workspaceLabel = `Workspace ${workspace.id.slice(0, 8)}`;
    $("#workspace-id").textContent = workspaceLabel;
    limit = workspace.max_file_bytes;
    $("#upload-limit").textContent =
      `Resumable uploads · Up to ${bytes(limit)} per file`;
    $<HTMLDialogElement>("#access-dialog").close();
    await refresh();
    notice("Workspace opened. Your access key is saved in this browser.", true);
  } catch (error) {
    notice((error as Error).message);
    $<HTMLDialogElement>("#access-dialog").close();
  }
});
for (const dialog of document.querySelectorAll("dialog"))
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        dialog.close();
    }
  });

async function initialize() {
  try {
    if (!token) {
      const created = await api("/workspaces", "POST");
      token = created.access_key;
      localStorage.setItem("clouddrive.access", token);
    }
    const workspace = await api("/workspace");
    limit = workspace.max_file_bytes;
    workspaceId = workspace.id;
    workspaceLabel = `Workspace ${workspace.id.slice(0, 8)}`;
    $("#workspace-id").textContent = workspaceLabel;
    $("#upload-limit").textContent =
      `Resumable uploads · Up to ${bytes(limit)} per file`;
    await refresh();
    connected = true;
    if (!pollTimer)
      pollTimer = setInterval(
        () =>
          refresh().catch(() => {
            $("#workspace-id").textContent = "Connection interrupted";
          }),
        3000,
      );
  } catch (error) {
    notice((error as Error).message);
    $("#workspace-id").textContent = "Could not connect";
  }
}
drawIcons();
void initialize();
