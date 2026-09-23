#!/usr/bin/env python3
"""
wp_publish.py — site-agnostic WordPress REST API publisher.

Generalized from the manifest-driven publisher originally built for
MyDogLucky.com. Works with any WordPress site reachable over the REST API
(wp-json/wp/v2), authenticated with a WordPress Application Password.
Nothing here is specific to one site — the site itself comes entirely from
environment variables, so the same script can live in every site's project.

Required environment variables:
    WP_SITE_URL       e.g. https://topsitespoker.com  (no trailing slash needed)
    WP_USER            the WordPress username the application password belongs to
    WP_APP_PASSWORD    the application password value, exactly as WordPress showed
                        it (spaces are fine — WordPress ignores them on its side)

Usage:
    python3 wp_publish.py --check
    python3 wp_publish.py --manifest posts/0001-example-slug.json --dry-run
    python3 wp_publish.py --manifest posts/0001-example-slug.json

Manifest format: see the accompanying runbook / posts/_template.json.
"""

import argparse
import json
import mimetypes
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    print("ERROR: the 'requests' package is not installed.", file=sys.stderr)
    print("Install it with: pip install requests --break-system-packages", file=sys.stderr)
    sys.exit(1)

# Body text that must never reach a live post — placeholders left by drafting.
FORBIDDEN_PLACEHOLDERS = [
    "INSERT-MEDIA-URL",
    "[Reviewer name",
    "[Month DD",
    "[Editor:",
]

REQUIRED_IMAGE_FIELDS = ("alt", "caption", "title", "description")


# --------------------------------------------------------------------------- #
# Environment / session
# --------------------------------------------------------------------------- #

