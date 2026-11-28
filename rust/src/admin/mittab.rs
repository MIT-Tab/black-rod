use std::{sync::LazyLock, time::Duration};

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use regex::Regex;
use reqwest::Client;
use scraper::{Html as Document, Selector};
use serde::Serialize;

use crate::{AppState, web};

static CLIENT: LazyLock<Client> = LazyLock::new(|| {
    Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .expect("MIT-Tab HTTP client")
});

#[derive(Debug, PartialEq, Serialize)]
struct Tournament {
    name: String,
    url: String,
}

fn tournaments(html: &str) -> Option<Vec<Tournament>> {
    static SITE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^(.*?)\.nu-tab\.com$").expect("tournament subdomain"));
    let content = Selector::parse("div#content").expect("content selector");
    let links = Selector::parse("a[href]").expect("link selector");
    let page = Document::parse_document(html);
    let content = page.select(&content).next()?;
    Some(
        content
            .select(&links)
            .filter_map(|anchor| {
                let text = anchor.text().map(str::trim).collect::<String>();
                let name = SITE.captures(&text)?.get(1)?.as_str().to_owned();
                let href = anchor.attr("href")?;
                let url = if href.starts_with("http") {
                    href.to_owned()
                } else {
                    format!("http://{text}")
                };
                Some(Tournament { name, url })
            })
            .collect(),
    )
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let user = match super::require(&state, &uri, &headers).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let source = std::env::var("NU_TAB_URL").unwrap_or_else(|_| "https://nu-tab.com".to_owned());
    let data = async {
        CLIENT
            .get(&source)
            .send()
            .await?
            .error_for_status()?
            .text()
            .await
    }
    .await;
    let (tournaments, error_message) = match data {
        Ok(html) => match tournaments(&html) {
            Some(tournaments) => (tournaments, None),
            None => (
                Vec::new(),
                Some("Content div not found in the response".to_owned()),
            ),
        },
        Err(error) => (
            Vec::new(),
            Some(format!("Failed to fetch data from nu-tab.com: {error}")),
        ),
    };
    let common = web::base_context(&state, &user, &uri);
    match web::render(
        "admin/mittab_dashboard.html",
        context! {
            tournaments, error_message, nu_tab_url=>source, ..common
        },
    ) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("MIT-Tab dashboard template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_anchors_in_order_and_resolves_relative_links_to_the_displayed_host() {
        let data = r#"<a href="/outside">outside.nu-tab.com</a>
            <div id="content">
                <a href="/invalid">invalid.test</a>
                <a href="/alpha"> alpha<em>.nu-tab.com</em> </a>
                <a href="https://beta.example.test/mittab">beta.nu-tab.com</a>
                <a>no-href.nu-tab.com</a>
            </div>"#;
        assert_eq!(
            tournaments(data),
            Some(vec![
                Tournament {
                    name: "alpha".into(),
                    url: "http://alpha.nu-tab.com".into()
                },
                Tournament {
                    name: "beta".into(),
                    url: "https://beta.example.test/mittab".into()
                },
            ])
        );
        assert_eq!(tournaments("<div id='content'></div>"), Some(Vec::new()));
        assert_eq!(tournaments("<p>Missing content</p>"), None);
    }
}
