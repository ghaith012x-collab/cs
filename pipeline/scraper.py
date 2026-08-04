"""Multi-source data scraper — GitHub Issues, StackOverflow, LeetCode → JSONL.

Usage:
    python -m pipeline.scraper --source all --output data/scraped.jsonl
    python -m pipeline.scraper --source leetcode --max 500
    python -m pipeline.scraper --source github --repo psf/requests
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
HTML_TAG_RE = re.compile(r"<[^>]+>")
CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")


def _clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    import html as _html

    text = HTML_TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    return text.strip()


def _extract_code_blocks(text: str) -> str:
    """Pull out markdown code blocks from raw text."""
    blocks = CODE_BLOCK_RE.findall(text)
    return "\n\n".join(b.strip("`").strip() for b in blocks) if blocks else text


def _save_jsonl(path: str | Path, records: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"  → saved {len(records)} records to {path}")


# ===================================================================
# 1. GitHub Issues Scraper
# ===================================================================
def scrape_github_issues(
    repo: str = "psf/requests",
    token: str | None = None,
    max_issues: int = 100,
    state: str = "closed",
    output: str = "data/github.jsonl",
) -> list[dict]:
    """Scrape closed issues + resolution comments from a GitHub repo."""
    token = token or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    records: list[dict] = []
    page = 1
    per_page = min(max_issues, 100)

    while len(records) < max_issues:
        url = (
            f"https://api.github.com/repos/{repo}/issues"
            f"?state={state}&per_page={per_page}&page={page}"
        )
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"  [!] GitHub API error {resp.status_code}: {resp.text[:200]}")
            break

        issues = resp.json()
        if not issues:
            break

        for issue in issues:
            if "pull_request" in issue:
                continue  # skip PRs

            title = issue.get("title", "")
            body = _clean_html(issue.get("body") or "")
            comments_url = issue.get("comments_url", "")

            # Fetch resolution comment
            resolution = "No resolution."
            if issue.get("comments", 0) > 0:
                c_resp = requests.get(comments_url, headers=headers)
                if c_resp.status_code == 200:
                    comments = c_resp.json()
                    if comments:
                        resolution = _clean_html(comments[-1].get("body", ""))
                time.sleep(0.2)

            records.append({
                "instruction": f"Resolve this GitHub issue from {repo}: {title}",
                "input": body or "No description.",
                "output": resolution,
                "source": "github",
                "repo": repo,
            })

        page += 1
        time.sleep(0.5)  # respect rate limits

    if records:
        _save_jsonl(output, records)
    return records


# ===================================================================
# 2. LeetCode Scraper (GraphQL API)
# ===================================================================
LEETCODE_GRAPHQL = "https://leetcode.com/graphql"

PROBLEM_LIST_QUERY = """
query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int, $filters: QuestionListFilterInput) {
  problemsetQuestionList: questionList(
    categorySlug: $categorySlug
    limit: $limit
    skip: $skip
    filters: $filters
  ) {
    total: totalNum
    questions: data {
      title
      titleSlug
      difficulty
      topicTags { name slug }
      content
      codeSnippets { lang langSlug code }
    }
  }
}
"""


def scrape_leetcode(
    max_problems: int = 300,
    output: str = "data/leetcode.jsonl",
    difficulty_filter: str | None = None,
) -> list[dict]:
    """Fetch LeetCode problems with metadata via the public GraphQL API."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; FreebuffPipeline/1.0)",
    }
    records: list[dict] = []
    skip = 0
    limit = min(max_problems, 100)

    while len(records) < max_problems:
        variables: dict[str, Any] = {
            "categorySlug": "",
            "skip": skip,
            "limit": limit,
            "filters": {},
        }
        if difficulty_filter:
            variables["filters"] = {"difficulty": difficulty_filter.upper()}

        resp = requests.post(
            LEETCODE_GRAPHQL,
            json={"query": PROBLEM_LIST_QUERY, "variables": variables},
            headers=headers,
            timeout=30,
        )

        if resp.status_code != 200:
            print(f"  [!] LeetCode API error {resp.status_code}")
            break

        data = resp.json()
        questions = (
            data.get("data", {})
            .get("problemsetQuestionList", {})
            .get("questions", [])
        )
        if not questions:
            break

        for q in questions:
            title = q.get("title", "")
            difficulty = q.get("difficulty", "Unknown")
            content_raw = q.get("content") or ""
            content = _clean_html(content_raw)
            tags = [t["name"] for t in q.get("topicTags", [])]

            # Extract Python code snippet as starter template
            snippets = q.get("codeSnippets", [])
            py_snippet = next(
                (s["code"] for s in snippets if s.get("langSlug") == "python3"), ""
            )

            records.append({
                "instruction": (
                    f"Solve this LeetCode problem ({difficulty}, tags: {', '.join(tags)}): {title}"
                ),
                "input": f"Problem:\n{content}\n\nStarter code:\n{py_snippet}",
                "output": (
                    f"# Provide an optimal solution with time/space complexity analysis.\n"
                    f"# Tags: {', '.join(tags)}\n"
                    f"# Difficulty: {difficulty}"
                ),
                "source": "leetcode",
                "difficulty": difficulty,
                "tags": tags,
            })

        skip += limit
        time.sleep(0.3)

    if records:
        _save_jsonl(output, records)
    return records