def env(name, required=True):
    val = os.environ.get(name)
    if required and not val:
        print(f"ERROR: environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def site_url():
    return env("WP_SITE_URL").rstrip("/")


def api_base():
    return f"{site_url()}/wp-json/wp/v2/"


def new_session():
    user = env("WP_USER")
    app_pw = env("WP_APP_PASSWORD")
    s = requests.Session()
    s.auth = (user, app_pw)
    s.headers.update({"User-Agent": "wp-publish/1.0 (+claude)"})
    return s


# --------------------------------------------------------------------------- #
# --check
# --------------------------------------------------------------------------- #

def check_connection():
    print(f"site: {site_url()}")
    s = new_session()
    url = urljoin(api_base(), "users/me")
    try:
        r = s.get(url, params={"context": "edit"}, timeout=20)
    except requests.RequestException as e:
        print(f"BLOCKED: could not reach {site_url()} — {e}")
        print(
            "If this looks like a connection/proxy error rather than a WordPress "
            "error, the cloud environment likely needs this domain added to "
            "Allowed domains (Network access -> Custom) before this can work."
        )
        sys.exit(1)

    print(f"users/me: {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        sys.exit(1)

    data = r.json()
    caps = data.get("capabilities", {}) or {}
    upload_files = bool(caps.get("upload_files"))
    edit_posts = bool(caps.get("edit_posts"))
    print(f"user: {data.get('name')} (id {data.get('id')})")
    print(f"upload_files: {upload_files}")
    print(f"edit_posts: {edit_posts}")

    if not (upload_files and edit_posts):
        print("WARNING: this user is missing a capability this script needs.")
        sys.exit(1)

    print("Connection OK.")


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #

def find_media_by_filename(s, filename):
    """Look for an existing Media Library item with this filename (skip re-upload)."""
    stem = Path(filename).stem
    url = urljoin(api_base(), "media")
    r = s.get(url, params={"search": stem, "per_page": 20}, timeout=20)
    r.raise_for_status()
    for item in r.json():
        src_name = Path(item.get("source_url", "")).name
        if src_name.startswith(stem):
            return item
    return None


def upload_image(s, image_spec, base_dir, dry_run):
    file_path = (base_dir / image_spec["file"]).resolve()
    if not file_path.exists():
        print(f"  MISSING local file: {file_path}")
        return None

    existing = find_media_by_filename(s, file_path.name)
    if existing:
        print(f"  SKIP (already in Media Library): {file_path.name} -> id {existing['id']}")
        media = existing
    else:
        if dry_run:
            print(f"  WOULD UPLOAD: {file_path.name}")
            return {"id": None, "source_url": f"[dry-run:{file_path.name}]"}
        mime, _ = mimetypes.guess_type(str(file_path))
        headers = {
            "Content-Disposition": f'attachment; filename="{file_path.name}"',
            "Content-Type": mime or "application/octet-stream",
        }
        with open(file_path, "rb") as f:
            r = s.post(urljoin(api_base(), "media"), headers=headers, data=f.read(), timeout=60)
        if r.status_code not in (200, 201):
            print(f"  UPLOAD FAILED ({r.status_code}) for {file_path.name}: {r.text[:300]}")
            return None
        media = r.json()
        print(f"  UPLOADED: {file_path.name} -> id {media['id']}")

    if not dry_run and media.get("id"):
        meta_payload = {
            "alt_text": image_spec.get("alt", ""),
            "caption": image_spec.get("caption", ""),
            "title": image_spec.get("title", ""),
            "description": image_spec.get("description", ""),
        }
        r2 = s.post(urljoin(api_base(), f"media/{media['id']}"), json=meta_payload, timeout=20)
        if r2.status_code not in (200, 201):
            print(f"  META UPDATE FAILED for id {media['id']}: {r2.text[:300]}")

    return media


def substitute_media_placeholders(body, image_specs, uploaded):
    for spec, media in zip(image_specs, uploaded):
        if not media:
            continue
        token = f"INSERT-MEDIA-URL:{spec['file']}"
        url = media.get("source_url", token)
        body = body.replace(token, url)
    return body


def find_forbidden_placeholders(body):
    return [token for token in FORBIDDEN_PLACEHOLDERS if token in body]


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #

def resolve_terms(s, taxonomy, names):
    """Match existing terms by exact name (case-insensitive). Never creates new ones."""
    ids = []
    for name in names or []:
        url = urljoin(api_base(), taxonomy)
        r = s.get(url, params={"search": name, "per_page": 20}, timeout=20)
        r.raise_for_status()
        match = next((t for t in r.json() if t["name"].lower() == name.lower()), None)
        if match:
            ids.append(match["id"])
        else:
            label = taxonomy[:-1] if taxonomy.endswith("s") else taxonomy
            print(f"  WARNING: unknown {label} '{name}' — skipping (not creating new terms).")
    return ids


# --------------------------------------------------------------------------- #
# Posts
# --------------------------------------------------------------------------- #

def find_post_by_slug(s, slug):
    url = urljoin(api_base(), "posts")
    r = s.get(url, params={"slug": slug, "status": "any", "context": "edit"}, timeout=20)
    r.raise_for_status()
    results = r.json()
    return results[0] if results else None


def backup_existing_body(s, existing_post, backups_dir, slug):
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backups_dir / f"{slug}-{stamp}.html"
    body = (existing_post.get("content") or {}).get("rendered", "")
    backup_path.write_text(body, encoding="utf-8")
    print(f"  Backed up existing body -> {backup_path}")


def publish(manifest_path, dry_run):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_dir = manifest_path.resolve().parent

    post_spec = manifest["post"]
    image_specs = manifest.get("images", [])

    body_path = base_dir / post_spec["body_file"]
    if not body_path.exists():
        print(f"ERROR: body file not found: {body_path}")
        sys.exit(1)
    body = body_path.read_text(encoding="utf-8")

    s = new_session()

    print(f"--- {'DRY RUN' if dry_run else 'PUBLISH'}: {post_spec.get('slug')} ({site_url()}) ---")

    print("Images:")
    uploaded = []
    for spec in image_specs:
        missing_fields = [f for f in REQUIRED_IMAGE_FIELDS if not spec.get(f)]
        if missing_fields:
            print(f"  ERROR: image {spec.get('file')} is missing required field(s): {missing_fields}")
            sys.exit(1)
        uploaded.append(upload_image(s, spec, base_dir, dry_run))

    body = substitute_media_placeholders(body, image_specs, uploaded)

    forbidden = find_forbidden_placeholders(body)
    if forbidden:
        print(f"ERROR: body still contains placeholder(s): {forbidden}. Refusing to publish.")
        sys.exit(1)

    print("Taxonomy:")
    category_ids = resolve_terms(s, "categories", post_spec.get("categories", []))
    tag_ids = resolve_terms(s, "tags", post_spec.get("tags", []))

    featured_media_id = None
    if post_spec.get("featured_image"):
        key = post_spec["featured_image"]
        match = next((u for spec, u in zip(image_specs, uploaded) if spec["file"] == key), None)
        if match:
            featured_media_id = match.get("id")
        else:
            print(f"  WARNING: featured_image '{key}' does not match any entry in images[].")

    existing = None
    if post_spec.get("id"):
        r = s.get(urljoin(api_base(), f"posts/{post_spec['id']}"), params={"context": "edit"}, timeout=20)
        if r.status_code == 200:
            existing = r.json()
    elif post_spec.get("slug"):
        existing = find_post_by_slug(s, post_spec["slug"])

    payload = {
        "title": post_spec["title"],
        "slug": post_spec["slug"],
        "excerpt": post_spec.get("excerpt", ""),
        "status": post_spec.get("status", "draft"),
        "content": body,
    }
    if category_ids:
        payload["categories"] = category_ids
    if tag_ids:
        payload["tags"] = tag_ids
    if featured_media_id:
        payload["featured_media"] = featured_media_id

    rank_math = post_spec.get("rank_math", {}) or {}
    meta = {}
    if rank_math.get("seo_title"):
        meta["rank_math_title"] = rank_math["seo_title"]
    if rank_math.get("seo_description"):
        meta["rank_math_description"] = rank_math["seo_description"]
    if rank_math.get("focus_keyword"):
        meta["rank_math_focus_keyword"] = rank_math["focus_keyword"]
    if meta:
        payload["meta"] = meta

    if dry_run:
        preview = {k: (f"<{len(v)} chars>" if k == "content" else v) for k, v in payload.items()}
        print("Would send payload:")
        print(json.dumps(preview, indent=2, ensure_ascii=False))
        result_id = None
        result_link = "(dry run — nothing saved)"
    else:
        if existing:
            backup_existing_body(s, existing, base_dir / "backups", post_spec["slug"])
            r = s.post(urljoin(api_base(), f"posts/{existing['id']}"), json=payload, timeout=30)
        else:
            r = s.post(urljoin(api_base(), "posts"), json=payload, timeout=30)

        if r.status_code not in (200, 201):
            print(f"ERROR: post save failed ({r.status_code}): {r.text[:500]}")
            sys.exit(1)
        result = r.json()
        result_id = result["id"]
        result_link = result.get("link")
        print(f"Saved post id {result_id}: {result_link}")

        if meta:
            r2 = s.get(urljoin(api_base(), f"posts/{result_id}"), params={"context": "edit"}, timeout=20)
            saved_meta = (r2.json().get("meta") or {}) if r2.status_code == 200 else {}
            not_saved = [k for k in meta if not saved_meta.get(k)]
            if not_saved:
                print(
                    "NOTE: these RankMath fields did not round-trip over REST "
                    "(the site hasn't registered them with show_in_rest yet). "
                    "Set them by hand in the editor:"
                )
                for k in not_saved:
                    print(f"  {k}: {meta[k]}")

    print("\n--- verify ---")
    for spec, media in zip(image_specs, uploaded):
        status = "FOUND" if media else "MISSING"
        print(f"  {spec['file']}: {status}")
    remaining = find_forbidden_placeholders(body)
    print(f"placeholders left on the page: {'none' if not remaining else remaining}")
    print(f"result: id={result_id} link={result_link}")


# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Site-agnostic WordPress REST API publisher.")
    parser.add_argument("--check", action="store_true", help="Verify connection and capabilities.")
    parser.add_argument("--manifest", help="Path to a post manifest JSON file.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; write nothing.")
    args = parser.parse_args()

    if args.check:
        check_connection()
    elif args.manifest:
        publish(args.manifest, args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
