use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

use anyhow::{bail, Context, Result};
use reqwest::{blocking::Client, header::CONTENT_TYPE};
use serde_json::Value;
use tempfile::{Builder, NamedTempFile};

use crate::{storage::Store, Job, Metadata, Preset, SourceKind};

#[derive(Clone)]
pub struct Tools {
    folder: Option<PathBuf>,
}

impl Tools {
    pub fn new(folder: Option<PathBuf>) -> Self {
        Self { folder }
    }

    fn executable(&self, name: &str) -> Option<PathBuf> {
        let name = format!("{name}{}", std::env::consts::EXE_SUFFIX);
        let configured = std::env::var_os("MEDIA_STUDIO_FFMPEG_DIR").map(PathBuf::from);
        let beside_exe = std::env::current_exe()
            .ok()
            .and_then(|path| path.parent().map(Path::to_owned));
        configured
            .into_iter()
            .chain(self.folder.clone())
            .chain(beside_exe)
            .chain(
                std::env::var_os("PATH")
                    .iter()
                    .flat_map(std::env::split_paths),
            )
            .map(|dir| dir.join(&name))
            .find(|path| path.is_file())
    }

    pub fn available(&self) -> bool {
        self.executable("ffmpeg").is_some() && self.executable("ffprobe").is_some()
    }

    pub fn require(&self) -> Result<()> {
        if !self.available() {
            bail!("Instale FFmpeg e FFprobe no PATH ou coloque os dois ao lado do aplicativo.");
        }
        Ok(())
    }

    pub fn inspect(&self, source: &Path, cancelled: impl Fn() -> bool) -> Result<Metadata> {
        let binary = self
            .executable("ffprobe")
            .context("FFprobe não foi encontrado.")?;
        let output = NamedTempFile::new()?;
        let mut command = command(binary);
        let mut child = command
            .args([
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-show_entries",
                "format=duration:stream=codec_type,width,height:stream_disposition=attached_pic",
                "-of",
                "json",
                "-i",
            ])
            .arg(source)
            .stdout(output.reopen()?)
            .stderr(Stdio::null())
            .spawn()
            .context("Não foi possível iniciar o FFprobe.")?;
        let started = Instant::now();
        let success = wait(
            &mut child,
            || cancelled() || started.elapsed() > Duration::from_secs(30),
            || Ok(()),
        )?;
        if !success {
            bail!("O arquivo não contém mídia reconhecida ou está corrompido.");
        }
        let data: Value = serde_json::from_reader(output.reopen()?)
            .context("Não foi possível ler os dados da mídia.")?;
        let streams = data["streams"]
            .as_array()
            .context("O arquivo não contém faixas reconhecidas.")?;
        let video = streams
            .iter()
            .find(|s| s["codec_type"] == "video" && s["disposition"]["attached_pic"] != 1);
        let has_audio = streams.iter().any(|s| s["codec_type"] == "audio");
        if video.is_none() && !has_audio {
            bail!("O arquivo não contém vídeo ou áudio compatível.");
        }
        Ok(Metadata {
            bytes: Some(fs::metadata(source)?.len()),
            seconds: data["format"]["duration"]
                .as_str()
                .and_then(|value| value.parse::<f64>().ok())
                .filter(|value| value.is_finite() && *value > 0.0),
            width: video.and_then(|s| s["width"].as_u64()),
            height: video.and_then(|s| s["height"].as_u64()),
            has_audio,
            has_video: video.is_some(),
        })
    }

