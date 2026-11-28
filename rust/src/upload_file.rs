use std::io::{self, Read, Seek};

use axum::{extract::multipart::Field, http::StatusCode};
use tokio::io::AsyncWriteExt;

pub(crate) struct UploadFile {
    file: std::fs::File,
    pub(crate) empty: bool,
}

impl UploadFile {
    pub(crate) async fn stage(mut field: Field<'_>) -> Result<Self, StatusCode> {
        let status = StatusCode::INTERNAL_SERVER_ERROR;
        let file = tokio::task::spawn_blocking(tempfile::tempfile)
            .await
            .map_err(|_| status)?
            .map_err(|_| status)?;
        let mut file = tokio::fs::File::from_std(file);
        let mut empty = true;
        while let Some(chunk) = field.chunk().await.map_err(|error| error.status())? {
            file.write_all(&chunk).await.map_err(|_| status)?;
            empty &= chunk.is_empty();
        }
        file.flush().await.map_err(|_| status)?;
        Ok(Self {
            file: file.into_std().await,
            empty,
        })
    }

    pub(crate) async fn parse<T: Send + 'static>(
        self,
        parse: impl FnOnce(&[u8]) -> T + Send + 'static,
    ) -> io::Result<T> {
        self.parse_reader(move |mut file| {
            let mut contents = Vec::new();
            file.read_to_end(&mut contents)?;
            Ok(parse(&contents))
        })
        .await
    }

    pub(crate) async fn parse_reader<T: Send + 'static>(
        self,
        parse: impl FnOnce(std::fs::File) -> io::Result<T> + Send + 'static,
    ) -> io::Result<T> {
        tokio::task::spawn_blocking(move || {
            let mut file = self.file;
            file.rewind()?;
            parse(file)
        })
        .await
        .map_err(io::Error::other)?
    }
}

pub(crate) async fn text(mut field: Field<'_>) -> Result<String, StatusCode> {
    let mut bytes = Vec::new();
    while let Some(chunk) = field.chunk().await.map_err(|error| error.status())? {
        if bytes.len() + chunk.len() > 2_621_440 {
            return Err(StatusCode::PAYLOAD_TOO_LARGE);
        }
        bytes.extend_from_slice(&chunk);
    }
    String::from_utf8(bytes).map_err(|_| StatusCode::BAD_REQUEST)
}
