"""
Baixador de Mídia 4.0
Downloads em lote, simultâneos, com saída em pasta ou arquivo ZIP.

Dependências:  pip install -r requirements.txt
Executar:      python baixador_midia.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
import yt_dlp

# ─── Constantes ──────────────────────────────────────────────────────────────

APP_TITLE   = "Baixador de Mídia"
APP_VERSION = "4.0"
CONFIG_FILE = Path.home() / ".baixador_config.json"
MAX_SIMULTANEOS = 8
URL_REGEX = re.compile(r"https?://[^\s,;\"'<>]+", re.IGNORECASE)
FONT_UI   = "Segoe UI" if sys.platform == "win32" else None
FONT_MONO = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"
RELEASES_URL = "https://github.com/IvensArj/Downloader/releases/latest"
FROZEN = bool(getattr(sys, "frozen", False))   # True dentro do .exe gerado pelo PyInstaller
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def resource_path(nome: str) -> Path:
    """Encontra recursos no Python normal e no executável PyInstaller."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / nome


def search_dirs() -> list[Path]:
    """Pastas onde procurar FFmpeg e runtimes JS: dentro do pacote e ao lado do executável."""
    dirs = [Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))]
    if FROZEN:
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir not in dirs:
            dirs.append(exe_dir)
    return dirs


def find_local(*nomes: str) -> str | None:
    """Primeiro arquivo encontrado, entre os nomes dados, nas pastas do app."""
    for pasta in search_dirs():
        for nome in nomes:
            candidato = pasta / nome
            if candidato.is_file():
                return str(candidato)
    return None


def ffmpeg_path() -> str | None:
    """Localiza o FFmpeg distribuído com o aplicativo."""
    return find_local("ffmpeg.exe", "ffmpeg")


ANSI_REGEX = re.compile(r"\x1b\[[0-9;]*m|\[\d+(?:;\d+)*m")


def limpar_ansi(texto: str) -> str:
    """Remove códigos de cor do terminal que o yt-dlp coloca nas mensagens."""
    return ANSI_REGEX.sub("", texto)


def detectar_js_runtimes() -> dict:
    """
    O YouTube exige um runtime JavaScript (EJS) para resolver os desafios do player.
    Sem ele aparecem erros 403 e 'formato indisponível'. Procura Deno, Node (>=22),
    Bun e QuickJS, primeiro ao lado do app e depois no PATH.
    Retorna o dicionário aceito pela opção 'js_runtimes' do yt-dlp.
    """
    runtimes: dict = {}
    for nome, exe in (("deno", "deno"), ("node", "node"), ("bun", "bun"), ("quickjs", "qjs")):
        achado = find_local(exe + ".exe", exe) or shutil.which(exe)
        if achado:
            runtimes[nome] = {"path": achado}
    return runtimes


PIP_CMD = f'"{sys.executable}" -m pip install -U "yt-dlp[default]"'


def node_major(caminho: str) -> int | None:
    """Versão principal do Node (o EJS exige 22 ou superior)."""
    try:
        saida = subprocess.run([caminho, "--version"], capture_output=True, text=True,
                               timeout=5, creationflags=NO_WINDOW).stdout.strip()
        return int(saida.lstrip("v").split(".")[0])
    except Exception:
        return None


def ejs_instalado() -> bool:
    """Confere se o pacote yt-dlp-ejs (scripts do solver) está instalado."""
    try:
        import yt_dlp_ejs  # noqa: F401
        return True
    except ImportError:
        return False


COLORS = {
    "bg":          "#0a0a0b",
    "surface":     "#131315",
    "surface2":    "#1b1b1e",
    "border":      "#29292d",
    "accent":      "#22c55e",
    "accent_dim":  "#16a34a",
    "accent_deep": "#15803d",
    "danger":      "#ef4444",
    "danger_dim":  "#dc2626",
    "warning":     "#f59e0b",
    "text":        "#f4f4f5",
    "muted":       "#8a8a93",
    "log_bg":      "#0e0e10",
    "log_fg":      "#a1a1aa",
}

QUALITY_OPTIONS_VIDEO = [
    ("Melhor disponível", "bv*+ba/b"),
    ("1080p",            "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/best"),
    ("720p",             "bv*[height<=720]+ba/b[height<=720]/best[height<=720]/best"),
    ("480p",             "bv*[height<=480]+ba/b[height<=480]/best[height<=480]/best"),
    ("360p",             "bv*[height<=360]+ba/b[height<=360]/best[height<=360]/best"),
    ("Menor tamanho",    "worstvideo+worstaudio/worst"),
]

QUALITY_OPTIONS_AUDIO = [
    ("320 kbps", "320"),
    ("192 kbps", "192"),
    ("128 kbps", "128"),
    ("96 kbps",  "96"),
]

STATUS_LABEL = {
    "aguardando": "○  Aguardando",
    "baixando":   "↓  Baixando",
    "ok":         "✓  Concluído",
    "erro":       "✕  Erro",
    "cancelado":  "■  Cancelado",
}


# ─── Modelos de dados ─────────────────────────────────────────────────────────

@dataclass
class Item:
    idx:        int
    url:        str
    titulo:     str = ""
    status:     str = "aguardando"
    pct:        float = 0.0
    velocidade: str = ""
    iid:        str = ""
    ultimo_ui:  float = 0.0     # throttle de atualização da interface


@dataclass
class JobConfig:
    pasta:        str
    formato:      str
    qualidade:    str
    playlist:     bool
    zip_mode:     bool
    zip_nome:     str
    simultaneos:  int
    ejs_github:   bool = False


@dataclass
class AppConfig:
    ultima_pasta: str = ""
    formato:      str = "mp4"
    qualidade_v:  int = 0
    qualidade_a:  int = 1
    playlist:     bool = False
    simultaneos:  int = 3
    modo:         str = "pasta"   # "pasta" ou "zip"
    ejs_github:   bool = False    # permite baixar o solver do YouTube do GitHub

    def save(self):
        try:
            CONFIG_FILE.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2))
        except Exception:
            pass

    @staticmethod
    def load() -> "AppConfig":
        try:
            data = json.loads(CONFIG_FILE.read_text())
            return AppConfig(**{k: v for k, v in data.items() if k in AppConfig.__dataclass_fields__})
        except Exception:
            return AppConfig()