    pub fn process(&self, job: &Job, store: &Store, cancelled: &impl Fn() -> bool) -> Result<()> {
        check_cancel(cancelled)?;
        let folder = Path::new(&job.folder);
        let fetched = if matches!(job.source.kind, SourceKind::Url) {
            store.phase(job.id, "downloading", None)?;
            let mut input = Builder::new()
                .prefix(".estudio-download-")
                .tempfile_in(folder)
                .context("Não foi possível criar um arquivo no destino.")?;
            fetch(job, store, &mut input, cancelled)?;
            Some(input)
        } else {
            None
        };
        store.phase(job.id, "inspecting", None)?;
        let source = fetched
            .as_ref()
            .map(NamedTempFile::path)
            .unwrap_or_else(|| Path::new(&job.source.location));
        let metadata = self.inspect(source, cancelled)?;
        store.metadata(job.id, &metadata)?;
        match job.preset {
            Preset::Mp4 if !metadata.has_video => {
                bail!("Este arquivo não tem vídeo. Escolha Extrair áudio.")
            }
            Preset::Mp3 if !metadata.has_audio => bail!("Este arquivo não tem uma faixa de áudio."),
            _ => {}
        }
        store.phase(job.id, "converting", metadata.seconds.map(|_| 0.0))?;
        let output = Builder::new()
            .prefix(".estudio-export-")
            .tempfile_in(folder)
            .context("Não foi possível criar um arquivo no destino.")?;
        self.convert(job, source, output.path(), &metadata, store, cancelled)?;
        store.phase(job.id, "saving", None)?;
        check_cancel(cancelled)?;
        let bytes = fs::metadata(output.path())?.len();
        if bytes == 0 {
            bail!("A conversão não gerou um arquivo.");
        }
        output
            .as_file()
            .sync_all()
            .context("Não foi possível gravar o resultado.")?;
        store.finish(job.id, || {
            Ok((publish(output, folder, &job.label, job.preset)?, bytes))
        })
    }

    fn convert(
        &self,
        job: &Job,
        source: &Path,
        output: &Path,
        metadata: &Metadata,
        store: &Store,
        cancelled: &impl Fn() -> bool,
    ) -> Result<()> {
        let binary = self
            .executable("ffmpeg")
            .context("FFmpeg não foi encontrado.")?;
        let mut process = command(&binary);
        process
            .args([
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-progress",
                "pipe:1",
                "-nostats",
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-i",
            ])
            .arg(source);
        match job.preset {
            Preset::Mp4 => {
                let info = command(&binary)
                    .args(["-hide_banner", "-encoders"])
                    .output()?;
                let encoders = String::from_utf8_lossy(&info.stdout);
                process.args([
                    "-map",
                    "0:V:0",
                    "-map",
                    "0:a:0?",
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
                ]);
                if encoders
                    .lines()
                    .any(|line| line.split_whitespace().nth(1) == Some("libx264"))
                {
                    process.args(["-c:v", "libx264", "-preset", "fast", "-crf", "22"]);
                } else if encoders
                    .lines()
                    .any(|line| line.split_whitespace().nth(1) == Some("h264_mf"))
                {
                    process.args(["-c:v", "h264_mf", "-b:v", "4M"]);
                } else {
                    bail!("Este FFmpeg precisa oferecer o codificador H.264 libx264 ou h264_mf.");
                }
                process.args([
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                ]);
            }
            Preset::Mp3 => {
                process.args([
                    "-map",
                    "0:a:0",
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    "-f",
                    "mp3",
                ]);
            }
        }
        let errors = NamedTempFile::new()?;
        let mut child = process
            .arg(output)
            .stdout(Stdio::piped())
            .stderr(errors.reopen()?)
            .spawn()
            .context("Não foi possível iniciar o FFmpeg.")?;
        let pipe = child.stdout.take().expect("stdout configurado como pipe");
        let (send, updates) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(pipe).lines().map_while(Result::ok) {
                if let Some(time) = line
                    .strip_prefix("out_time_us=")
                    .and_then(|v| v.parse::<f64>().ok())
                {
                    if send.send(time / 1_000_000.0).is_err() {
                        break;
                    }
                }
            }
        });
        let mut last = Instant::now();
        let mut seconds = 0.0;
        let completed = wait(&mut child, cancelled, || {
            for next in updates.try_iter() {
                seconds = next;
            }
            if let Some(duration) = metadata
                .seconds
                .filter(|_| last.elapsed() >= Duration::from_millis(350))
            {
                store.phase(
                    job.id,
                    "converting",
                    Some((seconds / duration).clamp(0.0, 0.99)),
                )?;
                last = Instant::now();
            }
            Ok(())
        })?;
        if !completed {
            let mut log = String::new();
            let _ = errors.reopen()?.take(2000).read_to_string(&mut log);
            bail!(
                "O FFmpeg não conseguiu converter este arquivo. {}",
                log.trim()
            );
        }
        Ok(())
    }
}

