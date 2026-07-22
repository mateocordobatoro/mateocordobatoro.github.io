from __future__ import annotations

import base64
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml


# ============================================================
# Configuration
# ============================================================

GITHUB_USERNAME = "mateocordobatoro"
PORTFOLIO_TOPIC = "portfolio-project"
PROJECT_METADATA_PATH = "portfolio/project.yml"

ROOT_DIRECTORY = Path(__file__).resolve().parent.parent

PROJECTS_OUTPUT_FILE = (
    ROOT_DIRECTORY
    / "generated"
    / "_projects.qmd"
)

BLOG_POSTS_DIRECTORY = (
    ROOT_DIRECTORY
    / "blog"
    / "posts"
)

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


# ============================================================
# GitHub API
# ============================================================

def github_request(url: str) -> Any:
    """
    Send a GET request to the GitHub API.
    """

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{GITHUB_USERNAME}-portfolio-sync",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    request = urllib.request.Request(
        url=url,
        headers=headers,
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None

        error_body = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"GitHub API returned HTTP {error.code} "
            f"for {url}\n{error_body}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Could not connect to GitHub: "
            f"{error.reason}"
        ) from error


def list_public_repositories() -> list[dict[str, Any]]:
    """
    Get all public repositories owned by the user.
    """

    repositories: list[dict[str, Any]] = []
    page = 1

    while True:
        query = urllib.parse.urlencode(
            {
                "type": "owner",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            }
        )

        url = (
            f"{GITHUB_API}/users/"
            f"{GITHUB_USERNAME}/repos?{query}"
        )

        page_repositories = github_request(url)

        if not page_repositories:
            break

        repositories.extend(page_repositories)

        if len(page_repositories) < 100:
            break

        page += 1

    return repositories


def read_repository_file(
    repository_name: str,
    file_path: str,
) -> str | None:
    """
    Read one text file from a repository.
    """

    encoded_path = urllib.parse.quote(
        file_path,
        safe="/",
    )

    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_USERNAME}/"
        f"{repository_name}/contents/"
        f"{encoded_path}"
    )

    response = github_request(url)

    if response is None:
        return None

    if response.get("type") != "file":
        return None

    encoded_content = response.get("content")

    if not encoded_content:
        return None

    return base64.b64decode(
        encoded_content
    ).decode("utf-8")


# ============================================================
# Project metadata
# ============================================================

