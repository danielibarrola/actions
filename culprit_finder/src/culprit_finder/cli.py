"""
Command-line interface for the Culprit Finder tool.

This module acts as the entry point for the application, handling argument parsing, input validation,
and authentication checks. It initializes the `CulpritFinder` with user-provided parameters
(repository, commit range, workflow) and reports the identified culprit commit or the lack thereof.
"""

import argparse
import logging
import os
import sys

from culprit_finder import culprit_finder
from culprit_finder import github

logging.basicConfig(
  level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def _validate_repo(repo: str) -> str:
  parts = repo.split("/")
  if len(parts) != 2 or not all(parts):
    raise argparse.ArgumentTypeError(f"Invalid repo format: {repo}")

  return repo


def main() -> None:
  """
  Entry point for the culprit finder CLI.

  Parses command-line arguments then initiates the bisection process using CulpritFinder.
  """
  parser = argparse.ArgumentParser(description="Culprit finder for GitHub Actions.")
  parser.add_argument(
    "-r",
    "--repo",
    required=True,
    help="Target GitHub repository (e.g., owner/repo)",
    type=_validate_repo,
  )
  parser.add_argument("-s", "--start", required=True, help="Last known good commit SHA")
  parser.add_argument("-e", "--end", required=True, help="First known bad commit SHA")
  parser.add_argument(
    "-w",
    "--workflow",
    required=True,
    help="Workflow filename (e.g., build_and_test.yml)",
  )
  parser.add_argument(
    "--cross-repo-dep",
    required=False,
    help="Cross-repository dependency (e.g., owner/repo2)",
    type=_validate_repo,
  )
  parser.add_argument(
    "--dep-pin-file",
    required=False,
    help="Path to the file in the primary repo that pins the dependency commit SHA (e.g., revision.bzl)",
  )

  args = parser.parse_args()

  if bool(args.cross_repo_dep) != bool(args.dep_pin_file):
    parser.error("--cross-repo-dep and --dep-pin-file must be used together.")

  gh_client = github.GithubClient(repo=args.repo)

  is_authenticated_with_cli = gh_client.check_auth_status()
  has_access_token = os.environ.get("GH_TOKEN") is not None

  if not is_authenticated_with_cli and not has_access_token:
    logging.error("Not authenticated with GitHub CLI or GH_TOKEN env var is not set.")
    sys.exit(1)

  logging.info("Initializing culprit finder for %s", args.repo)
  logging.info("Start commit: %s", args.start)
  logging.info("End commit: %s", args.end)
  logging.info("Workflow: %s", args.workflow)

  has_culprit_finder_workflow = any(
    wf["path"] == ".github/workflows/culprit_finder.yml"
    for wf in gh_client.get_workflows()
  )

  logging.info("Using culprit finder workflow: %s", has_culprit_finder_workflow)

  finder = culprit_finder.CulpritFinder(
    repo=args.repo,
    start_sha=args.start,
    end_sha=args.end,
    workflow_file=args.workflow,
    has_culprit_finder_workflow=has_culprit_finder_workflow,
    github_client=gh_client,
    cross_repo_dep=args.cross_repo_dep,
    dep_pin_file=args.dep_pin_file,
  )
  culprit_commit, repo = finder.run_bisection()
  if culprit_commit:
    commit_message = culprit_commit["message"].splitlines()[0]
    if repo != args.repo:
      print(f"\nCulprit commit found in cross-repo dependency: {repo}")
    print(
      f"\nThe culprit commit is: {commit_message} (SHA: {culprit_commit['sha']})",
    )
  else:
    print("No culprit commit found.")


if __name__ == "__main__":
  main()
