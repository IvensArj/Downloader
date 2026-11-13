use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    path::Path,
    thread,
    time::{Duration, Instant},
};

use anyhow::{Context, Result};
use studio_core::{Job, Preset, Source, SourceKind, Studio, Tools};

const SAMPLE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../src-tauri/resources/exemplo.mp4"
);

fn await_jobs(studio: &Studio, count: usize) -> Result<Vec<Job>> {
    let start = Instant::now();
    loop {
        let jobs = studio.list()?;
        if jobs.len() == count
            && jobs
                .iter()
                .all(|job| matches!(job.status.as_str(), "done" | "failed" | "cancelled"))
        {
            return Ok(jobs);
        }
        if start.elapsed() > Duration::from_secs(25) {
            anyhow::bail!("A fila não terminou: {jobs:?}");
        }
        thread::sleep(Duration::from_millis(60));
    }
}

#[test]
fn real_mp4_and_mp3_from_local_and_http_and_repeat_without_overwrite() -> Result<()> {
    let directory = tempfile::tempdir()?;
    let tools = Tools::new(None);
    tools
        .require()
        .context("O teste de integração precisa de FFmpeg e FFprobe no PATH")?;
    let studio = Studio::open(&directory.path().join("jobs.sqlite3"), tools.clone())?;
    let local = Source {
        kind: SourceKind::File,
        location: SAMPLE.into(),
    };
    let metadata = studio.inspect(&local)?;
    assert_eq!((metadata.width, metadata.height), (Some(480), Some(270)));
    assert!(metadata.has_audio && metadata.has_video);

    let server = TcpListener::bind("127.0.0.1:0")?;
    let url = format!("http://{}/demo.mp4", server.local_addr()?);
    let host = thread::spawn(move || -> Result<()> {
        let (mut stream, _) = server.accept()?;
        stream.set_read_timeout(Some(Duration::from_secs(3)))?;
        let mut request = [0; 2048];
        let _ = stream.read(&mut request)?;
        let data = fs::read(SAMPLE)?;
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", data.len())?;
        stream.write_all(&data)?;
        Ok(())
    });

    let result = (|| -> Result<()> {
        studio.add(&[local.clone()], Preset::Mp4, directory.path())?;
        studio.add(
            &[Source {
                kind: SourceKind::Url,
                location: url,
            }],
            Preset::Mp3,
            directory.path(),
        )?;
        studio.start_workers();
        let jobs = await_jobs(&studio, 2)?;
        assert!(jobs.iter().all(|job| job.status == "done"), "{jobs:?}");
        let mp3 = studio.output(jobs[0].id)?;
        let mp4 = studio.output(jobs[1].id)?;
        assert_eq!(mp3.extension().unwrap(), "mp3");
        assert_eq!(mp4.extension().unwrap(), "mp4");
        assert!(!tools.inspect(&mp3, || false)?.has_video);
        assert!(tools.inspect(&mp4, || false)?.has_video);
        studio.repeat(jobs[1].id)?;
        let repeated = await_jobs(&studio, 3)?;
        assert_eq!(repeated[0].status, "done");
        assert_ne!(studio.output(repeated[0].id)?, mp4);
        assert!(mp4.is_file() && Path::new(SAMPLE).is_file());
        assert!(!fs::read_dir(directory.path())?
            .flatten()
            .any(|entry| entry.file_name().to_string_lossy().starts_with(".estudio-")));
        Ok(())
    })();
    studio.join_workers();
    if result.is_ok() {
        host.join().expect("servidor de teste")?;
    }
    result
}
