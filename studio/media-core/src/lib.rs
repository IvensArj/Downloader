mod engine;
mod storage;

use std::{
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

pub use engine::Tools;
use storage::Store;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceKind {
    File,
    Url,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Source {
    pub kind: SourceKind,
    pub location: String,
}

impl Source {
    fn check(&self) -> Result<()> {
        match self.kind {
            SourceKind::File if Path::new(&self.location).is_file() => Ok(()),
            SourceKind::File => bail!("O arquivo original não está acessível."),
            SourceKind::Url => {
                let url = reqwest::Url::parse(&self.location).context("O link é inválido.")?;
                if !matches!(url.scheme(), "http" | "https")
                    || url.host().is_none()
                    || !url.username().is_empty()
                    || url.password().is_some()
                {
                    bail!("Use um link direto HTTP ou HTTPS sem credenciais no endereço.");
                }
                Ok(())
            }
        }
    }

    fn label(&self) -> String {
        match self.kind {
            SourceKind::File => Path::new(&self.location)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("Arquivo")
                .to_owned(),
            SourceKind::Url => reqwest::Url::parse(&self.location)
                .ok()
                .and_then(|url| {
                    let name = url.path_segments()?.rfind(|name| !name.is_empty())?;
                    Some(name.to_owned())
                })
                .unwrap_or_else(|| "Mídia do link".into()),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Preset {
    Mp4,
    Mp3,
}

impl Preset {
    fn extension(self) -> &'static str {
        match self {
            Self::Mp4 => "mp4",
            Self::Mp3 => "mp3",
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct Metadata {
    pub bytes: Option<u64>,
    pub seconds: Option<f64>,
    pub width: Option<u64>,
    pub height: Option<u64>,
    pub has_audio: bool,
    pub has_video: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Job {
    pub id: i64,
    pub batch: i64,
    pub source: Source,
    pub label: String,
    pub preset: Preset,
    pub folder: String,
    pub status: String,
    pub progress: Option<f64>,
    pub metadata: Option<Metadata>,
    pub output: Option<String>,
    pub output_bytes: Option<u64>,
    pub error: Option<String>,
    pub created_at: i64,
}

#[derive(Clone)]
pub struct Studio {
    store: Arc<Store>,
    tools: Tools,
    closing: Arc<AtomicBool>,
    workers: Arc<Mutex<Vec<thread::JoinHandle<()>>>>,
}

impl Studio {
    pub fn open(database: &Path, tools: Tools) -> Result<Self> {
        Ok(Self {
            store: Arc::new(Store::open(database)?),
            tools,
            closing: Arc::new(AtomicBool::new(false)),
            workers: Arc::default(),
        })
    }

    pub fn start_workers(&self) {
        let mut workers = self.workers.lock().expect("lista de threads da fila");
        if !workers.is_empty() {
            return;
        }
        for _ in 0..2 {
            let studio = self.clone();
            workers.push(thread::spawn(move || {
                while !studio.closing.load(Ordering::Relaxed) {
                    match studio.store.claim() {
                        Ok(Some(job)) => studio.execute(job),
                        Ok(None) => thread::sleep(Duration::from_millis(180)),
                        Err(error) => {
                            eprintln!("Fila: {error:#}");
                            thread::sleep(Duration::from_secs(1));
                        }
                    }
                }
            }));
        }
    }

    pub fn shutdown(&self) {
        self.closing.store(true, Ordering::Relaxed);
    }

    pub fn join_workers(&self) {
        self.shutdown();
        if let Ok(mut workers) = self.workers.lock() {
            for worker in workers.drain(..) {
                let _ = worker.join();
            }
        }
    }

    pub fn list(&self) -> Result<Vec<Job>> {
        self.store.list()
    }

    pub fn inspect(&self, source: &Source) -> Result<Metadata> {
        source.check()?;
        if !matches!(source.kind, SourceKind::File) {
            bail!("Os dados do link serão lidos após baixar o arquivo.");
        }
        self.tools.inspect(Path::new(&source.location), || false)
    }

    pub fn add(&self, sources: &[Source], preset: Preset, folder: &Path) -> Result<()> {
        self.tools.require()?;
        if sources.is_empty() || sources.len() > 100 {
            bail!("Adicione de 1 a 100 arquivos ou links por lote.");
        }
        for source in sources {
            source.check()?;
        }
        let folder =
            std::path::absolute(folder).context("Escolha uma pasta de saída que já exista.")?;
        if !folder.is_dir() {
            bail!("O destino precisa ser uma pasta existente.");
        }
        self.store.add(sources, preset, &folder)
    }

    pub fn retry(&self, id: i64) -> Result<()> {
        self.store.retry(id)
    }
    pub fn cancel(&self, id: i64) -> Result<()> {
        self.store.cancel(id)
    }

    pub fn repeat(&self, id: i64) -> Result<()> {
        let jobs = self.store.batch_for(id)?;
        let first = jobs.first().context("Lote não encontrado.")?;
        let sources: Vec<_> = jobs.iter().map(|job| job.source.clone()).collect();
        self.add(&sources, first.preset, Path::new(&first.folder))
    }

    pub fn output(&self, id: i64) -> Result<PathBuf> {
        let job = self.store.find(id)?;
        let path = PathBuf::from(
            job.output
                .filter(|_| job.status == "done")
                .context("O resultado ainda não está disponível.")?,
        );
        if !path.is_file() {
            bail!("O arquivo de saída foi movido ou removido.");
        }
        Ok(path)
    }

    fn execute(&self, job: Job) {
        let cancelled =
            || self.closing.load(Ordering::Relaxed) || self.store.cancelled(job.id).unwrap_or(true);
        let outcome = self.tools.process(&job, &self.store, &cancelled);
        if let Err(error) = outcome {
            let status = if self.closing.load(Ordering::Relaxed) {
                "interrupted"
            } else if cancelled() {
                "cancelled"
            } else {
                "failed"
            };
            if let Err(write_error) = self.store.fail(job.id, status, &format!("{error:#}")) {
                eprintln!("Não foi possível registrar a falha: {write_error:#}");
            }
        }
    }
}