def normalize_project(
    repository: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert repository metadata into a standard structure.
    """

    repository_name = repository["name"]

    title = str(
        metadata.get("title")
        or repository_name.replace("-", " ").title()
    ).strip()

    description = str(
        metadata.get("description")
        or repository.get("description")
        or "Project description coming soon."
    ).strip()

    slug = str(
        metadata.get("slug")
        or repository_name
    ).strip()

    technologies = metadata.get(
        "technologies",
        [],
    )

    if isinstance(technologies, str):
        technologies = [technologies]

    if not isinstance(technologies, list):
        technologies = []

    order = metadata.get("order", 999)

    try:
        order = int(order)
    except (TypeError, ValueError):
        order = 999

    blog_file = str(
        metadata.get("blog_file")
        or ""
    ).strip()

    return {
        "repository_name": repository_name,
        "title": title,
        "slug": slug,
        "description": description,
        "repository_url": str(
            metadata.get("repository_url")
            or repository["html_url"]
        ).strip(),
        "demo_url": str(
            metadata.get("demo_url")
            or ""
        ).strip(),
        "presentation_url": str(
            metadata.get("presentation_url")
            or ""
        ).strip(),
        "blog_file": blog_file,
        "blog_url": str(
            metadata.get("blog_url")
            or f"blog/posts/{slug}/"
        ).strip(),
        "technologies": [
            str(technology).strip()
            for technology in technologies
            if str(technology).strip()
        ],
        "status": str(
            metadata.get("status")
            or "published"
        ).strip().lower(),
        "order": order,
        "blog_synced": False,
    }


def load_portfolio_projects() -> list[dict[str, Any]]:
    """
    Find repositories marked with portfolio-project.
    """

    repositories = list_public_repositories()
    projects: list[dict[str, Any]] = []

    for repository in repositories:
        repository_name = repository["name"]
        topics = repository.get("topics") or []

        if repository.get("fork"):
            continue

        if repository.get("archived"):
            continue

        if (
            repository_name
            == f"{GITHUB_USERNAME}.github.io"
        ):
            continue

        if PORTFOLIO_TOPIC not in topics:
            continue

        print(
            f"Reading project metadata: "
            f"{repository_name}"
        )

        raw_metadata = read_repository_file(
            repository_name,
            PROJECT_METADATA_PATH,
        )

        if raw_metadata is None:
            print(
                f"  Skipped: "
                f"{PROJECT_METADATA_PATH} not found."
            )
            continue

        try:
            metadata = (
                yaml.safe_load(raw_metadata)
                or {}
            )

        except yaml.YAMLError as error:
            print(
                f"  Skipped: invalid YAML: "
                f"{error}"
            )
            continue

        if not isinstance(metadata, dict):
            print(
                "  Skipped: project.yml must "
                "contain a YAML object."
            )
            continue

        project = normalize_project(
            repository,
            metadata,
        )

        if project["status"] != "published":
            print(
                f"  Skipped: status is "
                f"'{project['status']}'."
            )
            continue

        projects.append(project)

    projects.sort(
        key=lambda project: (
            project["order"],
            project["title"].lower(),
        )
    )

    return projects


# ============================================================
# Blog synchronization
# ============================================================

def synchronize_project_blog(
    project: dict[str, Any],
) -> bool:
    """
    Copy portfolio/index.qmd from the project repository
    into the central portfolio blog.
    """

    blog_file = project["blog_file"]

    if not blog_file:
        print(
            f"  Blog skipped for "
            f"{project['repository_name']}: "
            f"blog_file is empty."
        )
        return False

    raw_blog = read_repository_file(
        project["repository_name"],
        blog_file,
    )

    if raw_blog is None:
        print(
            f"  Blog skipped for "
            f"{project['repository_name']}: "
            f"{blog_file} was not found."
        )
        return False

    destination_directory = (
        BLOG_POSTS_DIRECTORY
        / project["slug"]
    )

    destination_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    destination_file = (
        destination_directory
        / "index.qmd"
    )

    destination_file.write_text(
        raw_blog,
        encoding="utf-8",
    )

    print(
        f"  Blog synchronized: "
        f"{destination_file.relative_to(ROOT_DIRECTORY)}"
    )

    return True


def synchronize_blogs(
    projects: list[dict[str, Any]],
) -> int:
    """
    Synchronize the blog file for every project.
    """

    synchronized_count = 0

    for project in projects:
        project["blog_synced"] = (
            synchronize_project_blog(project)
        )

        if project["blog_synced"]:
            synchronized_count += 1

    return synchronized_count


# ============================================================
# Projects page generation
# ============================================================

def make_link(
    label: str,
    url: str,
) -> str:
    """
    Generate a Quarto Markdown link.
    """

    safe_label = html.escape(label)
    safe_url = url.replace(")", "%29")

    return (
        f"[{safe_label}]({safe_url})"
        "{.project-link}"
    )


def render_project(
    project: dict[str, Any],
) -> str:
    """
    Convert one project into Quarto Markdown.
    """

    title = html.escape(project["title"])
    description = html.escape(
        project["description"]
    )

    lines = [
        "::: {.project-card}",
        "",
        f"## {title}",
        "",
        description,
        "",
    ]

    technologies = project["technologies"]

    if technologies:
        technology_text = " · ".join(
            html.escape(technology)
            for technology in technologies
        )

        lines.extend(
            [
                (
                    '<p class="project-technologies">'
                    f"{technology_text}</p>"
                ),
                "",
            ]
        )

    links: list[str] = []

    if project["demo_url"]:
        links.append(
            make_link(
                "Open project demo",
                project["demo_url"],
            )
        )

    if project["presentation_url"]:
        links.append(
            make_link(
                "View presentation",
                project["presentation_url"],
            )
        )

    links.append(
        make_link(
            "View repository",
            project["repository_url"],
        )
    )

    if project["blog_synced"]:
        links.append(
            make_link(
                "Read project blog",
                project["blog_url"],
            )
        )

    lines.extend(
        [
            '<div class="project-actions">',
            "",
            " ".join(links),
            "",
            "</div>",
            "",
            ":::",
        ]
    )

    return "\n".join(lines)


def write_projects_file(
    projects: list[dict[str, Any]],
) -> None:
    """
    Generate generated/_projects.qmd.
    """

    PROJECTS_OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    header = [
        "<!--",
        "This file is generated automatically.",
        "Do not edit it manually.",
        "Run: python scripts/sync_projects.py",
        "-->",
        "",
    ]

    if projects:
        body = "\n\n".join(
            render_project(project)
            for project in projects
        )
    else:
        body = (
            '::: {.no-projects}\n\n'
            "No portfolio projects have been "
            "published yet.\n\n"
            ":::"
        )

    PROJECTS_OUTPUT_FILE.write_text(
        "\n".join(header)
        + body
        + "\n",
        encoding="utf-8",
    )


# ============================================================
# Main
# ============================================================

def main() -> int:
    try:
        projects = load_portfolio_projects()

        synchronized_blogs = synchronize_blogs(
            projects
        )

        write_projects_file(projects)

    except Exception as error:
        print(
            f"Portfolio synchronization failed: "
            f"{error}"
        )
        return 1

    print()
    print(f"Projects found: {len(projects)}")
    print(
        f"Blogs synchronized: "
        f"{synchronized_blogs}"
    )
    print(
        f"Generated projects file: "
        f"{PROJECTS_OUTPUT_FILE}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())