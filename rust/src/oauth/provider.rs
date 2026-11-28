use std::time::Duration;

use oauth2::{
    AccessToken, AuthType, AuthorizationCode, Client, ClientId, ClientSecret, EndpointNotSet,
    EndpointSet, RedirectUrl, RefreshToken, Scope, StandardRevocableToken, TokenResponse, TokenUrl,
    basic::{
        BasicErrorResponse, BasicRevocationErrorResponse, BasicTokenIntrospectionResponse,
        BasicTokenType,
    },
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use url::Url;

const CALLBACK: &str = "/accounts/apdaonline/login/callback/";
const DEFAULT_BASE: &str = "https://apda.online/";

type TokenClient = Client<
    BasicErrorResponse,
    ProviderToken,
    BasicTokenIntrospectionResponse,
    StandardRevocableToken,
    BasicRevocationErrorResponse,
    EndpointNotSet,
    EndpointNotSet,
    EndpointNotSet,
    EndpointNotSet,
    EndpointSet,
>;

pub struct OAuth {
    token: TokenClient,
    authorize: Url,
    profile: Url,
    http: reqwest::Client,
}

impl OAuth {
    pub fn new(
        client_id: String,
        secret: String,
        base: Option<String>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let base = Url::parse(base.as_deref().unwrap_or(DEFAULT_BASE))?;
        let loopback = matches!(base.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"));
        if !(base.scheme() == "https" || (base.scheme() == "http" && loopback))
            || base.host().is_none()
        {
            return Err("Invalid APDAOnline OAuth base URL".into());
        }
        let token = Client::new(ClientId::new(client_id))
            .set_client_secret(ClientSecret::new(secret))
            .set_token_uri(TokenUrl::from_url(base.join("oauth/token")?))
            .set_auth_type(AuthType::RequestBody);
        Ok(Self {
            token,
            authorize: base.join("oauth/authorize")?,
            profile: base.join("oauth/me")?,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(30))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
        })
    }

    pub(super) fn client_id(&self) -> &str {
        self.token.client_id().as_str()
    }

    pub(super) fn authorization_url(&self, origin: &str, state: &str) -> Url {
        // Keep the allauth redirect parameters, including an empty scope.
        let mut url = self.authorize.clone();
        url.query_pairs_mut()
            .append_pair("client_id", self.client_id())
            .append_pair("redirect_uri", &format!("{origin}{CALLBACK}"))
            .append_pair("scope", "")
            .append_pair("response_type", "code")
            .append_pair("state", state);
        url
    }

    pub(super) async fn profile(
        &self,
        origin: &str,
        code: &str,
    ) -> Option<(Token, Value, Profile)> {
        let callback = RedirectUrl::new(format!("{origin}{CALLBACK}")).ok()?;
        let response = self
            .token
            .exchange_code(AuthorizationCode::new(code.to_owned()))
            .set_redirect_uri(std::borrow::Cow::Borrowed(&callback))
            .request_async(&self.http)
            .await
            .ok()?;
        let fields = self
            .http
            .get(self.profile.clone())
            .bearer_auth(response.access_token().secret())
            .send()
            .await
            .ok()?
            .error_for_status()
            .ok()?
            .json::<Value>()
            .await
            .ok()?;
        let profile = serde_json::from_value(fields.clone()).ok()?;
        let token = Token {
            access_token: response.access_token.secret().clone(),
            refresh_token: response.refresh_token.secret().clone(),
            expires_in: response.expires_in,
        };
        Some((token, fields, profile))
    }
}

// APDAOnline's legacy response can omit token_type. Its profile API uses Bearer.
#[derive(Debug, Deserialize, Serialize)]
struct ProviderToken {
    access_token: AccessToken,
    #[serde(default = "empty_refresh_token")]
    refresh_token: RefreshToken,
    expires_in: Option<i64>,
}

fn empty_refresh_token() -> RefreshToken {
    RefreshToken::new(String::new())
}

impl TokenResponse for ProviderToken {
    type TokenType = BasicTokenType;

    fn access_token(&self) -> &AccessToken {
        &self.access_token
    }

    fn token_type(&self) -> &BasicTokenType {
        &BasicTokenType::Bearer
    }

    fn expires_in(&self) -> Option<Duration> {
        Some(Duration::from_secs(self.expires_in?.try_into().ok()?))
    }

    fn refresh_token(&self) -> Option<&RefreshToken> {
        (!self.refresh_token.secret().is_empty()).then_some(&self.refresh_token)
    }

    fn scopes(&self) -> Option<&Vec<Scope>> {
        None
    }
}

pub(super) struct Token {
    pub(super) access_token: String,
    pub(super) refresh_token: String,
    pub(super) expires_in: Option<i64>,
}

#[derive(Deserialize)]
pub(super) struct Profile {
    #[serde(rename = "ID")]
    id: Value,
    pub(super) user_nicename: String,
    pub(super) user_email: Option<String>,
    pub(super) display_name: String,
    user_roles: Vec<String>,
}

impl Profile {
    pub(super) fn uid(&self) -> Option<String> {
        match &self.id {
            Value::String(id) if id.bytes().all(|byte| byte.is_ascii_digit()) && !id.is_empty() => {
                Some(id.clone())
            }
            Value::Number(id) if id.as_u64().is_some() => Some(id.to_string()),
            _ => None,
        }
    }

    pub(super) fn can_view_video(&self) -> bool {
        self.user_roles
            .iter()
            .any(|role| role == "private_side_viewer")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        Json, Router,
        http::{HeaderMap, header},
        routing::{get, post},
    };
    use serde_json::json;
    use tokio::net::TcpListener;

    #[tokio::test]
    async fn exchanges_codes_with_apdaonline_legacy_token_responses() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let callback = format!("{base}{CALLBACK}");
        let routes = Router::new()
            .route(
                "/oauth/token",
                post(move |body: String| {
                    let callback = callback.clone();
                    async move {
                        let fields: std::collections::HashMap<_, _> =
                            url::form_urlencoded::parse(body.as_bytes())
                                .into_owned()
                                .collect();
                        for (key, expected) in [
                            ("client_id", "fixture-client"),
                            ("client_secret", "fixture-secret"),
                            ("grant_type", "authorization_code"),
                            ("code", "fixture-code"),
                            ("redirect_uri", callback.as_str()),
                        ] {
                            assert_eq!(fields.get(key).map(String::as_str), Some(expected));
                        }
                        Json(json!({"access_token": "fixture-access", "expires_in": 600}))
                    }
                }),
            )
            .route(
                "/oauth/me",
                get(|headers: HeaderMap| async move {
                    assert_eq!(headers[header::AUTHORIZATION], "Bearer fixture-access");
                    Json(json!({
                        "ID": 42,
                        "user_nicename": "fixture",
                        "user_email": "fixture@example.test",
                        "display_name": "Fixture Person",
                        "user_roles": ["private_side_viewer"]
                    }))
                }),
            );
        let server = tokio::spawn(async move { axum::serve(listener, routes).await.unwrap() });
        let provider = OAuth::new(
            "fixture-client".into(),
            "fixture-secret".into(),
            Some(base.clone()),
        )
        .unwrap();
        let url = provider.authorization_url(&base, "fixture-state");
        assert_eq!(
            url.query_pairs().find(|(key, _)| key == "scope").unwrap().1,
            ""
        );
        let (token, _, profile) = provider.profile(&base, "fixture-code").await.unwrap();
        assert_eq!(token.access_token, "fixture-access");
        assert_eq!(token.refresh_token, "");
        assert_eq!(token.expires_in, Some(600));
        assert_eq!(profile.uid().as_deref(), Some("42"));
        assert!(profile.can_view_video());
        server.abort();
    }
}