# ===================================================================
# 3. StackOverflow Scraper (Stack Exchange API)
# ===================================================================
def scrape_stackoverflow(
    max_questions: int = 200,
    tagged: str = "python",
    output: str = "data/stackoverflow.jsonl",
    sort: str = "votes",
) -> list[dict]:
    """Fetch top-voted StackOverflow questions + accepted answers."""
    records: list[dict] = []
    page = 1
    pagesize = min(max_questions, 100)

    while len(records) < max_questions:
        url = (
            f"https://api.stackexchange.com/2.3/questions"
            f"?order=desc&sort={sort}&tagged={tagged}"
            f"&site=stackoverflow&filter=withbody"
            f"&pagesize={pagesize}&page={page}"
        )
        resp = requests.get(url, timeout=30)

        if resp.status_code != 200:
            print(f"  [!] StackExchange API error {resp.status_code}")
            break

        items = resp.json().get("items", [])
        if not items:
            break

        for item in items:
            title = item.get("title", "")
            body = _clean_html(item.get("body", ""))
            answer_id = item.get("accepted_answer_id")

            answer_body = "No accepted answer."
            if answer_id:
                ans_url = (
                    f"https://api.stackexchange.com/2.3/answers/{answer_id}"
                    f"?site=stackoverflow&filter=withbody"
                )
                ans_resp = requests.get(ans_url, timeout=30)
                if ans_resp.status_code == 200:
                    ans_items = ans_resp.json().get("items", [])
                    if ans_items:
                        answer_body = _clean_html(ans_items[0].get("body", ""))
                time.sleep(0.15)

            records.append({
                "instruction": f"Answer this programming question: {title}",
                "input": body[:2000] if body else "No question body.",
                "output": answer_body[:3000],
                "source": "stackoverflow",
                "tags": item.get("tags", []),
            })

        page += 1
        time.sleep(0.3)

    if records:
        _save_jsonl(output, records)
    return records


# ===================================================================
# 4. Unified Entry Point
# ===================================================================
SOURCES = {
    "github": scrape_github_issues,
    "leetcode": scrape_leetcode,
    "stackoverflow": scrape_stackoverflow,
}


def scrape_all(output_dir: str = "data", **kwargs: Any) -> dict[str, int]:
    """Run all scrapers and return counts."""
    counts: dict[str, int] = {}
    for name, fn in SOURCES.items():
        print(f"\n[{name}] scraping…")
        out = f"{output_dir}/{name}.jsonl"
        records = fn(output=out, **kwargs.get(name, {}))
        counts[name] = len(records)
    return counts


# ===================================================================
# CLI
# ===================================================================
def main() -> None:
    p = argparse.ArgumentParser(
        description="Scrape training data from GitHub, LeetCode, and StackOverflow"
    )
    p.add_argument(
        "--source",
        choices=["all", "github", "leetcode", "stackoverflow"],
        default="all",
    )
    p.add_argument("--output-dir", default="data", help="Output directory for JSONL files")
    p.add_argument("--max", type=int, default=300, help="Max items per source")
    p.add_argument("--repo", default="psf/requests", help="GitHub repo (owner/name)")
    p.add_argument("--tag", default="python", help="StackOverflow tag")
    p.add_argument("--github-token", default=None, help="GitHub PAT (or set GITHUB_TOKEN)")

    args = p.parse_args()

    if args.source in ("all", "github"):
        scrape_github_issues(
            repo=args.repo,
            token=args.github_token,
            max_issues=args.max,
            output=f"{args.output_dir}/github.jsonl",
        )

    if args.source in ("all", "leetcode"):
        scrape_leetcode(
            max_problems=args.max,
            output=f"{args.output_dir}/leetcode.jsonl",
        )

    if args.source in ("all", "stackoverflow"):
        scrape_stackoverflow(
            max_questions=args.max,
            tagged=args.tag,
            output=f"{args.output_dir}/stackoverflow.jsonl",
        )

    print(f"\n✓ Scraping complete. Data saved to {args.output_dir}/")


if __name__ == "__main__":
    main()
