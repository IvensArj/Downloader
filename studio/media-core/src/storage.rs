use std::{
    fs::{self, File, OpenOptions},
    path::Path,
    sync::{Mutex, MutexGuard},
};

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension, Row};

use crate::{Job, Metadata, Preset, Source};

pub struct Store {
    connection: Mutex<Connection>,
    _lock: File,
}

fn decode<T: serde::de::DeserializeOwned>(row: &Row, index: usize) -> rusqlite::Result<T> {
    serde_json::from_str(&row.get::<_, String>(index)?).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            index,
            rusqlite::types::Type::Text,
            Box::new(error),
        )
    })
}

fn read_job(row: &Row) -> rusqlite::Result<Job> {
    Ok(Job {
        id: row.get(0)?,
        batch: row.get(1)?,
        source: decode(row, 2)?,
        label: row.get(3)?,
        preset: decode(row, 4)?,
        folder: row.get(5)?,
        status: row.get(6)?,
        progress: row.get(7)?,
        metadata: decode(row, 8)?,
        output: row.get(9)?,
        output_bytes: row.get(10)?,
        error: row.get(11)?,
        created_at: row.get(12)?,
    })
}

impl Store {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent)?;
        }
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.with_extension("lock"))?;
        fs2::FileExt::try_lock_exclusive(&lock).context("O Estúdio de Mídia já está aberto.")?;
        let connection = Connection::open(path).context("Não foi possível abrir o histórico.")?;
        connection.execute_batch("PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, batch INTEGER NOT NULL, source TEXT NOT NULL,
                label TEXT NOT NULL, preset TEXT NOT NULL, folder TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', progress REAL, metadata TEXT NOT NULL DEFAULT 'null',
                output TEXT, output_bytes INTEGER, error TEXT, created_at INTEGER NOT NULL DEFAULT(unixepoch()),
                cancel INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, id);
            CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch, id);
            UPDATE jobs SET status='interrupted', progress=NULL, cancel=0,
                error='O aplicativo foi fechado durante o processamento.'
                WHERE status IN ('downloading','inspecting','converting','saving','cancelling');")?;
        Ok(Self {
            connection: Mutex::new(connection),
            _lock: lock,
        })
    }

    fn conn(&self) -> Result<MutexGuard<'_, Connection>> {
        self.connection
            .lock()
            .map_err(|_| anyhow::anyhow!("O histórico está indisponível."))
    }

    pub fn list(&self) -> Result<Vec<Job>> {
        let conn = self.conn()?;
        let mut query = conn.prepare("SELECT * FROM jobs ORDER BY id DESC")?;
        let rows = query
            .query_map([], read_job)?
            .collect::<rusqlite::Result<_>>()?;
        Ok(rows)
    }

    pub fn find(&self, id: i64) -> Result<Job> {
        Ok(self
            .conn()?
            .query_row("SELECT * FROM jobs WHERE id=?1", [id], read_job)?)
    }

    pub fn batch_for(&self, id: i64) -> Result<Vec<Job>> {
        let conn = self.conn()?;
        let mut query = conn.prepare(
            "SELECT * FROM jobs WHERE batch=(SELECT batch FROM jobs WHERE id=?1) ORDER BY id",
        )?;
        let rows = query
            .query_map([id], read_job)?
            .collect::<rusqlite::Result<_>>()?;
        Ok(rows)
    }

    pub fn add(&self, sources: &[Source], preset: Preset, folder: &Path) -> Result<()> {
        let mut conn = self.conn()?;
        let tx = conn.transaction()?;
        let batch: i64 =
            tx.query_row("SELECT coalesce(max(batch), 0) + 1 FROM jobs", [], |row| {
                row.get(0)
            })?;
        let preset = serde_json::to_string(&preset)?;
        for source in sources {
            tx.execute(
                "INSERT INTO jobs(batch,source,label,preset,folder) VALUES (?1,?2,?3,?4,?5)",
                params![
                    batch,
                    serde_json::to_string(source)?,
                    source.label(),
                    preset,
                    folder.to_string_lossy()
                ],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    pub fn claim(&self) -> Result<Option<Job>> {
        let conn = self.conn()?;
        Ok(conn.query_row("UPDATE jobs SET status='inspecting' WHERE id=(SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1) RETURNING *", [], read_job).optional()?)
    }

    pub fn retry(&self, id: i64) -> Result<()> {
        if self.conn()?.execute(
            "UPDATE jobs SET status='queued', cancel=0, progress=NULL, error=NULL
            WHERE id=?1 AND status IN ('failed','cancelled','interrupted')",
            [id],
        )? == 0
        {
            bail!("Este item não está disponível para uma nova tentativa.");
        }
        Ok(())
    }

    pub fn cancel(&self, id: i64) -> Result<()> {
        if self.conn()?.execute("UPDATE jobs SET cancel=1, status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END
            WHERE id=?1 AND status IN ('queued','downloading','inspecting','converting','saving')", [id])? == 0 {
            bail!("Este item já foi finalizado ou está sendo cancelado.");
        }
        Ok(())
    }

    pub fn cancelled(&self, id: i64) -> Result<bool> {
        Ok(self
            .conn()?
            .query_row("SELECT cancel FROM jobs WHERE id=?1", [id], |row| {
                row.get(0)
            })?)
    }

    pub fn phase(&self, id: i64, status: &str, progress: Option<f64>) -> Result<()> {
        self.conn()?.execute(
            "UPDATE jobs SET status=?1, progress=?2 WHERE id=?3 AND cancel=0",
            params![status, progress, id],
        )?;
        Ok(())
    }

    pub fn metadata(&self, id: i64, value: &Metadata) -> Result<()> {
        self.conn()?.execute(
            "UPDATE jobs SET metadata=?1 WHERE id=?2",
            params![serde_json::to_string(value)?, id],
        )?;
        Ok(())
    }

    pub fn finish(
        &self,
        id: i64,
        publish: impl FnOnce() -> Result<(std::path::PathBuf, u64)>,
    ) -> Result<()> {
        let conn = self.conn()?;
        if conn.query_row("SELECT cancel FROM jobs WHERE id=?1", [id], |row| {
            row.get::<_, bool>(0)
        })? {
            bail!("Operação cancelada.");
        }
        let (path, bytes) = publish()?;
        conn.execute(
            "UPDATE jobs SET status='done', progress=1, output=?1, output_bytes=?2 WHERE id=?3",
            params![path.to_string_lossy(), bytes, id],
        )?;
        Ok(())
    }

    pub fn fail(&self, id: i64, status: &str, message: &str) -> Result<()> {
        self.conn()?.execute(
            "UPDATE jobs SET status=?1, progress=NULL, error=?2 WHERE id=?3",
            params![status, message, id],
        )?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SourceKind;

    #[test]
    fn recovers_interruption_and_preserves_waiting_jobs() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let db = dir.path().join("jobs.sqlite3");
        let source = Source {
            kind: SourceKind::Url,
            location: "https://example.org/film.mp4".into(),
        };
        let store = Store::open(&db)?;
        assert!(Store::open(&db).is_err());
        store.add(&[source.clone(), source], Preset::Mp4, dir.path())?;
        let running = store.claim()?.unwrap();
        drop(store);
        let reopened = Store::open(&db)?;
        assert_eq!(reopened.find(running.id)?.status, "interrupted");
        assert_eq!(reopened.claim()?.unwrap().id, running.id + 1);
        reopened.retry(running.id)?;
        assert_eq!(reopened.claim()?.unwrap().id, running.id);
        reopened.cancel(running.id)?;
        assert!(reopened.cancelled(running.id)?);
        assert!(reopened
            .finish(running.id, || panic!("publicação de trabalho cancelado"))
            .is_err());
        Ok(())
    }
}