fn command(program: impl AsRef<Path>) -> Command {
    let mut command = Command::new(program.as_ref());
    command.stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command
}

fn check_cancel(cancelled: impl Fn() -> bool) -> Result<()> {
    if cancelled() {
        bail!("Operação cancelada.");
    }
    Ok(())
}

fn wait(
    child: &mut Child,
    cancelled: impl Fn() -> bool,
    mut update: impl FnMut() -> Result<()>,
) -> Result<bool> {
    let result = (|| loop {
        check_cancel(&cancelled)?;
        if let Some(status) = child.try_wait()? {
            return Ok(status.success());
        }
        update()?;
        thread::sleep(Duration::from_millis(80));
    })();
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    result
}

fn fetch(
    job: &Job,
    store: &Store,
    output: &mut NamedTempFile,
    cancelled: &impl Fn() -> bool,
) -> Result<()> {
    let client = Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(45))
        .build()?;
    let mut response = client
        .get(&job.source.location)
        .send()
        .context("Não foi possível acessar o arquivo pelo link.")?;
    if !response.status().is_success() {
        bail!(
            "O servidor respondeu com HTTP {}.",
            response.status().as_u16()
        );
    }
    if response
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.starts_with("text/html"))
    {
        bail!("Este endereço é uma página. Cole o link direto do arquivo de mídia.");
    }
    let size = response.content_length().filter(|size| *size > 0);
    let mut received = 0_u64;
    let mut buffer = [0; 65536];
    let mut last = Instant::now();
    loop {
        check_cancel(cancelled)?;
        let length = response
            .read(&mut buffer)
            .context("A conexão caiu durante o download.")?;
        if length == 0 {
            break;
        }
        output
            .write_all(&buffer[..length])
            .context("Não foi possível gravar a mídia temporária no destino.")?;
        received += length as u64;
        if last.elapsed() >= Duration::from_millis(350) {
            store.phase(
                job.id,
                "downloading",
                size.map(|size| (received as f64 / size as f64).min(0.99)),
            )?;
            last = Instant::now();
        }
    }
    if received == 0 || size.is_some_and(|size| received != size) {
        bail!("O arquivo recebido está vazio ou incompleto.");
    }
    Ok(())
}

fn publish(mut file: NamedTempFile, folder: &Path, label: &str, preset: Preset) -> Result<PathBuf> {
    let stem = Path::new(label)
        .file_stem()
        .and_then(|name| name.to_str())
        .unwrap_or("midia");
    let stem: String = stem
        .chars()
        .take(75)
        .map(|ch| {
            if ch.is_control() || "<>:\"/\\|?*".contains(ch) {
                '_'
            } else {
                ch
            }
        })
        .collect();
    let stem = stem.trim_matches([' ', '.']);
    let ext = preset.extension();
    for index in 1..=1000 {
        let suffix = if index == 1 {
            String::new()
        } else {
            format!(" ({index})")
        };
        let path = folder.join(format!("{stem} - {ext}{suffix}.{ext}"));
        match file.persist_noclobber(&path) {
            Ok(_) => return Ok(path),
            Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => {
                file = error.file
            }
            Err(error) => {
                return Err(error.error).context("Não foi possível publicar o arquivo de saída.")
            }
        }
    }
    bail!("Não foi possível escolher um nome de saída disponível.")
}
