# Forum profile links

The WordPress forum calls `POST /api/private/forum-profiles/` with a Bearer
`FORUM_PROFILE_API_TOKEN` and JSON `{"wordpress_user_id": 123}`. This is a
server-to-server integration; it is excluded from the public API schema and
rejects browser sessions without the credential. Responses must not be cached
by proxies. Only public debater IDs, names, and schools are returned.

The lookup uses the `apdaonline` SocialAccount provider's WordPress user ID and
approved `Debater.user` claims. It returns all permanent, nonsynthetic profiles
owned by active linked results accounts. No migrations, backfill, email matching,
or new login are required. Pending claim requests do not grant a link.

## Deployment

Deploy this backend before MIT-Tab/apda-forum's results-profile integration.
Configure a random 32-byte hex credential as the repository Actions secret
`FORUM_PROFILE_API_TOKEN`. The existing Linode deployment copies only that key
into the application's `.env` before its normal restart, retaining that file's
owner and permissions. An absent secret leaves existing server configuration
alone; an absent application setting disables the endpoint (401).

Configure WordPress's `APDA_RESULTS_PROFILE_TOKEN` constant to the same value
using a protected file outside the web root. Never put the credential in plugin
source, browser JavaScript, PR descriptions, screenshots, or logs. Rotate both
sides together; removing the constant disables the forum integration.

The forum caches successful links for five minutes, empty results for one minute,
and failures for thirty seconds. Unlinking or changing an approved claim is
reflected after that cache expires. Existing forum visibility permissions remain
in force. An outage omits the links without breaking the rest of the profile.
