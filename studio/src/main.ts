import "@fontsource-variable/instrument-sans";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "@fontsource/ibm-plex-mono/latin-600.css";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { open } from "@tauri-apps/plugin-dialog";
import icon from "../src-tauri/icons/icon.png";
import "./styles.css";

type Source = { kind: "file" | "url"; location: string };
type Metadata = {
  bytes: number | null;
  seconds: number | null;
  width: number | null;
  height: number | null;
  has_audio: boolean;
  has_video: boolean;
};
type Draft = {
  source: Source;
  label: string;
  metadata?: Metadata;
  error?: string;
};
type Job = {
  id: number;
  batch: number;
  source: Source;
  label: string;
  preset: "mp4" | "mp3";
  folder: string;
  status: string;
  progress: number | null;
  metadata: Metadata | null;
  output: string | null;
  output_bytes: number | null;
  error: string | null;
  created_at: number;
};
type Details =
  | { kind: "draft"; item: Draft }
  | { kind: "job"; id: number }
  | null;

function byId<T extends HTMLElement>(id: string) {
  return document.getElementById(id) as T;
}
function element<T extends keyof HTMLElementTagNameMap>(
  tag: T,
  text = "",
  className = "",
) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(text: string, style: string, action: () => void) {
  const node = element("button", text, style);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

const labels: Record<string, string> = {
  queued: "Na fila",
  inspecting: "Inspecionando",
  downloading: "Baixando",
  converting: "Convertendo",
  saving: "Salvando",
  cancelling: "Cancelando",
  done: "Concluído",
  failed: "Falhou",
  cancelled: "Cancelado",
  interrupted: "Interrompido",
};
const terminal = new Set(["done", "failed", "cancelled", "interrupted"]);
const drafts: Draft[] = [];
let jobs: Job[] = [];
let details: Details = null;
let folder = "";
let view: "active" | "history" = "active";
let toolsReady = false;
let pending = false;
let refreshing = false;
let refreshAgain = false;
let detailsKey = "";
const draftList = byId<HTMLUListElement>("sources");
const jobList = byId<HTMLUListElement>("jobs");
const queueButton = byId<HTMLButtonElement>("queue");
const detailsPanel = byId<HTMLDivElement>("details");
const link = byId<HTMLInputElement>("link");

byId<HTMLImageElement>("brand-icon").src = icon;
const theme = byId<HTMLSelectElement>("theme");
const system = matchMedia("(prefers-color-scheme: dark)");
theme.value = localStorage.getItem("theme") ?? "system";
if (!theme.value) theme.value = "system";
function applyTheme() {
  document.documentElement.dataset.theme =
    theme.value === "system"
      ? system.matches
        ? "dark"
        : "light"
      : theme.value;
}
applyTheme();
theme.addEventListener("change", () => {
  localStorage.setItem("theme", theme.value);
  applyTheme();
});
system.addEventListener("change", applyTheme);

function notice(message: string, error = false) {
  const node = byId("notice");
  node.textContent = message;
  node.dataset.error = String(error);
}
function failure(error: unknown) {
  notice(error instanceof Error ? error.message : String(error), true);
}
function fileName(location: string) {
  return location.split(/[/\\]/).filter(Boolean).at(-1) || location;
}
function sourceName(source: Source) {
  if (source.kind === "file") return fileName(source.location);
  const url = new URL(source.location);
  return fileName(url.pathname) === "/" ? url.hostname : fileName(url.pathname);
}
function size(bytes: number) {
  const unit =
    bytes >= 1024 ** 3
      ? "GB"
      : bytes >= 1024 ** 2
        ? "MB"
        : bytes >= 1024
          ? "KB"
          : "B";
  const divisor = 1024 ** ["B", "KB", "MB", "GB"].indexOf(unit);
  return `${new Intl.NumberFormat("pt-BR", { maximumFractionDigits: unit === "B" ? 0 : 1 }).format(bytes / divisor)} ${unit}`;
}
function duration(seconds: number) {
  const rounded = Math.round(seconds);
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
}

function updateComposer() {
  byId("input-count").textContent = drafts.length
    ? `${drafts.length} ${drafts.length === 1 ? "item" : "itens"}`
    : "";
  queueButton.disabled = !toolsReady || !drafts.length || !folder || pending;
  queueButton.textContent = pending ? "Adicionando…" : "Adicionar à fila";
  const pick = byId<HTMLButtonElement>("folder");
  pick.textContent = folder ? fileName(folder) : "Escolher pasta…";
  pick.title = folder;
}

function select(next: Details) {
  details = next;
  for (const node of document.querySelectorAll<HTMLButtonElement>(
    ".source-select",
  )) {
    node.setAttribute(
      "aria-current",
      String(
        details?.kind === "draft" &&
          drafts[Number(node.dataset.index)] === details.item,
      ),
    );
  }
  for (const node of document.querySelectorAll<HTMLButtonElement>(
    ".job-select",
  )) {
    node.setAttribute(
      "aria-current",
      String(details?.kind === "job" && details.id === Number(node.dataset.id)),
    );
  }
  renderDetails();
}

function renderDrafts() {
  draftList.replaceChildren(
    ...drafts.map((item, index) => {
      const row = element("li", "", "source");
      const title = button(item.label, "source-select", () =>
        select({ kind: "draft", item }),
      );
      title.dataset.index = String(index);
      title.title = item.label;
      const remove = button("×", "quiet source-remove", () => {
        drafts.splice(drafts.indexOf(item), 1);
        if (details?.kind === "draft" && details.item === item) details = null;
        renderDrafts();
        (
          draftList.querySelectorAll<HTMLButtonElement>(".source-select")[
            Math.min(index, drafts.length - 1)
          ] ?? byId("browse")
        ).focus();
      });
      remove.setAttribute("aria-label", `Remover ${item.label}`);
      row.append(title, remove);
      return row;
    }),
  );
  updateComposer();
  select(details);
}

const inspections: Draft[] = [];
let inspecting = false;
async function inspectDrafts() {
  if (inspecting) return;
  inspecting = true;
  try {
    while (inspections.length) {
      const item = inspections.shift()!;
      if (!drafts.includes(item)) continue;
      try {
        item.metadata = await invoke<Metadata>("inspect_source", {
          source: item.source,
        });
      } catch (error) {
        item.error = String(error);
      }
      renderDetails();
    }
  } finally {
    inspecting = false;
  }
}

function importSources(sources: Source[]) {
  const added: Draft[] = [];
  for (const source of sources) {
    if (drafts.length >= 100) {
      notice("O limite é de 100 itens por lote.", true);
      break;
    }
    if (
      drafts.some(
        (item) =>
          item.source.kind === source.kind &&
          item.source.location === source.location,
      )
    )
      continue;
    const item = { source, label: sourceName(source) };
    drafts.push(item);
    added.push(item);
    if (toolsReady && source.kind === "file") inspections.push(item);
  }
  if (added.length) details = { kind: "draft", item: added[0] };
  renderDrafts();
  void inspectDrafts();
}

function detailField(list: HTMLDListElement, title: string, value: string) {
  list.append(element("dt", title), element("dd", value));
}
function renderDetails() {
  const item =
    details?.kind === "draft"
      ? details.item
      : details?.kind === "job"
        ? jobs.find((job) => details?.kind === "job" && job.id === details.id)
        : undefined;
  const job = item && "id" in item ? item : undefined;
  const signature =
    JSON.stringify(item && { ...item, progress: undefined }) ?? "empty";
  if (signature === detailsKey) return;
  detailsKey = signature;
  if (!item) {
    detailsPanel.replaceChildren(
      element(
        "p",
        "Selecione um arquivo ou trabalho para inspecionar.",
        "muted",
      ),
    );
    return;
  }
  const subtitle = job
    ? labels[job.status]
    : item.source.kind === "file"
      ? "Arquivo local"
      : "Link direto";
  const title = element("strong", item.label, "detail-title");
  const info = element("p", subtitle, "muted");
  const list = element("dl");
  const meta = item.metadata;
  if (meta?.seconds) detailField(list, "Duração", duration(meta.seconds));
  if (meta?.width && meta.height)
    detailField(list, "Resolução", `${meta.width} × ${meta.height}`);
  if (meta?.bytes != null)
    detailField(list, "Tamanho de origem", size(meta.bytes));
  if (meta?.has_audio) detailField(list, "Faixa de áudio", "Disponível");
  if (!meta && !item.error)
    detailField(
      list,
      "Informações da mídia",
      item.source.kind === "url"
        ? "Após baixar o arquivo"
        : "Aguardando inspeção",
    );
  if (job) detailField(list, "Lote", `#${job.batch}`);
  if (job?.output_bytes != null)
    detailField(list, "Tamanho da saída", size(job.output_bytes));
  if (job?.output) {
    detailField(list, "Arquivo exportado", fileName(job.output));
    (list.lastElementChild as HTMLElement).title = job.output;
  }
  const nodes: HTMLElement[] = [title, info, list];
  if (item.error) nodes.push(element("p", item.error, "detail-error"));
  if (job && terminal.has(job.status)) {
    const actions = element("div", "", "detail-actions");
    actions.append(
      button(
        "Repetir lote",
        "secondary",
        () => void act("repeat_batch", job.id),
      ),
    );
    nodes.push(actions);
  }
  detailsPanel.replaceChildren(...nodes);
}

const rows = new Map<
  number,
  {
    row: HTMLLIElement;
    choose: HTMLButtonElement;
    title: HTMLElement;
    state: HTMLElement;
    bar: HTMLProgressElement;
    action: HTMLButtonElement;
  }
>();
function renderJobs() {
  const active = jobs.filter((job) => !terminal.has(job.status));
  const shown =
    view === "active"
      ? [...active].reverse()
      : jobs.filter((job) => terminal.has(job.status));
  const displayed = new Set(shown.map((job) => job.id));
  for (const [id, record] of rows)
    if (!displayed.has(id)) {
      record.row.remove();
      rows.delete(id);
    }
  let position: ChildNode | null = jobList.firstChild;
  for (const job of shown) {
    let record = rows.get(job.id);
    if (!record) {
      const row = element("li", "", "job");
      const choose = button("", "job-select", () =>
        select({ kind: "job", id: job.id }),
      );
      choose.dataset.id = String(job.id);
      const format = element("span", job.preset.toUpperCase(), "format");
      const info = element("span");
      const title = element("span", "", "job-label");
      const state = element("span", "", "job-status");
      const bar = element("progress");
      bar.max = 1;
      info.append(title, state, bar);
      choose.append(format, info);
      const action = button("", "secondary job-action", () => {
        const current = jobs.find((item) => item.id === job.id);
        if (current)
          void act(
            current.status === "done"
              ? "open_output"
              : terminal.has(current.status)
                ? "retry_job"
                : "cancel_job",
            current.id,
          );
      });
      row.append(choose, action);
      record = { row, choose, title, state, bar, action };
      rows.set(job.id, record);
    }
    if (position !== record.row) jobList.insertBefore(record.row, position);
    position = record.row.nextSibling;
    record.title.textContent = job.label;
    record.choose.title = job.label;
    record.state.textContent =
      labels[job.status] +
      (job.progress != null && !terminal.has(job.status)
        ? ` · ${Math.round(job.progress * 100)}%`
        : "");
    record.state.dataset.state = job.status;
    record.bar.hidden = !["downloading", "converting"].includes(job.status);
    if (job.progress == null) record.bar.removeAttribute("value");
    else record.bar.value = job.progress;
    record.bar.setAttribute("aria-label", `Progresso de ${job.label}`);
    record.action.textContent =
      job.status === "done"
        ? "Abrir"
        : terminal.has(job.status)
          ? "Tentar de novo"
          : "Cancelar";
    record.action.disabled = job.status === "cancelling";
    record.action.setAttribute(
      "aria-label",
      `${record.action.textContent} ${job.label}`,
    );
  }
  byId("jobs-empty").hidden = shown.length > 0;
  byId("empty-title").textContent =
    view === "active" ? "A fila está vazia" : "Nenhum trabalho no histórico";
  byId("empty-description").textContent =
    view === "active"
      ? jobs.length
        ? "Consulte o Histórico para ver os resultados."
        : "Adicione um arquivo ou link para começar."
      : "Os resultados e as falhas aparecem aqui.";
  const historyCount = jobs.length - active.length;
  const historyTab = document.querySelector<HTMLButtonElement>(
    '[data-view="history"]',
  )!;
  historyTab.textContent = historyCount
    ? `Histórico (${historyCount})`
    : "Histórico";
  byId("activity").textContent = active.length
    ? `${active.length} na fila`
    : "";
  select(details);
}

async function refresh() {
  if (refreshing) {
    refreshAgain = true;
    return;
  }
  refreshing = true;
  try {
    do {
      refreshAgain = false;
      jobs = await invoke<Job[]>("list_jobs");
      renderJobs();
    } while (refreshAgain);
  } catch (error) {
    failure(error);
  } finally {
    refreshing = false;
  }
}
function switchView(next: "active" | "history") {
  view = next;
  for (const tab of document.querySelectorAll<HTMLButtonElement>("[data-view]"))
    tab.setAttribute("aria-pressed", String(tab.dataset.view === view));
  renderJobs();
}
async function act(command: string, id: number) {
  try {
    await invoke(command, { id });
    if (command === "retry_job" || command === "repeat_batch")
      switchView("active");
    await refresh();
  } catch (error) {
    failure(error);
  }
}

byId("browse").addEventListener("click", async () => {
  try {
    const paths = await open({
      multiple: true,
      title: "Importar vídeo ou áudio",
      filters: [
        {
          name: "Vídeo e áudio",
          extensions: [
            "mp4",
            "mkv",
            "mov",
            "webm",
            "avi",
            "m4v",
            "wmv",
            "flv",
            "ts",
            "mp3",
            "wav",
            "m4a",
            "aac",
            "flac",
            "ogg",
            "opus",
            "wma",
          ],
        },
      ],
    });
    if (paths)
      importSources(
        (Array.isArray(paths) ? paths : [paths]).map((location) => ({
          kind: "file",
          location,
        })),
      );
  } catch (error) {
    failure(error);
  }
});
byId("sample").addEventListener("click", async () => {
  try {
    importSources([
      { kind: "file", location: await invoke<string>("sample_path") },
    ]);
  } catch (error) {
    failure(error);
  }
});
byId("link-form").addEventListener("submit", (event) => {
  event.preventDefault();
  try {
    const url = new URL(link.value.trim());
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password
    )
      throw new Error("Use um link HTTP ou HTTPS sem credenciais no endereço.");
    importSources([{ kind: "url", location: url.href }]);
    link.value = "";
  } catch (error) {
    failure(error);
  }
});
byId("folder").addEventListener("click", async () => {
  try {
    const selected = await open({
      directory: true,
      title: "Salvar arquivos exportados",
    });
    if (typeof selected === "string") {
      folder = selected;
      updateComposer();
    }
  } catch (error) {
    failure(error);
  }
});
queueButton.addEventListener("click", async () => {
  pending = true;
  updateComposer();
  const batch = [...drafts];
  try {
    await invoke("add_jobs", {
      sources: batch.map((item) => item.source),
      preset: byId<HTMLSelectElement>("preset").value,
      folder,
    });
    for (const item of batch) {
      const index = drafts.indexOf(item);
      if (index >= 0) drafts.splice(index, 1);
    }
    details = null;
    notice("Lote adicionado à fila.");
    renderDrafts();
    switchView("active");
    await refresh();
  } catch (error) {
    failure(error);
  } finally {
    pending = false;
    updateComposer();
  }
});
for (const tab of document.querySelectorAll<HTMLButtonElement>("[data-view]"))
  tab.addEventListener("click", () =>
    switchView(tab.dataset.view === "history" ? "history" : "active"),
  );

async function start() {
  try {
    const config = await invoke<{ folder: string; tools_ready: boolean }>(
      "settings",
    );
    folder = config.folder;
    toolsReady = config.tools_ready;
    byId("tool-warning").hidden = toolsReady;
    updateComposer();
    await refresh();
    setInterval(() => {
      if (jobs.some((job) => !terminal.has(job.status))) void refresh();
    }, 500);
    const drop = byId("drop-zone");
    await getCurrentWebview().onDragDropEvent((event) => {
      drop.dataset.hover = String(
        ["enter", "over"].includes(event.payload.type),
      );
      if (event.payload.type === "drop")
        importSources(
          event.payload.paths.map((location) => ({ kind: "file", location })),
        );
    });
  } catch (error) {
    failure(error);
  }
}
void start();
