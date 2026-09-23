# WordPress publishing runbook — TopSitesPoker.com

**Status:** connection confirmed working 2026-09-23. `topsitespoker.com` is reachable
from the "WP · topsitespoker.com" cloud environment; `WP_USER=optinbird` (role:
administrator) and `WP_APP_PASSWORD` are valid — `upload_files`, `edit_posts` and
`publish_posts` all `True`. **One env var is still missing**: `WP_SITE_URL` was not
set, so add `WP_SITE_URL=https://topsitespoker.com` (no `www`, which is not in the
Allowed domains list) to the environment's variables and start a fresh session
afterward — env var changes only apply to new sessions.

This repo (`optinbird/topsitespoker-ops`) is the working copy for claude.ai/code
sessions on this site: it's a plain GitHub repo, separate from the TopSitesPoker.com
knowledge project (docs/content-plan spreadsheets), which claude.ai/code sessions
can't read. Keep this repo's `wp_publish.py`/`posts/` as the source of truth for
anything a Code session runs; copy updates back to the knowledge project by hand if
both need to stay in sync.

---

## Why this exists

This is a site-specific copy of the general **wp-publish** pattern (a Claude skill),
built so the same publishing workflow can scale to many WordPress sites without
sharing credentials or rewriting the script per site. Per site:

- **One cloud environment**: holds `topsitespoker.com` in Allowed domains, and
  `WP_SITE_URL`, `WP_USER`, `WP_APP_PASSWORD` as environment variables. Nothing about
  any other site lives in this environment.
- **One Claude project** (this one): holds this file, `wp_publish.py`, and the post
  manifests in `posts/`. The project's settings should point at the dedicated
  environment above so every thread in this project already has the right domain
  and credentials without picking anything manually.
- **The wp-publish skill** (shared, account-level): the instructions for restoring
  this toolkit and running it. Identical across every site's project.

## Environment variables this script needs

| Variable          | Example                        |
| ------------------ | ------------------------------- |
| `WP_SITE_URL`      | `https://topsitespoker.com`     |
| `WP_USER`           | the WP username the app password belongs to |
| `WP_APP_PASSWORD`  | the Application Password value (spaces are fine) |

Set these in the environment dialog (Network access **Custom** + `topsitespoker.com`
in Allowed domains, plus these three lines under Environment variables). Changes only
apply to sessions started after you save — a session already running keeps its old
settings.

## Check the connection

```bash
python3 wp_publish.py --check
```

Expect `users/me: 200` with `upload_files: True` and `edit_posts: True`. A
`BLOCKED: could not reach ...` message almost always means the domain isn't in this
environment's Allowed domains yet, or this session started before you added it.

## Publish

```bash
python3 wp_publish.py --manifest posts/<id>-<slug>.json --dry-run
python3 wp_publish.py --manifest posts/<id>-<slug>.json
```

Always read the dry run first. At the end of a real run, check the verify block:
every image should say `FOUND`, and `placeholders left on the page: none`.

## Writing a manifest

Copy `posts/_template.json` and `posts/_template-body.html`. See the comment at the
top of `_template-body.html` for the image-placeholder convention
(`INSERT-MEDIA-URL:<file>`).

Per image, four fields are always filled in: `alt` (for readers who can't see the
image — never a keyword dump), `caption` (printed under the image), `title` (Media
Library title), `description` (an internal note: which article/section, and its
rendered width).

Per post: `title`, `slug`, `excerpt`, `status`, `categories`, `tags`,
`featured_image`, `body_file`, and the three `rank_math` fields. Omit `post.id` to
create a new post instead of updating one. `categories`/`tags` must already exist on
the site — the script warns and skips anything it can't match rather than creating
new terms.

### Exposing RankMath fields over REST

By default RankMath's SEO fields aren't in the REST response, so the script prints
them for manual entry instead of silently dropping them. To make them travel over
REST, add this to a small must-use plugin (`wp-content/mu-plugins/rankmath-rest.php`)
on the WordPress site:

```php
<?php
add_action('init', function () {
    $fields = [
        'rank_math_title'          => 'string',
        'rank_math_description'    => 'string',
        'rank_math_focus_keyword'  => 'string',
    ];
    foreach ($fields as $key => $type) {
        register_post_meta('post', $key, [
            'show_in_rest' => true,
            'single'       => true,
            'type'         => $type,
            'auth_callback' => function () {
                return current_user_can('edit_posts');
            },
        ]);
    }
});
```

## Rules that are not negotiable

- Never publish a body containing `INSERT-MEDIA-URL`, `[Reviewer name…]`,
  `[Month DD…]` or `[Editor: …]` — the script refuses on its own, this is belt and
  suspenders.
- Never invent a slug or a category/tag. Unknown taxonomy terms are skipped with a
  warning, never auto-created.
- The script backs up the current body to `posts/backups/` before overwriting an
  existing post. Keep those backups, and keep this project's copy of `posts/` as the
  source of truth for what's been sent.

## After publishing

Update the status line at the top of this file (what's live, which manifest/body
version, what's still outstanding), and note the live + edit links here so the next
session doesn't have to look them up again.
