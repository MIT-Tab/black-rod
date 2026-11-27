use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, LazyLock},
};

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use regex::Regex;
use serde::Serialize;
use sqlx::{AnyPool, Row};
use tokio_stream::StreamExt;

use crate::{ApiError, AppState, session, web};

static TOKENS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"[\p{L}\p{N}_]+(\.?[\p{L}\p{N}_]+)*").expect("valid search tokenizer")
});
static PHRASES: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#""[^"\n]*""#).expect("valid search phrase regex"));
const STOP_WORDS: &[&str] = &[
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from", "have", "if", "in",
    "is", "it", "may", "not", "of", "on", "or", "tbd", "that", "the", "this", "to", "us", "we",
    "when", "will", "with", "yet", "you", "your",
];
const PAGE_SIZE: usize = 20;
const SOURCE: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, TRIM(d.first_name || ' ' || d.last_name) AS name,
        CAST(s.id AS BIGINT) AS school_id, s.name AS school
    FROM core_debater d LEFT JOIN core_school s ON s.id = d.school_id
    WHERE NOT d.temporary AND NOT d.synthetic ORDER BY d.id
";

fn terms(input: &str) -> Vec<String> {
    TOKENS
        .find_iter(input)
        .filter_map(|word| {
            let term = word.as_str().to_lowercase();
            if term.chars().count() < 2 || STOP_WORDS.contains(&term.as_str()) {
                None
            } else {
                Some(porter_stemmer::stem(&term))
            }
        })
        .collect()
}

#[derive(PartialEq, Eq, Hash)]
struct Clause {
    phrase: bool,
    negative: bool,
    terms: Vec<String>,
}

fn parse(input: &str) -> Vec<Clause> {
    fn unquoted(out: &mut Vec<Clause>, text: &str) {
        for word in text.split_whitespace() {
            let negative = word.starts_with('-') && word.len() > 1;
            let analyzed = terms(if negative { &word[1..] } else { word });
            out.extend(analyzed.into_iter().map(|term| Clause {
                phrase: false,
                negative,
                terms: vec![term],
            }));
        }
    }
    let mut clauses = Vec::new();
    let mut end = 0;
    for phrase in PHRASES.find_iter(input) {
        unquoted(&mut clauses, &input[end..phrase.start()]);
        let text = phrase.as_str();
        let terms = terms(&text[1..text.len() - 1]);
        if !terms.is_empty() {
            clauses.push(Clause {
                phrase: true,
                negative: false,
                terms,
            });
        }
        end = phrase.end();
    }
    unquoted(&mut clauses, &input[end..]);
    // The reference query optimizer combines repeated terms.
    let mut seen = HashSet::new();
    clauses.retain(|clause| seen.insert((clause.phrase, clause.negative, clause.terms.clone())));
    clauses
}

#[derive(Serialize)]
struct School {
    name: String,
    url: String,
}

#[derive(Serialize)]
struct Record {
    id: i64,
    name: String,
    url: String,
    school: Option<School>,
    #[serde(skip)]
    terms: Vec<String>,
}

#[derive(Default)]
pub(crate) struct SearchIndex {
    records: Vec<Record>,
    postings: HashMap<String, Vec<usize>>,
    average_length: f64,
}

impl SearchIndex {
    async fn load(pool: &AnyPool) -> Result<Self, ApiError> {
        let mut result = Self::default();
        let mut cursor = sqlx::query(SOURCE).fetch(pool);
        let mut total = 0_usize;
        while let Some(row) = cursor.next().await {
            let row = row?;
            let id = row.try_get::<i64, _>("id")?;
            let name = row.try_get::<String, _>("name")?;
            let school = row
                .try_get::<Option<i64>, _>("school_id")?
                .map(|school_id| -> Result<School, sqlx::Error> {
                    Ok(School {
                        name: row.try_get("school")?,
                        url: format!("/core/schools/{school_id}"),
                    })
                })
                .transpose()?;
            let mut document = terms(&name);
            if let Some(school) = &school {
                document.extend(terms(&school.name));
            }
            let position = result.records.len();
            for token in document.iter().collect::<HashSet<_>>() {
                result
                    .postings
                    .entry(token.clone())
                    .or_default()
                    .push(position);
            }
            total += document.len();
            result.records.push(Record {
                id,
                name,
                school,
                terms: document,
                url: format!("/core/debaters/{id}"),
            });
        }
        result.average_length =
            (total as f64 / result.records.len().max(1) as f64).max(f64::EPSILON);
        Ok(result)
    }