# ─── Utilitários ──────────────────────────────────────────────────────────────

def fmt_velocidade(bps: float | None) -> str:
    if not bps:
        return ""
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def barra_texto(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    cheios = int(pct // 10)
    return "▰" * cheios + "▱" * (10 - cheios) + f" {pct:3.0f}%"


# ─── Lógica de download ───────────────────────────────────────────────────────

class _YTDLPLogger:
    def __init__(self, idx: int, log):
        self.idx = idx
        self.log = log

    def debug(self, msg: str):
        pass

    def warning(self, msg: str):
        self.log(f"[#{self.idx}] Aviso: {limpar_ansi(msg)}", "aviso")

    def error(self, msg: str):
        self.log(f"[#{self.idx}] {limpar_ansi(msg)}", "erro")


class Downloader:
    """Executa um download por vez; vários itens rodam em paralelo, cada um com seu YoutubeDL."""

    def __init__(self, cancel: threading.Event, on_item, on_log):
        self.cancel  = cancel
        self.on_item = on_item     # callback(item, **campos)
        self.on_log  = on_log      # callback(msg, tag)

    def download(self, item: Item, workdir: str, cfg: JobConfig) -> None:
        if "spotify.com" in item.url:
            self._spotify(item, workdir)
        else:
            self._ytdlp(item, workdir, cfg)
        if self.cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled("Cancelado pelo usuário.")

    def _hook(self, item: Item, d: dict):
        if self.cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled("Cancelado pelo usuário.")

        titulo = (d.get("info_dict") or {}).get("title")
        status = d.get("status")

        if status == "downloading":
            agora = time.monotonic()
            if agora - item.ultimo_ui < 0.15:
                return
            item.ultimo_ui = agora
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            baixado = d.get("downloaded_bytes", 0)
            pct = (baixado / total * 100) if total else 0
            self.on_item(item, titulo=titulo, pct=pct, velocidade=fmt_velocidade(d.get("speed")))
        elif status == "finished":
            self.on_item(item, titulo=titulo, pct=100, velocidade="processando…")

    def _opts(self, item: Item, workdir: str, cfg: JobConfig) -> dict:
        opts = {
            "outtmpl":             os.path.join(workdir, "%(title).150s_%(id)s.%(ext)s"),
            "noplaylist":          not cfg.playlist,
            "quiet":               True,
            "no_warnings":         True,
            "progress_hooks":      [lambda d, it=item: self._hook(it, d)],
            "continuedl":          True,
            "retries":             10,
            "fragment_retries":    10,
            "extractor_retries":   3,
            "file_access_retries": 3,
            "socket_timeout":      30,
            "geo_bypass":          True,
            "ignoreerrors":        cfg.playlist,
            "logger":              _YTDLPLogger(item.idx, self.on_log),
        }
        caminho = ffmpeg_path()
        if caminho:
            opts["ffmpeg_location"] = caminho

        # Runtime JS para os desafios do YouTube (sem isso: 403 / formatos ausentes).
        runtimes = detectar_js_runtimes()
        if runtimes:
            opts["js_runtimes"] = runtimes
        if cfg.ejs_github:
            opts["remote_components"] = ["ejs:github"]

        if cfg.formato == "mp3":
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": cfg.qualidade,
                }],
            })
        else:
            opts.update({
                "format":              cfg.qualidade,
                "merge_output_format": "mp4",
                "prefer_ffmpeg":       True,
            })
        return opts

    def _ytdlp(self, item: Item, workdir: str, cfg: JobConfig):
        with yt_dlp.YoutubeDL(self._opts(item, workdir, cfg)) as ydl:
            ydl.download([item.url])

    def _spotify(self, item: Item, workdir: str):
        if FROZEN:
            # No .exe, sys.executable é o próprio app: "-m spotdl" abriria outra janela dele.
            raise RuntimeError("Links do Spotify não funcionam no executável. "
                               "Use a versão em Python com o spotdl instalado.")
        cmd = [sys.executable, "-m", "spotdl", "download", item.url, "--output", workdir]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, creationflags=NO_WINDOW)
        while True:
            try:
                saida, _ = proc.communicate(timeout=0.4)
                break
            except subprocess.TimeoutExpired:
                if self.cancel.is_set():
                    proc.terminate()
                    raise yt_dlp.utils.DownloadCancelled("Cancelado pelo usuário.")
        if proc.returncode != 0:
            raise RuntimeError((saida or "").strip()[-400:] or "spotDL falhou.")


# ─── Interface gráfica ────────────────────────────────────────────────────────

class DownloaderApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title(f"{APP_TITLE} {APP_VERSION}")
        self.root.geometry("1200x780")
        self.root.minsize(1080, 700)

        self.cfg = AppConfig.load()
        self._apply_theme()

        # Variáveis
        self.pasta_var    = tk.StringVar(value=self.cfg.ultima_pasta)
        self.formato_var  = tk.StringVar(value=self.cfg.formato if self.cfg.formato in ("mp4", "mp3") else "mp4")
        self.modo_var     = tk.StringVar(value=self.cfg.modo if self.cfg.modo in ("pasta", "zip") else "pasta")
        self.playlist_var = tk.BooleanVar(value=self.cfg.playlist)
        self.ejs_var      = tk.BooleanVar(value=self.cfg.ejs_github)
        self.status_var   = tk.StringVar(value="Pronto.")
        self.resumo_var   = tk.StringVar(value="0 itens")
        self.geral_txt    = tk.StringVar(value="Nenhum download em andamento")
        self._simult      = max(1, min(MAX_SIMULTANEOS, self.cfg.simultaneos))

        self._qual_idx = {"mp4": self.cfg.qualidade_v, "mp3": self.cfg.qualidade_a}

        # Estado
        self._items: list[Item] = []
        self._by_iid: dict[str, Item] = {}
        self._batch: list[Item] = []
        self._next_idx = 1
        self.is_downloading = False
        self._cancel = threading.Event()
        self._dica_403 = False
        self._uiq: queue.Queue = queue.Queue()

        self._dl = Downloader(self._cancel, self._emit_item, self._log_from_thread)

        self._build_fonts()
        self._build_ui()
        self._refresh_quality()
        self._sync_modo()
        self._update_overall()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll)

        self._diagnostico_inicial()

    # ── Tema e fontes ─────────────────────────────────────────────────────────

    def _apply_theme(self):
        C = COLORS
        ctk.set_appearance_mode("dark")
        self.root.configure(fg_color=C["bg"])

        # A fila usa ttk.Treeview (muito mais leve que dezenas de widgets): estilizado à mão.
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        fam = FONT_UI or "TkDefaultFont"
        style.configure("Treeview", background=C["surface"], fieldbackground=C["surface"],
                        foreground=C["text"], rowheight=34, borderwidth=0, relief="flat",
                        font=(fam, 10))
        style.configure("Treeview.Heading", background=C["surface2"], foreground=C["muted"],
                        font=(fam, 9, "bold"), relief="flat", borderwidth=0, padding=(8, 7))
        style.map("Treeview", background=[("selected", C["border"])],
                  foreground=[("selected", C["text"])])
        style.map("Treeview.Heading", background=[("active", C["border"])])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # sem borda branca

    def _build_fonts(self):
        self.f_title  = ctk.CTkFont(family=FONT_UI, size=20, weight="bold")
        self.f_icon   = ctk.CTkFont(family=FONT_UI, size=22, weight="bold")
        self.f_card   = ctk.CTkFont(family=FONT_UI, size=11, weight="bold")
        self.f_body   = ctk.CTkFont(family=FONT_UI, size=13)
        self.f_small  = ctk.CTkFont(family=FONT_UI, size=12)
        self.f_btn    = ctk.CTkFont(family=FONT_UI, size=14, weight="bold")
        self.f_mono   = ctk.CTkFont(family=FONT_MONO, size=12)

    # ── Construção da UI ──────────────────────────────────────────────────────

    def _card(self, parent, titulo: str, expand: bool = False, pady=(0, 10),
              side: str = "top") -> ctk.CTkFrame:
        C = COLORS
        card = ctk.CTkFrame(parent, fg_color=C["surface"], corner_radius=14,
                            border_width=1, border_color=C["border"])
        card.pack(fill="both" if expand else "x", expand=expand, pady=pady, side=side)
        ctk.CTkLabel(card, text=titulo, font=self.f_card, text_color=C["muted"],
                     anchor="w").pack(fill="x", padx=16, pady=(10, 6))
        return card

    def _ghost(self, parent, texto: str, comando, width: int | None = None) -> ctk.CTkButton:
        C = COLORS
        kw = {"width": width} if width else {}
        return ctk.CTkButton(parent, text=texto, command=comando, height=32, corner_radius=8,
                             fg_color=C["surface2"], hover_color=C["border"],
                             text_color=C["text"], text_color_disabled=C["muted"],
                             font=self.f_small, **kw)

    def _entry(self, parent, **kw) -> ctk.CTkEntry:
        C = COLORS
        return ctk.CTkEntry(parent, height=34, corner_radius=8, fg_color=C["surface2"],
                            border_color=C["border"], text_color=C["text"],
                            placeholder_text_color=C["muted"], font=self.f_body, **kw)

    def _segmented(self, parent, values, command, width: int | None = None) -> ctk.CTkSegmentedButton:
        C = COLORS
        seg = ctk.CTkSegmentedButton(
            parent, values=values, command=command, height=32, corner_radius=8,
            fg_color=C["surface2"], unselected_color=C["surface2"],
            unselected_hover_color=C["border"], selected_color=C["accent_deep"],
            selected_hover_color=C["accent_dim"], text_color=C["text"], font=self.f_small)
        if width:
            seg.configure(width=width)
        return seg

    def _switch(self, parent, texto: str, variable, comando=None) -> ctk.CTkSwitch:
        C = COLORS
        return ctk.CTkSwitch(parent, text=texto, variable=variable, command=comando,
                             progress_color=C["accent_dim"], button_color=C["text"],
                             button_hover_color="#ffffff", fg_color=C["border"],
                             text_color=C["text"], font=self.f_small)

    def _build_ui(self):
        C = COLORS

        # Cabeçalho
        header = ctk.CTkFrame(self.root, fg_color=C["surface"], corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="↓", font=self.f_icon, text_color=C["accent"]
                     ).pack(side="left", padx=(24, 6))
        ctk.CTkLabel(header, text=APP_TITLE, font=self.f_title, text_color=C["text"]
                     ).pack(side="left")
        ctk.CTkLabel(header, text=f"v{APP_VERSION}", font=self.f_small, text_color=C["muted"]
                     ).pack(side="left", padx=10, pady=(6, 0))
        if FROZEN:   # no .exe não dá para atualizar pelo pip: abre a página de versões
            self._update_btn = self._ghost(header, "Baixar nova versão", self._abrir_releases, width=160)
        else:
            self._update_btn = self._ghost(header, "Atualizar yt-dlp", self._atualizar_ytdlp, width=140)
        self._update_btn.pack(side="right", padx=(8, 22))
        self._yt_pill = ctk.CTkLabel(header, text="", height=26, corner_radius=13,
                                     font=self.f_small, fg_color=C["surface2"],
                                     text_color=C["muted"])
        self._yt_pill.pack(side="right", padx=8)
        ctk.CTkFrame(self.root, fg_color=C["border"], height=1, corner_radius=0).pack(fill="x")

        # Barra de status (antes do corpo para ficar sempre visível)
        statusbar = ctk.CTkFrame(self.root, fg_color=C["surface"], corner_radius=0, height=32)
        statusbar.pack(fill="x", side="bottom")
        statusbar.pack_propagate(False)
        ctk.CTkLabel(statusbar, textvariable=self.status_var, font=self.f_small,
                     text_color=C["muted"]).pack(side="left", padx=18)
        ctk.CTkLabel(statusbar, textvariable=self.resumo_var, font=self.f_mono,
                     text_color=C["accent"]).pack(side="right", padx=18)

        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(14, 8))

        left = ctk.CTkFrame(body, fg_color="transparent", width=380)
        left.pack(side="left", fill="y", padx=(0, 14))
        left.pack_propagate(False)
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, parent):
        C = COLORS

        # Ações fixas no rodapé da coluna: o botão principal nunca sai da tela.
        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(side="bottom", fill="x", pady=(4, 0))
        self._download_btn = ctk.CTkButton(
            btns, text="Baixar tudo", command=self._iniciar, height=46, corner_radius=12,
            font=self.f_btn, fg_color=C["accent"], hover_color=C["accent_dim"],
            text_color="#04130a", text_color_disabled=C["muted"])
        self._download_btn.pack(side="left", fill="x", expand=True)
        self._cancel_btn = ctk.CTkButton(
            btns, text="Cancelar", command=self._cancelar, height=46, width=112,
            corner_radius=12, font=self.f_btn, fg_color=C["surface2"],
            hover_color=C["danger_dim"], text_color=C["text"],
            text_color_disabled=C["muted"], state="disabled")
        self._cancel_btn.pack(side="left", padx=(8, 0))

        # ── Links ─────────────────────────────────────────────────────────────
        links = self._card(parent, "LINKS  ·  UM POR LINHA", expand=True)
        self._links_text = ctk.CTkTextbox(
            links, height=70, corner_radius=10, fg_color=C["surface2"], border_width=1,
            border_color=C["border"], text_color=C["text"], font=self.f_mono, wrap="none",
            scrollbar_button_color=C["border"], scrollbar_button_hover_color=C["muted"])
        self._links_text.pack(fill="both", expand=True, padx=16)
        row = ctk.CTkFrame(links, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(8, 14))
        self._ghost(row, "Colar", self._colar).pack(side="left")
        self._ghost(row, "Importar .txt", self._importar_txt).pack(side="left", padx=6)
        self._ghost(row, "Limpar", lambda: self._links_text.delete("1.0", "end")).pack(side="left")

        # ── Destino ───────────────────────────────────────────────────────────
        dest = self._card(parent, "DESTINO")
        prow = ctk.CTkFrame(dest, fg_color="transparent")
        prow.pack(fill="x", padx=16)
        self._entry(prow, textvariable=self.pasta_var).pack(side="left", fill="x", expand=True)
        self._ghost(prow, "Escolher", self._escolher_pasta, width=84).pack(side="left", padx=(8, 0))

        self._modo_seg = self._segmented(dest, ["Pasta", "ZIP"], self._on_modo_click)
        self._modo_seg.pack(fill="x", padx=16, pady=(10, 0))
        self._zip_entry = self._entry(dest, placeholder_text="Nome do ZIP  (vazio = midias_data_hora)")
        self._zip_entry.pack(fill="x", padx=16, pady=(8, 14))

        # ── Opções ────────────────────────────────────────────────────────────
        op = self._card(parent, "OPÇÕES")
        r1 = ctk.CTkFrame(op, fg_color="transparent")
        r1.pack(fill="x", padx=16)
        self._fmt_seg = self._segmented(r1, ["MP4", "MP3"], self._on_formato_click, width=130)
        self._fmt_seg.pack(side="left")
        self._qual_menu = ctk.CTkOptionMenu(
            r1, values=[""], command=self._on_qual_select, height=32, corner_radius=8,
            dynamic_resizing=False, fg_color=C["surface2"], button_color=C["border"],
            button_hover_color=C["muted"], text_color=C["text"], font=self.f_small,
            dropdown_fg_color=C["surface2"], dropdown_hover_color=C["accent_deep"],
            dropdown_text_color=C["text"], dropdown_font=self.f_small)
        self._qual_menu.pack(side="left", fill="x", expand=True, padx=(8, 0))

        r2 = ctk.CTkFrame(op, fg_color="transparent")
        r2.pack(fill="x", padx=16, pady=(10, 0))
        ctk.CTkLabel(r2, text="Simultâneos", font=self.f_small, text_color=C["text"]
                     ).pack(side="left")
        self._simult_lbl = ctk.CTkLabel(r2, text=str(self._simult), width=22,
                                        font=self.f_btn, text_color=C["accent"])
        self._simult_lbl.pack(side="right")
        self._simult_slider = ctk.CTkSlider(
            r2, from_=1, to=MAX_SIMULTANEOS, number_of_steps=MAX_SIMULTANEOS - 1,
            command=self._on_simult, progress_color=C["accent_dim"], button_color=C["accent"],
            button_hover_color=C["accent"], fg_color=C["border"])
        self._simult_slider.set(self._simult)
        self._simult_slider.pack(side="left", fill="x", expand=True, padx=12)

        r3 = ctk.CTkFrame(op, fg_color="transparent")
        r3.pack(fill="x", padx=16, pady=(12, 16))
        self._switch(r3, "Playlist inteira", self.playlist_var).pack(side="left")
        self._switch(r3, "Solver via GitHub", self.ejs_var,
                     self._on_ejs_toggle).pack(side="right")

        # estado inicial dos controles
        self._fmt_seg.set(self.formato_var.get().upper())
        self._modo_seg.set("ZIP" if self.modo_var.get() == "zip" else "Pasta")

    def _build_right(self, parent):
        C = COLORS

        # O log é criado primeiro e fixo no rodapé: assim a fila nunca o espreme.
        log = self._card(parent, "LOG", pady=(10, 0), side="bottom")
        self._log = ctk.CTkTextbox(
            log, height=130, corner_radius=10, fg_color=C["log_bg"], text_color=C["log_fg"],
            font=self.f_mono, wrap="word", border_width=0,
            scrollbar_button_color=C["border"], scrollbar_button_hover_color=C["muted"])
        self._log.pack(fill="x", padx=16, pady=(0, 14))
        self._log.tag_config("ok",    foreground=C["accent"])
        self._log.tag_config("erro",  foreground=C["danger"])
        self._log.tag_config("aviso", foreground=C["warning"])
        self._log.tag_config("info",  foreground=C["log_fg"])
        self._log.configure(state="disabled")

        # ── Fila ──────────────────────────────────────────────────────────────
        fila = self._card(parent, "FILA DE DOWNLOADS", expand=True, pady=0)

        bar = ctk.CTkFrame(fila, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(0, 10))
        self._ghost(bar, "Abrir pasta", self._abrir_pasta).pack(side="left")
        self._ghost(bar, "Limpar concluídos",
                    lambda: self._limpar_lista(somente_ok=True)).pack(side="left", padx=6)
        self._ghost(bar, "Limpar tudo",
                    lambda: self._limpar_lista(somente_ok=False)).pack(side="left")

        tree_wrap = tk.Frame(fila, bg=C["surface"], highlightthickness=0, bd=0)
        tree_wrap.pack(fill="both", expand=True, padx=(16, 12))
        cols = ("n", "titulo", "status", "progresso", "vel")
        self._tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", selectmode="extended")
        for col, txt, w, anchor, stretch in (
            ("n",         "#",             38,  "center", False),
            ("titulo",    "Título / link", 150, "w",      True),
            ("status",    "Status",        116, "w",      False),
            ("progresso", "Progresso",     172, "w",      False),
            ("vel",       "Velocidade",    90,  "e",      False),
        ):
            # Larguras iniciais pequenas: o Treeview cresce a coluna "título" sozinho,
            # mas nunca encolhe colunas já largas (a última sumiria em janelas pequenas).
            self._tree.heading(col, text=txt, anchor="w" if anchor == "w" else anchor)
            self._tree.column(col, width=w, minwidth=38 if col == "n" else (100 if stretch else w),
                              anchor=anchor, stretch=stretch)
        vsb = ctk.CTkScrollbar(tree_wrap, orientation="vertical", command=self._tree.yview,
                               width=12, fg_color=C["surface"], button_color=C["border"],
                               button_hover_color=C["muted"])
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", padx=(4, 0))
        self._tree.pack(side="left", fill="both", expand=True)

        self._tree.tag_configure("aguardando", foreground=C["muted"])
        self._tree.tag_configure("baixando",   foreground=C["text"])
        self._tree.tag_configure("ok",         foreground=C["accent"])
        self._tree.tag_configure("erro",       foreground=C["danger"])
        self._tree.tag_configure("cancelado",  foreground=C["warning"])

        # Mensagem de fila vazia (some quando há itens)
        self._empty = tk.Label(
            tree_wrap, bg=C["surface"], fg=C["muted"], justify="center",
            text="Nenhum link na fila\n\nCole os links ao lado e clique em “Baixar tudo”",
            font=(FONT_UI or "TkDefaultFont", 11))

        self._menu = tk.Menu(self.root, tearoff=0, bg=C["surface2"], fg=C["text"],
                             activebackground=C["accent_deep"], activeforeground="#ffffff",
                             relief="flat", borderwidth=0)
        self._menu.add_command(label="Copiar link", command=self._copiar_links)
        self._menu.add_command(label="Remover da lista", command=self._remover_selecionados)
        self._tree.bind("<Button-3>", self._menu_popup)
        self._tree.bind("<Button-2>", self._menu_popup)
        self._tree.bind("<Delete>", lambda e: self._remover_selecionados())

        # Progresso geral
        self._geral_bar = ctk.CTkProgressBar(fila, height=8, corner_radius=4,
                                             fg_color=C["surface2"], progress_color=C["accent"])
        self._geral_bar.set(0)
        self._geral_bar.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(fila, textvariable=self.geral_txt, font=self.f_small,
                     text_color=C["muted"], anchor="w").pack(fill="x", padx=16, pady=(4, 12))

    # ── Diagnóstico ───────────────────────────────────────────────────────────

    def _diagnostico_inicial(self):
        C = COLORS
        if not ffmpeg_path() and not shutil.which("ffmpeg"):
            self._log_msg("FFmpeg não encontrado: mesclar vídeo/áudio e criar MP3 pode falhar.", "aviso")

        versao = getattr(getattr(yt_dlp, "version", None), "__version__", "?")
        self._log_msg(f"yt-dlp {versao}", "info")
        runtimes = detectar_js_runtimes()
        if runtimes:
            self._log_msg("Runtime JavaScript: " + ", ".join(runtimes), "ok")
        else:
            self._log_msg("Nenhum runtime JavaScript (Deno/Node 22+) encontrado: o YouTube "
                          "vai falhar com erro 403. Instale o Deno: winget install DenoLand.Deno", "erro")
        node_antigo = False
        if "node" in runtimes and "deno" not in runtimes:
            major = node_major(runtimes["node"]["path"])
            if major is not None and major < 22:
                node_antigo = True
                self._log_msg(f"Node {major} é antigo demais (precisa do 22+). "
                              "Atualize o Node ou instale o Deno.", "erro")
        ejs = ejs_instalado()
        if not ejs:
            if FROZEN:
                self._log_msg("Os componentes do YouTube (yt-dlp-ejs) não vieram neste executável. "
                              "Baixe uma versão mais nova em Releases.", "erro")
            else:
                self._log_msg("Pacote yt-dlp-ejs ausente. Feche o app e rode no terminal:", "erro")
                self._log_msg(f"{PIP_CMD}", "erro")

        if runtimes and ejs and not node_antigo:
            self._yt_pill.configure(text="●  YouTube pronto", text_color=C["accent"])
        elif not runtimes or node_antigo:
            self._yt_pill.configure(text="●  Falta runtime JS", text_color=C["danger"])
        else:
            self._yt_pill.configure(text="●  Falta yt-dlp-ejs", text_color=C["warning"])

    # ── Fila de eventos thread → interface ────────────────────────────────────

    def _poll(self):
        try:
            while True:
                fn = self._uiq.get_nowait()
                try:
                    fn()
                except Exception as e:  # nunca deixa o loop morrer
                    print("Erro na UI:", e, file=sys.stderr)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _emit_item(self, item: Item, **campos):
        self._uiq.put(lambda: self._apply(item, campos))

    def _log_from_thread(self, texto: str, tag: str = "info"):
        self._uiq.put(lambda: self._log_msg(texto, tag))

    def _log_msg(self, texto: str, tag: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.configure(state="normal")
        self._log.insert("end", f"[{ts}] {texto}\n", tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    # ── Atualização de itens ──────────────────────────────────────────────────

    def _apply(self, item: Item, campos: dict):
        for k, v in campos.items():
            if v is None:
                continue
            setattr(item, k, v)
        self._refresh_row(item)
        self._update_overall()

    def _refresh_row(self, item: Item):
        if item.iid not in self._by_iid:
            return
        self._tree.item(item.iid, tags=(item.status,), values=(
            item.idx,
            (item.titulo or item.url)[:90],
            STATUS_LABEL.get(item.status, item.status),
            barra_texto(100 if item.status == "ok" else item.pct),
            item.velocidade if item.status == "baixando" else "",
        ))

    def _set_geral(self, pct: float):
        self._geral_bar.set(max(0.0, min(1.0, pct / 100)))

    def _update_overall(self):
        n = len(self._items)
        self.resumo_var.set(f"{n} iten{'s' if n != 1 else ''}")
        if n:
            self._empty.place_forget()
        else:
            self._empty.place(relx=0.5, rely=0.5, anchor="center")
            self._empty.lift()
        if not self._batch:
            return
        total = len(self._batch)
        final = ("ok", "erro", "cancelado")
        feitos = sum(1 for i in self._batch if i.status in final)
        soma = sum(100 if i.status in final else i.pct for i in self._batch)
        self._set_geral(soma / total)
        ativos = sum(1 for i in self._batch if i.status == "baixando")
        self.geral_txt.set(f"{feitos} de {total} finalizados   ·   {ativos} em andamento   ·   {soma / total:.0f}%")

    def _adicionar_item(self, url: str) -> Item:
        item = Item(idx=self._next_idx, url=url)
        self._next_idx += 1
        item.iid = self._tree.insert("", "end")
        self._items.append(item)
        self._by_iid[item.iid] = item
        self._refresh_row(item)
        return item

    # ── Links ─────────────────────────────────────────────────────────────────

    def _colar(self):
        try:
            txt = self.root.clipboard_get().strip()
        except Exception:
            return
        if txt:
            atual = self._links_text.get("1.0", "end").strip()
            if atual:
                self._links_text.insert("end", "\n")
            self._links_text.insert("end", txt + "\n")

    def _importar_txt(self):
        caminho = filedialog.askopenfilename(
            title="Escolha um .txt com links",
            filetypes=[("Texto", "*.txt *.csv *.list"), ("Todos", "*.*")])
        if not caminho:
            return
        try:
            conteudo = Path(caminho).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível ler o arquivo:\n{e}")
            return
        atual = self._links_text.get("1.0", "end").strip()
        if atual:
            self._links_text.insert("end", "\n")
        self._links_text.insert("end", conteudo.strip() + "\n")

    def _ler_links(self) -> int:
        """Lê a caixa de texto, adiciona links novos à fila e limpa a caixa."""
        texto = self._links_text.get("1.0", "end")
        existentes = {i.url for i in self._items}
        novos = 0
        for linha in texto.splitlines():
            linha = linha.strip()
            if not linha:
                continue
            achados = URL_REGEX.findall(linha)
            if not achados:
                self._log_msg(f"Linha ignorada (não parece um link): {linha[:60]}", "aviso")
                continue
            for url in achados:
                if url in existentes:
                    self._log_msg(f"Link repetido ignorado: {url[:70]}", "aviso")
                    continue
                existentes.add(url)
                self._adicionar_item(url)
                novos += 1
        if novos:
            self._links_text.delete("1.0", "end")
        self._update_overall()
        return novos

    # ── Pasta / formato / modo / opções ───────────────────────────────────────

    def _escolher_pasta(self):
        pasta = filedialog.askdirectory(title="Onde deseja salvar?")
        if pasta:
            self.pasta_var.set(pasta)

    def _abrir_pasta(self):
        pasta = self.pasta_var.get().strip()
        if not pasta or not Path(pasta).exists():
            messagebox.showwarning("Aviso", "Pasta inválida ou não selecionada.")
            return
        if sys.platform == "win32":
            os.startfile(pasta)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", pasta])
        else:
            subprocess.Popen(["xdg-open", pasta])

    def _opcoes_qualidade(self):
        return QUALITY_OPTIONS_AUDIO if self.formato_var.get() == "mp3" else QUALITY_OPTIONS_VIDEO

    def _on_formato_click(self, valor: str):
        self.formato_var.set(valor.lower())
        self._refresh_quality()

    def _refresh_quality(self):
        opcoes = self._opcoes_qualidade()
        nomes = [q[0] for q in opcoes]
        idx = max(0, min(self._qual_idx.get(self.formato_var.get(), 0), len(nomes) - 1))
        self._qual_menu.configure(values=nomes)
        self._qual_menu.set(nomes[idx])

    def _on_qual_select(self, valor: str):
        nomes = [q[0] for q in self._opcoes_qualidade()]
        if valor in nomes:
            self._qual_idx[self.formato_var.get()] = nomes.index(valor)

    def _get_qualidade(self) -> str:
        opcoes = self._opcoes_qualidade()
        idx = max(0, min(self._qual_idx.get(self.formato_var.get(), 0), len(opcoes) - 1))
        return opcoes[idx][1]

    def _on_modo_click(self, valor: str):
        self.modo_var.set("zip" if valor == "ZIP" else "pasta")
        self._sync_modo()

    def _sync_modo(self):
        self._zip_entry.configure(state="normal" if self.modo_var.get() == "zip" else "disabled")

    def _on_simult(self, valor: float):
        self._simult = max(1, min(MAX_SIMULTANEOS, int(round(valor))))
        self._simult_lbl.configure(text=str(self._simult))

    def _on_ejs_toggle(self):
        if not self.ejs_var.get():
            return
        ok = messagebox.askyesno(
            "Baixar solver do GitHub",
            "Esta opção faz o yt-dlp baixar scripts do GitHub e executá-los no seu computador "
            "para resolver os desafios do YouTube.\n\n"
            "A alternativa mais segura é instalar o pacote yt-dlp-ejs pelo pip.\n\n"
            "Deseja ativar mesmo assim?",
            icon="warning")
        if not ok:
            self.ejs_var.set(False)

    # ── Lista: limpar / remover / menu ────────────────────────────────────────

    def _limpar_lista(self, somente_ok: bool):
        if self.is_downloading:
            messagebox.showinfo("Aguarde", "Cancele ou espere o fim dos downloads para limpar a lista.")
            return
        for item in list(self._items):
            if not somente_ok or item.status == "ok":
                self._remover_item(item)
        self._batch = []
        self._set_geral(0)
        self.geral_txt.set("Nenhum download em andamento")
        self._update_overall()

    def _remover_item(self, item: Item):
        try:
            self._tree.delete(item.iid)
        except tk.TclError:
            pass
        self._by_iid.pop(item.iid, None)
        if item in self._items:
            self._items.remove(item)

    def _remover_selecionados(self):
        if self.is_downloading:
            return
        for iid in self._tree.selection():
            item = self._by_iid.get(iid)
            if item:
                self._remover_item(item)
        self._update_overall()

    def _copiar_links(self):
        urls = [self._by_iid[i].url for i in self._tree.selection() if i in self._by_iid]
        if urls:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(urls))

    def _menu_popup(self, event):
        iid = self._tree.identify_row(event.y)
        if iid and iid not in self._tree.selection():
            self._tree.selection_set(iid)
        if self._tree.selection():
            self._menu.tk_popup(event.x_root, event.y_root)

    # ── Execução do lote ──────────────────────────────────────────────────────

    def _set_running(self, rodando: bool):
        C = COLORS
        self._download_btn.configure(
            state="disabled" if rodando else "normal",
            fg_color=C["surface2"] if rodando else C["accent"])
        self._cancel_btn.configure(
            state="normal" if rodando else "disabled",
            fg_color=C["danger"] if rodando else C["surface2"])

    def _iniciar(self):
        if self.is_downloading:
            return

        pasta = self.pasta_var.get().strip()
        if not pasta:
            messagebox.showwarning("Aviso", "Escolha a pasta de destino.")
            return
        try:
            Path(pasta).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível criar a pasta:\n{e}")
            return

        self._ler_links()
        pendentes = [i for i in self._items if i.status != "ok"]
        if not pendentes:
            messagebox.showwarning("Aviso", "Cole ao menos um link para baixar.")
            return

        simult = self._simult
        fmt = self.formato_var.get()
        cfg = JobConfig(
            pasta=pasta, formato=fmt, qualidade=self._get_qualidade(),
            playlist=self.playlist_var.get(), zip_mode=self.modo_var.get() == "zip",
            zip_nome=self._zip_entry.get(), simultaneos=simult,
            ejs_github=self.ejs_var.get(),
        )

        # Salva preferências
        self.cfg.ultima_pasta = pasta
        self.cfg.formato      = fmt
        self.cfg.qualidade_v  = self._qual_idx.get("mp4", 0)
        self.cfg.qualidade_a  = self._qual_idx.get("mp3", 1)
        self.cfg.playlist     = cfg.playlist
        self.cfg.simultaneos  = simult
        self.cfg.modo         = "zip" if cfg.zip_mode else "pasta"
        self.cfg.ejs_github   = cfg.ejs_github
        self.cfg.save()

        # Prepara itens (inclui tentar de novo os que falharam)
        for item in pendentes:
            item.status, item.pct, item.velocidade = "aguardando", 0.0, ""
            self._refresh_row(item)
        self._batch = pendentes

        self._cancel.clear()
        self.is_downloading = True
        self._set_running(True)
        self._set_geral(0)
        self._update_overall()
        self.status_var.set(f"Baixando {len(pendentes)} item(ns) — {simult} por vez…")
        self._log_msg(f"▶ Lote iniciado: {len(pendentes)} link(s), {simult} simultâneo(s), "
                      f"{fmt.upper()} · {'ZIP' if cfg.zip_mode else 'pasta'}", "info")

        threading.Thread(target=self._run_batch, args=(pendentes, cfg), daemon=True).start()

    def _run_item(self, item: Item, cfg: JobConfig, temp_dir: Path | None) -> str:
        if self._cancel.is_set():
            self._emit_item(item, status="cancelado", velocidade="")
            return "cancelado"

        workdir = cfg.pasta
        if temp_dir is not None:
            workdir = str(temp_dir / f"{item.idx:03d}")
            os.makedirs(workdir, exist_ok=True)

        self._emit_item(item, status="baixando", pct=0.0)
        self._log_from_thread(f"▶ [#{item.idx}] {item.url}", "info")
        try:
            self._dl.download(item, workdir, cfg)
            self._emit_item(item, status="ok", pct=100.0, velocidade="")
            self._log_from_thread(f"✓ [#{item.idx}] concluído", "ok")
            return "ok"
        except yt_dlp.utils.DownloadCancelled:
            self._emit_item(item, status="cancelado", velocidade="")
            self._log_from_thread(f"■ [#{item.idx}] cancelado", "aviso")
            return "cancelado"
        except Exception as e:
            self._emit_item(item, status="erro", velocidade="")
            msg = limpar_ansi(str(e)).strip().splitlines()[-1] if str(e).strip() else "erro desconhecido"
            self._log_from_thread(f"✕ [#{item.idx}] {msg}", "erro")
            if "403" in msg and not self._dica_403:
                self._dica_403 = True
                if FROZEN:
                    dica = ("Dica: erro 403 no YouTube. Instale o Deno ou o Node 22+, reabra o app "
                            "e, se continuar, baixe a versão mais nova em Releases.")
                else:
                    dica = (f"Dica: erro 403 no YouTube. Feche o app, rode  {PIP_CMD}  "
                            "e abra de novo. Precisa de Deno ou Node 22+.")
                self._log_from_thread(dica, "aviso")
            return "erro"

    def _run_batch(self, items: list[Item], cfg: JobConfig):
        temp_dir: Path | None = None
        zip_path: Path | None = None
        resultados: list[str] = []
        try:
            if cfg.zip_mode:
                temp_dir = Path(tempfile.mkdtemp(prefix=".baixador_tmp_", dir=cfg.pasta))

            with ThreadPoolExecutor(max_workers=cfg.simultaneos) as pool:
                resultados = list(pool.map(lambda it: self._run_item(it, cfg, temp_dir), items))

            if cfg.zip_mode and temp_dir is not None and not self._cancel.is_set() \
                    and any(r == "ok" for r in resultados):
                self._log_from_thread("Compactando arquivos…", "info")
                self._uiq.put(lambda: self.status_var.set("Compactando ZIP…"))
                zip_path = self._compactar(temp_dir, Path(cfg.pasta), cfg.zip_nome)
        except Exception as e:
            self._log_from_thread(f"✕ Falha no lote: {e}", "erro")
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

        self._uiq.put(lambda: self._batch_done(resultados, zip_path))

    @staticmethod
    def _compactar(origem: Path, destino_dir: Path, nome: str) -> Path | None:
        nome = "".join(c for c in nome.strip() if c not in '<>:"/\\|?*').strip()
        if not nome:
            nome = f"midias_{datetime.now():%Y%m%d_%H%M%S}"
        if nome.lower().endswith(".zip"):
            nome = nome[:-4]

        arquivos = [a for a in sorted(origem.rglob("*"))
                    if a.is_file() and a.suffix.lower() not in {".part", ".ytdl", ".temp"}]
        if not arquivos:
            return None

        destino = destino_dir / f"{nome}.zip"
        n = 1
        while destino.exists():
            destino = destino_dir / f"{nome} ({n}).zip"
            n += 1

        usados: set[str] = set()
        # Mídia já é comprimida: ZIP_STORED é muito mais rápido e quase do mesmo tamanho.
        with zipfile.ZipFile(destino, "w", zipfile.ZIP_STORED) as zf:
            for arq in arquivos:
                arc = arq.name
                k = 1
                while arc in usados:
                    arc = f"{arq.stem} ({k}){arq.suffix}"
                    k += 1
                usados.add(arc)
                zf.write(arq, arc)
        return destino

    def _batch_done(self, resultados: list[str], zip_path: Path | None):
        ok       = resultados.count("ok")
        erros    = resultados.count("erro")
        cancel   = resultados.count("cancelado")

        self.is_downloading = False
        self._set_running(False)
        self._update_overall()

        if zip_path:
            self._log_msg(f"ZIP criado: {zip_path}", "ok")
        resumo = f"{ok} concluído(s), {erros} com erro, {cancel} cancelado(s)"
        if self._cancel.is_set():
            self.status_var.set(f"■ Cancelado — {resumo}.")
            self._log_msg(f"■ Lote cancelado — {resumo}.", "aviso")
        elif erros:
            self.status_var.set(f"Finalizado com erros — {resumo}. Clique em Baixar para tentar de novo.")
            self._log_msg(f"Lote finalizado — {resumo}.", "aviso")
        else:
            self.status_var.set(f"✓ Finalizado — {resumo}.")
            self._log_msg(f"✓ Lote finalizado — {resumo}.", "ok")

    def _abrir_releases(self):
        webbrowser.open(RELEASES_URL)

    def _atualizar_ytdlp(self):
        if FROZEN:   # proteção extra: o pip não existe dentro do .exe
            self._abrir_releases()
            return
        if self.is_downloading:
            messagebox.showinfo("Aguarde", "Espere os downloads terminarem para atualizar.")
            return
        self._update_btn.configure(state="disabled")
        self._download_btn.configure(state="disabled")
        self.status_var.set("Atualizando yt-dlp… (pode levar um minuto)")
        self._log_msg(f"▶ Executando: {PIP_CMD}", "info")
        threading.Thread(target=self._rodar_pip, daemon=True).start()

    def _rodar_pip(self):
        cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", creationflags=NO_WINDOW)
            for linha in proc.stdout:
                linha = limpar_ansi(linha).strip()
                if linha:
                    self._log_from_thread(f"pip: {linha}", "info")
            codigo = proc.wait()
        except Exception as e:
            codigo = -1
            self._log_from_thread(f"✕ Não foi possível executar o pip: {e}", "erro")

        def fim():
            self._update_btn.configure(state="normal")
            self._download_btn.configure(state="normal")
            if codigo == 0:
                self.status_var.set("✓ Atualizado. Feche e abra o app de novo.")
                self._log_msg("✓ Atualização concluída. FECHE E ABRA O APP para valer.", "ok")
                messagebox.showinfo("Atualizado", "Componentes atualizados.\n"
                                    "Feche e abra o app de novo antes de baixar.")
            else:
                self.status_var.set("Falha ao atualizar.")
                self._log_msg("✕ O pip falhou. Leia as linhas 'pip:' acima para ver o motivo.", "erro")
        self._uiq.put(fim)

    def _cancelar(self):
        if self.is_downloading:
            self._cancel.set()
            self.status_var.set("Cancelando…")
            self._cancel_btn.configure(state="disabled")

    def _on_close(self):
        if self.is_downloading:
            if not messagebox.askyesno("Sair", "Há downloads em andamento. Deseja cancelar e sair?"):
                return
            self._cancel.set()
        self.root.destroy()


# ─── Ponto de entrada ─────────────────────────────────────────────────────────

def main():
    root = ctk.CTk()
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
