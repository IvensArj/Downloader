# Componentes de terceiros

O Baixador de Mídia usa e, no executável das Releases, distribui os componentes abaixo.
Cada um segue a sua própria licença.

| Componente | Uso | Licença |
|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Motor de download | Unlicense (domínio público) |
| [yt-dlp-ejs](https://github.com/yt-dlp/ejs) | Scripts que resolvem os desafios do YouTube | Unlicense; embute `meriyah` (ISC) e `astring` (MIT) |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | Interface gráfica | MIT |
| [PyInstaller](https://pyinstaller.org) | Gera o executável | GPLv2+ com exceção que permite distribuir os programas gerados |
| [FFmpeg](https://ffmpeg.org) | Junta vídeo e áudio e cria MP3 | LGPL (build "lgpl" de [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds); a versão exata da licença está no pacote do build) |

## FFmpeg

O executável das Releases inclui `ffmpeg.exe` e `ffprobe.exe` do build **LGPL** do FFmpeg,
sem componentes GPL (como o x264). Eles são programas separados, não fazem parte do código
deste projeto, e podem ser substituídos: no pacote em pasta (`BaixadorMidia-windows.zip`),
troque os arquivos dentro da pasta `_internal` por outra versão do FFmpeg.

O código-fonte do FFmpeg está em <https://ffmpeg.org/download.html>, e os scripts do build usado
estão em <https://github.com/BtbN/FFmpeg-Builds>.
