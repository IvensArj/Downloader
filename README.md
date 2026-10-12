# Baixador de Mídia

Aplicativo desktop em Python baseado em [`yt-dlp`](https://github.com/yt-dlp/yt-dlp), com interface em CustomTkinter. Baixe somente conteúdo que você tenha autorização para salvar.

## Recursos

- Vários links de uma vez (um por linha, colados ou importados de um `.txt`)
- Downloads simultâneos (de 1 a 8) com progresso individual e geral
- Saída em pasta ou em um único arquivo `.zip`
- Vídeo em MP4 (com escolha de qualidade) ou áudio em MP3
- Playlists, repetição automática dos links que falharam e cancelamento do lote
- Links do Spotify via `spotdl` (opcional, só na versão em Python)

## Usar no Windows (sem instalar Python)

1. Abra a página de [**Releases**](https://github.com/IvensArj/Downloader/releases/latest) e baixe `BaixadorMidia-windows.zip`.
2. Extraia a pasta e abra `BaixadorMidia.exe`.
3. Para baixar do **YouTube**, instale o [Deno](https://deno.com) (`winget install DenoLand.Deno`) ou o Node.js 22+. Se preferir não instalar, coloque o `deno.exe` na mesma pasta do `BaixadorMidia.exe`.

O programa não é assinado digitalmente, então o Windows pode exibir o aviso "O Windows protegeu o computador". Clique em **Mais informações → Executar assim mesmo**. O SHA256 do zip está na descrição de cada versão, se quiser conferir o arquivo.

O botão **Baixar nova versão** (no topo do app) abre a página de Releases.

## Executar pelo Python

Requisitos: Python 3.10 ou superior e FFmpeg.

```bat
python -m pip install -r requirements.txt
python downloader.py
```

**FFmpeg:** instale com `winget install Gyan.FFmpeg`, ou coloque `ffmpeg.exe` e `ffprobe.exe` na mesma pasta de `downloader.py`. Eles são necessários para combinar vídeo e áudio e para transformar áudio em MP3.

**Spotify (opcional):** `python -m pip install -r requirements-spotify.txt`

### YouTube: runtime JavaScript

O YouTube exige que o `yt-dlp` execute JavaScript para liberar os vídeos. Sem isso, os downloads falham com `HTTP Error 403: Forbidden`. É preciso:

1. **Um runtime JavaScript** no PATH: Deno (recomendado, versão 2.3 ou superior) ou Node.js 22 ou superior.
2. **O pacote `yt-dlp-ejs`**, que já vem com `pip install -r requirements.txt` (pelo `yt-dlp[default]`).

Ao abrir, o app confere os dois itens e mostra o resultado no cabeçalho (**YouTube pronto**, **Falta runtime JS** ou **Falta yt-dlp-ejs**) e no log.

> A opção **Solver via GitHub** é uma alternativa ao pacote `yt-dlp-ejs`: o `yt-dlp` baixa scripts do GitHub e os executa no seu computador. Fica desligada por padrão e pede confirmação ao ser ativada. Prefira instalar o pacote.

## Criar o EXE

No Windows, coloque `ffmpeg.exe` e `ffprobe.exe` (de um build **LGPL**, pasta `bin`, em [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases)) ao lado do `build_exe.bat` e execute:

```bat
build_exe.bat            :: pasta dist\BaixadorMidia\ + dist\BaixadorMidia-windows.zip (recomendado)
build_exe.bat onefile    :: arquivo único dist\BaixadorMidia.exe
```

O modo em pasta abre mais rápido e dispara menos alertas de antivírus do que o arquivo único. O EXE não inclui o Deno nem o Node, e os links do Spotify não funcionam nele.

### Publicar uma nova versão

O workflow `.github/workflows/release.yml` compila no GitHub e anexa o zip à Release. Para publicar:

```bash
git tag v4.0
git push origin v4.0
```

Para só testar a compilação, abra a aba **Actions → Release (Windows) → Run workflow**: o zip fica disponível como artefato, sem criar Release.

## Problemas comuns

| Sintoma | O que fazer |
|---|---|
| `HTTP Error 403: Forbidden` no YouTube | Instale Deno ou Node 22+ e o `yt-dlp-ejs`, depois atualize o `yt-dlp`. Feche e abra o app. No EXE, baixe a versão mais nova em Releases. |
| Cabeçalho mostra **Falta yt-dlp-ejs** | Rode `python -m pip install -U "yt-dlp[default]"` com o mesmo Python que abre o app. |
| Cabeçalho mostra **Falta runtime JS** | Instale o Deno (`winget install DenoLand.Deno`) e reabra o app. |
| Aviso de FFmpeg não encontrado | Instale com `winget install Gyan.FFmpeg` ou coloque `ffmpeg.exe` e `ffprobe.exe` ao lado do app. |
| Alguns links falharam no lote | Clique em **Baixar tudo** de novo: só os que falharam são repetidos. |
| Aviso do Windows ao abrir o EXE | O programa não é assinado. Veja "Usar no Windows" acima. |

## Android / APK

Um programa Tkinter não pode ser convertido diretamente em APK: Tkinter não é uma interface Android. A lógica de download está isolada na classe `Downloader`, mas a tela deverá ser portada para Kivy, Flutter ou uma interface Android nativa. No APK, também será necessário usar um binário FFmpeg compilado para Android e respeitar as políticas de cada plataforma e dos serviços de vídeo.

## Manutenção

Sites de vídeo mudam com frequência. Quando uma fonte específica deixar de funcionar, atualize o motor. Pela versão em Python, use o botão **Atualizar yt-dlp** do app ou o terminal:

```bat
python -m pip install -U "yt-dlp[default]"
```

Depois de atualizar, feche e abra o app de novo. As preferências (pasta, formato, qualidade, número de downloads simultâneos) ficam salvas em `~/.baixador_config.json`.

## Licença

Código sob a licença [MIT](LICENSE). Os componentes de terceiros e o FFmpeg incluído no executável seguem as próprias licenças, listadas em [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