    fn bm25(&self, term: &str, doc: &Record) -> f64 {
        let count = self.postings.get(term).map_or(0, Vec::len);
        let idf = (self.records.len() as f64 / (count + 1) as f64).ln() + 1.0;
        let frequency = doc
            .terms
            .iter()
            .filter(|token| token.as_str() == term)
            .count() as f64;
        let length = doc.terms.len() as f64;
        idf * (frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / self.average_length)))
    }

    fn results(&self, query: &str) -> Vec<&Record> {
        let clauses = parse(query);
        if clauses.is_empty() {
            return Vec::new();
        }
        let required = clauses
            .iter()
            .filter(|clause| !clause.negative)
            .flat_map(|clause| &clause.terms);
        let rarest = required.min_by_key(|term| self.postings.get(*term).map_or(0, Vec::len));
        let candidates: Box<dyn Iterator<Item = usize>> = if let Some(rarest) = rarest {
            Box::new(self.postings.get(rarest).into_iter().flatten().copied())
        } else {
            Box::new(0..self.records.len())
        };
        let mut scored = Vec::new();
        for id in candidates {
            let doc = &self.records[id];
            let matched = clauses.iter().all(|clause| {
                let found = if clause.phrase {
                    doc.terms
                        .windows(clause.terms.len())
                        .any(|portion| portion == clause.terms)
                } else {
                    doc.terms.contains(&clause.terms[0])
                };
                found != clause.negative
            });
            if matched {
                let score = clauses
                    .iter()
                    .filter(|clause| !clause.negative)
                    .flat_map(|clause| &clause.terms)
                    .map(|term| self.bm25(term, doc))
                    .sum::<f64>();
                scored.push((score, doc));
            }
        }
        scored.sort_by(|(left, a), (right, b)| right.total_cmp(left).then_with(|| a.id.cmp(&b.id)));
        scored.into_iter().map(|(_, doc)| doc).collect()
    }
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let fields = crate::login::form_values(uri.query().unwrap_or_default().as_bytes());
    let query = fields
        .get("q")
        .map(|value| value.trim())
        .unwrap_or_default();
    let page_number = match fields.get("page") {
        Some(value) => match value.trim().parse::<usize>() {
            Ok(value) if value > 0 => value,
            _ => return StatusCode::NOT_FOUND.into_response(),
        },
        None => 1,
    };
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let index = if query.is_empty()
        || fields
            .get("models")
            .is_some_and(|model| model != "core.debater")
    {
        Arc::new(SearchIndex::default())
    } else {
        match state
            .search
            .try_get_with((), async {
                SearchIndex::load(&state.pool).await.map(Arc::new)
            })
            .await
        {
            Ok(index) => index,
            Err(error) => {
                eprintln!("Search index build failed: {error:?}");
                return StatusCode::INTERNAL_SERVER_ERROR.into_response();
            }
        }
    };
    let results = index.results(query);
    let Some(offset) = (page_number - 1).checked_mul(PAGE_SIZE) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    if page_number > 1 && offset >= results.len() {
        return StatusCode::NOT_FOUND.into_response();
    }
    let page = context! {
        object_list => results.iter().skip(offset).take(PAGE_SIZE).map(|record| context!{object => record}).collect::<Vec<_>>(),
        has_previous => page_number > 1,
        has_next => offset + PAGE_SIZE < results.len(),
        previous_page_number => page_number.saturating_sub(1),
        next_page_number => page_number + 1,
    };
    let common = web::base_context(&state, &user, &uri);
    match web::render("search/search.html", context! {page,query,..common}) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Search template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
