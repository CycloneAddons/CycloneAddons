#!/usr/bin/env python3
"""Auto-fetch GitHub pinned, top starred/forked repositories, and profile metrics.

Updates the <!-- PROJECTS:START --> ... <!-- PROJECTS:END --> and optional
<!-- STATS:START --> ... <!-- STATS:END --> sections in the profile README.
Uses only the Python standard library so it runs cleanly with zero setup.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from typing import Any

USER_AGENT = "CycloneAddons-Profile-Updater/1.0"

GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    followers {
      totalCount
    }
    repositories(privacy: PUBLIC, ownerAffiliations: OWNER) {
      totalCount
    }
    pinnedItems(first: 6, types: REPOSITORY) {
      nodes {
        ... on Repository {
          name
          nameWithOwner
          description
          url
          homepageUrl
          stargazerCount
          forkCount
          isFork
          isArchived
          isPrivate
          pushedAt
          primaryLanguage {
            name
          }
        }
      }
    }
  }
}
""".strip()


def http_request(
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    accept: str = "application/vnd.github+json",
) -> tuple[int, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def fetch_json(
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    _, body = http_request(url, token=token, payload=payload)
    return json.loads(body)


def normalize_rest_repo(repo: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": repo.get("name") or "",
        "full_name": repo.get("full_name") or "",
        "description": repo.get("description") or "",
        "url": repo.get("html_url") or "",
        "homepage": (repo.get("homepage") or "").strip(),
        "stars": int(repo.get("stargazers_count") or 0),
        "forks": int(repo.get("forks_count") or 0),
        "language": repo.get("language") or "",
        "is_fork": bool(repo.get("fork")),
        "is_archived": bool(repo.get("archived")),
        "is_private": bool(repo.get("private")),
        "pushed_at": repo.get("pushed_at") or "",
    }


def normalize_graphql_repo(node: dict[str, Any]) -> dict[str, Any]:
    lang = node.get("primaryLanguage") or {}
    return {
        "name": node.get("name") or "",
        "full_name": node.get("nameWithOwner") or "",
        "description": node.get("description") or "",
        "url": node.get("url") or "",
        "homepage": (node.get("homepageUrl") or "").strip(),
        "stars": int(node.get("stargazerCount") or 0),
        "forks": int(node.get("forkCount") or 0),
        "language": lang.get("name") or "",
        "is_fork": bool(node.get("isFork")),
        "is_archived": bool(node.get("isArchived")),
        "is_private": bool(node.get("isPrivate")),
        "pushed_at": node.get("pushedAt") or "",
    }


def fetch_user_profile(username: str, token: str | None) -> dict[str, int]:
    data = fetch_json(f"https://api.github.com/users/{username}", token=token)
    return {
        "followers": int(data.get("followers") or 0),
        "public_repos": int(data.get("public_repos") or 0),
    }


def fetch_all_owned_repos(username: str, token: str | None) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/users/{username}/repos"
            f"?per_page=100&type=owner&sort=updated&page={page}"
        )
        batch = fetch_json(url, token=token)
        if not batch:
            break
        repos.extend(normalize_rest_repo(item) for item in batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_pinned_via_graphql(
    username: str, token: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    res = fetch_json(
        "https://api.github.com/graphql",
        token=token,
        payload={"query": GRAPHQL_QUERY, "variables": {"login": username}},
    )
    if res.get("errors"):
        raise RuntimeError(f"GraphQL errors: {res['errors']}")

    user = (res.get("data") or {}).get("user")
    if not user:
        raise RuntimeError(f"User '{username}' not found via GraphQL")

    nodes = (user.get("pinnedItems") or {}).get("nodes") or []
    pinned = [normalize_graphql_repo(n) for n in nodes if n and not n.get("isPrivate")]
    profile = {
        "followers": int((user.get("followers") or {}).get("totalCount") or 0),
        "public_repos": int((user.get("repositories") or {}).get("totalCount") or 0),
    }
    return pinned, profile


def fetch_pinned_via_html(
    username: str,
    repo_map: dict[str, dict[str, Any]],
    token: str | None,
) -> list[dict[str, Any]]:
    _, page_html = http_request(
        f"https://github.com/{username}",
        accept="text/html,application/xhtml+xml",
    )
    pinned: list[dict[str, Any]] = []
    seen: set[str] = set()

    for block in re.findall(r"<li[^>]*pinned-item-list-item.*?</li>", page_html, re.S):
        href_match = re.search(r'<a[^>]*href="/([^/"?#]+/[^/"?#]+)"', block)
        if not href_match:
            continue
        full_name = html.unescape(href_match.group(1)).strip()
        key = full_name.lower()
        if key in seen:
            continue
        seen.add(key)

        if key in repo_map:
            pinned.append(repo_map[key])
            continue

        try:
            raw = fetch_json(f"https://api.github.com/repos/{full_name}", token=token)
            pinned.append(normalize_rest_repo(raw))
        except Exception:
            desc_match = re.search(r"pinned-item-desc[^>]*>(.*?)</p>", block, re.S)
            lang_match = re.search(
                r'itemprop="programmingLanguage"[^>]*>(.*?)<', block, re.S
            )
            pinned.append(
                {
                    "name": full_name.split("/")[-1],
                    "full_name": full_name,
                    "description": html.unescape(desc_match.group(1)).strip()
                    if desc_match
                    else "",
                    "url": f"https://github.com/{full_name}",
                    "homepage": "",
                    "stars": 0,
                    "forks": 0,
                    "language": html.unescape(lang_match.group(1)).strip()
                    if lang_match
                    else "",
                    "is_fork": False,
                    "is_archived": False,
                    "is_private": False,
                    "pushed_at": "",
                }
            )
    return pinned


def clean_description(text: str, max_len: int = 115) -> str:
    cleaned = html.unescape(text or "")
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = cleaned.replace("|", "\\|")
    # Strip markdown bold markers inside table descriptions to keep row typography uniform
    cleaned = cleaned.replace("**", "")
    if not cleaned:
        return "_No description provided._"
    if len(cleaned) <= max_len:
        return cleaned
    truncated = cleaned[: max_len - 1].rsplit(" ", 1)[0].rstrip(".,;:- ")
    return f"{truncated}…"


def format_date(iso_ts: str) -> str:
    if not iso_ts:
        return "—"
    try:
        dt = datetime.strptime(iso_ts[:10], "%Y-%m-%d")
        return dt.strftime("%b %Y")
    except ValueError:
        return "—"


def format_language(lang: str) -> str:
    return f"`{lang}`" if lang else "—"


def format_repo_link(repo: dict[str, Any]) -> str:
    name = repo["name"].replace("|", "\\|")
    link = f"[**{name}**]({repo['url']})"
    homepage = (repo.get("homepage") or "").strip()
    if homepage.startswith(("http://", "https://")):
        link += f" · [↗ Live]({homepage})"
    return link


def build_pinned_table(pinned_repos: list[dict[str, Any]], max_pinned: int) -> str:
    items = [r for r in pinned_repos if not r.get("is_private")][:max_pinned]
    lines = [
        "### 📌 Pinned Projects",
        "",
        "| Project | Description | Language | ⭐ Stars | 🍴 Forks | Last Push |",
        "|:---|:---|:---:|:---:|:---:|:---:|",
    ]
    if not items:
        lines.append("| _No pinned repositories found_ | — | — | — | — | — |")
        return "\n".join(lines)

    for repo in items:
        lines.append(
            f"| {format_repo_link(repo)} "
            f"| {clean_description(repo['description'])} "
            f"| {format_language(repo['language'])} "
            f"| {repo['stars']} "
            f"| {repo['forks']} "
            f"| {format_date(repo['pushed_at'])} |"
        )
    return "\n".join(lines)


def build_top_table(
    all_repos: list[dict[str, Any]],
    username: str,
    max_top: int,
    include_forks: bool,
) -> str:
    profile_repo = f"{username}/{username}".lower()
    candidates = [
        r
        for r in all_repos
        if not r.get("is_private")
        and r.get("full_name", "").lower() != profile_repo
        and (include_forks or not r.get("is_fork"))
    ]

    ranked = sorted(
        candidates,
        key=lambda r: (
            r["stars"] + r["forks"],
            r["stars"],
            r["forks"],
            r["pushed_at"],
        ),
        reverse=True,
    )

    engaged = [r for r in ranked if r["stars"] > 0 or r["forks"] > 0]
    items = (engaged if engaged else ranked)[:max_top]

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [
        "### 🔥 Top Starred & Forked Repositories",
        "",
        "| Rank | Repository | Description | Language | ⭐ Stars | 🍴 Forks | Last Push |",
        "|:---:|:---|:---|:---:|:---:|:---:|:---:|",
    ]
    if not items:
        lines.append("| — | _No repositories found_ | — | — | — | — | — |")
        return "\n".join(lines)

    for idx, repo in enumerate(items, start=1):
        rank_badge = medals.get(idx, f"`#{idx}`")
        lines.append(
            f"| {rank_badge} "
            f"| {format_repo_link(repo)} "
            f"| {clean_description(repo['description'])} "
            f"| {format_language(repo['language'])} "
            f"| **{repo['stars']}** "
            f"| **{repo['forks']}** "
            f"| {format_date(repo['pushed_at'])} |"
        )
    return "\n".join(lines)


def build_stats_table(
    profile: dict[str, int],
    all_repos: list[dict[str, Any]],
) -> str:
    public_repos = profile.get("public_repos") or len(all_repos)
    original_repos = [r for r in all_repos if not r.get("is_fork")]
    total_stars = sum(r["stars"] for r in all_repos)
    total_forks = sum(r["forks"] for r in all_repos)
    followers = profile.get("followers", 0)

    lang_counts: Counter[str] = Counter()
    lang_engagement: Counter[str] = Counter()
    for repo in original_repos:
        lang = repo.get("language")
        if lang:
            lang_counts[lang] += 1
            lang_engagement[lang] += repo["stars"] + repo["forks"]

    ranked_langs = sorted(
        lang_counts.keys(),
        key=lambda lang: (lang_counts[lang], lang_engagement[lang], lang),
        reverse=True,
    )
    top_langs = " · ".join(f"`{lang}`" for lang in ranked_langs[:5]) or "—"

    return "\n".join(
        [
            "| 📦 Public Repos | 🛠️ Original Repos | ⭐ Total Stars | 🍴 Total Forks | 👥 Followers | 🏆 Top Languages |",
            "|:---:|:---:|:---:|:---:|:---:|:---|",
            f"| **{public_repos}** | **{len(original_repos)}** | **{total_stars}** | **{total_forks}** | **{followers}** | {top_langs} |",
        ]
    )


def replace_marker_block(
    content: str,
    start_marker: str,
    end_marker: str,
    new_block: str,
) -> tuple[str, bool]:
    pattern = re.compile(
        rf"({re.escape(start_marker)}).*?({re.escape(end_marker)})",
        re.DOTALL,
    )
    if not pattern.search(content):
        return content, False
    updated = pattern.sub(rf"\1\n{new_block}\n\2", content, count=1)
    return updated, True


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update README project & stats tables from GitHub API."
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("GITHUB_USERNAME") or "CycloneAddons",
        help="GitHub username to fetch repositories for.",
    )
    parser.add_argument(
        "--file",
        default=os.environ.get("README_PATH") or "README.md",
        help="Target Markdown file containing PROJECTS markers.",
    )
    parser.add_argument(
        "--max-pinned",
        type=int,
        default=int(os.environ.get("MAX_PINNED") or 6),
        help="Maximum pinned repositories to display.",
    )
    parser.add_argument(
        "--max-top",
        type=int,
        default=int(os.environ.get("MAX_TOP_REPOS") or 6),
        help="Maximum top starred/forked repositories to display.",
    )
    parser.add_argument(
        "--include-forks",
        action="store_true",
        default=parse_bool(os.environ.get("INCLUDE_FORKS"), default=False),
        help="Include forked repositories in the top starred/forked table.",
    )
    args = parser.parse_args()

    token = (os.environ.get("GITHUB_TOKEN") or "").strip() or None
    if not os.path.exists(args.file):
        print(f"Error: target file '{args.file}' does not exist.", file=sys.stderr)
        return 1

    all_repos = fetch_all_owned_repos(args.username, token)
    repo_map = {r["full_name"].lower(): r for r in all_repos}

    pinned_repos: list[dict[str, Any]] = []
    profile: dict[str, int] | None = None

    if token:
        try:
            pinned_repos, profile = fetch_pinned_via_graphql(args.username, token)
        except Exception as exc:
            print(
                f"Warning: GraphQL fetch failed ({exc}); falling back to HTML + REST.",
                file=sys.stderr,
            )

    if not pinned_repos:
        pinned_repos = fetch_pinned_via_html(args.username, repo_map, token)

    if profile is None:
        profile = fetch_user_profile(args.username, token)

    projects_md = "\n\n".join(
        [
            build_pinned_table(pinned_repos, args.max_pinned),
            build_top_table(all_repos, args.username, args.max_top, args.include_forks),
        ]
    )
    stats_md = build_stats_table(profile, all_repos)

    with open(args.file, "r", encoding="utf-8") as fh:
        original_content = fh.read()

    updated_content, found_projects = replace_marker_block(
        original_content,
        "<!-- PROJECTS:START -->",
        "<!-- PROJECTS:END -->",
        projects_md,
    )
    if not found_projects:
        print(
            f"Error: <!-- PROJECTS:START --> / <!-- PROJECTS:END --> markers not found in '{args.file}'.",
            file=sys.stderr,
        )
        return 1

    updated_content, found_stats = replace_marker_block(
        updated_content,
        "<!-- STATS:START -->",
        "<!-- STATS:END -->",
        stats_md,
    )

    if updated_content != original_content:
        with open(args.file, "w", encoding="utf-8") as fh:
            fh.write(updated_content)
        print(
            f"Updated {args.file}: {len(pinned_repos)} pinned repo(s), "
            f"{len(all_repos)} owned repo(s)"
            + (", stats refreshed." if found_stats else ".")
        )
    else:
        print(f"No changes needed in {args.file}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
