"""
Command-line interface for the Culprit Finder tool.

This module acts as the entry point for the application, handling argument parsing, input validation,
and authentication checks. It initializes the `CulpritFinder` with user-provided parameters
(repository, commit range, workflow) and reports the identified culprit commit or the lack thereof.
"""

import argparse
import logging
import sys
import re

from github.Commit import Commit

from culprit_finder import culprit_finder
from culprit_finder import culprit_finder_state
from culprit_finder import github_client

logging.basicConfig(
  level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def _validate_repo(repo: str) -> str:
  """
  Validates that the repository string is in the format 'owner/repo'.

  Args:
      repo: The repository string to validate.

  Returns:
      The validated repository string.

  Raises:
      argparse.ArgumentTypeError: If the repository string is in an invalid format.
  """
  parts = repo.split("/")
  if len(parts) != 2 or not all(parts):
    raise argparse.ArgumentTypeError(f"Invalid repo format: {repo}")

  return repo


def _get_repo_from_url(url: str) -> str:
  """
  Extracts the repository name from a GitHub URL.

  Args:
      url: The GitHub URL.

  Returns:
      The repository name in 'owner/repo' format.

  Raises:
      ValueError: If the repository name cannot be extracted from the URL.
  """
  match = re.search(r"github\.com/([^/]+/[^/]+)", url)
  if not match:
    raise ValueError(f"Could not extract repo from URL: {url}")
  return match.group(1)


def _parse_args(
  parser: argparse.ArgumentParser, args: list[str] | None = None
) -> argparse.Namespace:
  """
  Parses command-line arguments.

  Args:
      parser: The ArgumentParser instance.
      args: Optional list of strings to parse. If None, sys.argv[1:] is used.

  Returns:
      The parsed Namespace object.
  """
  parser.add_argument("url", nargs="?", help="GitHub Actions Run URL")
  parser.add_argument(
    "-r",
    "--repo",
    help="Target GitHub repository (e.g., owner/repo)",
    type=_validate_repo,
  )
  parser.add_argument("-s", "--start", help="Last known good commit SHA")
  parser.add_argument("-e", "--end", help="First known bad commit SHA")
  parser.add_argument(
    "-w",
    "--workflow",
    help="Workflow filename (e.g., build_and_test.yml)",
  )
  parser.add_argument(
    "-j",
    "--job",
    required=False,
    help="The specific job name within the workflow to monitor for pass/fail",
  )
  parser.add_argument(
    "--clear-cache",
    action="store_true",
    help="Deletes the local state file before execution",
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

  parsed_args = parser.parse_args(args)

  if bool(parsed_args.cross_repo_dep) != bool(parsed_args.dep_pin_file):
    parser.error("--cross-repo-dep and --dep-pin-file must be used together.")

  return parsed_args


def _extract_from_url(
  url: str,
  gh_client: github_client.GithubClient,
  start: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
  """
  Extracts repository, start SHA, end SHA, and workflow filename from a URL.

  Args:
      url: The GitHub Actions Run URL.
      gh_client: The GithubClient instance.
      start: The provided start SHA, if any.

  Returns:
      A tuple containing (start_sha, end_sha, workflow_file_name, job_name).

  Raises:
      ValueError: If the URL does not point to a failed workflow run.
  """
  run, job_details = gh_client.get_run_and_job_from_url(url)
  if run.conclusion != "failure":
    raise ValueError("The provided URL does not point to a failed workflow run.")

  if not start:
    if job_details:
      previous_run = gh_client.find_previous_successful_job_run(run, job_details.name)
    else:
      previous_run = gh_client.find_previous_successful_run(run)
    start = previous_run.head_sha
  end = run.head_sha

  workflow_details = gh_client.get_workflow(run.workflow_id)
  workflow_file_name = workflow_details.path.split("/")[-1]
  job_name = job_details.name if job_details else None

  return start, end, workflow_file_name, job_name


def _initialize_state(
  state_persister: culprit_finder_state.StatePersister,
  repo: str,
  workflow_file_name: str,
  start: str,
  end: str,
  job_name: str | None = None,
) -> culprit_finder_state.CulpritFinderState:
  """
  Initializes or resumes the bisection state.

  Args:
      state_persister: The StatePersister instance.
      repo: The repository name.
      workflow_file_name: The workflow filename.
      start: The start commit SHA.
      end: The end commit SHA.
      job_name: The job name, if applicable.

  Returns:
      The initialized or loaded CulpritFinderState dictionary.
  """
  state: culprit_finder_state.CulpritFinderState = {
    "repo": repo,
    "workflow": workflow_file_name,
    "original_start": start,
    "original_end": end,
    "current_good": "",
    "current_bad": "",
    "job": job_name,
    "cache": {},
  }

  if state_persister.exists():
    print("\nA previous bisection state was found.")
    resume = input("Do you want to resume from the saved state? (y/n): ").lower()
    if resume not in ["y", "yes"]:
      print("Starting a new bisection. Deleting the old state...")
      state_persister.delete()
    else:
      state = state_persister.load()
      print("Resuming from the saved state.")
  return state


def _print_culprit_finder_results(
  culprit_commit: Commit | None, culprit_repo: str, repo: str
) -> None:
  """
  Prints the results of the culprit finder.

  Args:
      culprit_commit: The identified culprit commit, or None if not found.
      culprit_repo: The repository where the culprit was found.
      repo: The main repository being tested.
  """
  if culprit_commit:
    commit_message = culprit_commit.commit.message.splitlines()[0]
    if culprit_repo != repo:
      print(f"\nCulprit commit found in cross-repo dependency: {culprit_repo}")
    print(
      f"\nThe culprit commit is: {commit_message} (SHA: {culprit_commit.sha})",
    )
  else:
    print("No culprit commit found.")


def main() -> None:
  """
  Entry point for the culprit finder CLI.

  Parses command-line arguments then initiates the bisection process using CulpritFinder.
  """
  parser = argparse.ArgumentParser(
    prog="culprit-finder", description="Culprit finder for GitHub Actions."
  )
  args = _parse_args(parser)

  repo: str | None = args.repo
  start: str | None = args.start
  end: str | None = args.end
  workflow_file_name: str | None = args.workflow
  job_name: str | None = args.job

  if args.url:
    repo = _get_repo_from_url(args.url)

  if not repo:
    parser.error(
      "the following arguments are required: -r/--repo (or provided via URL)"
    )

  token = github_client.get_github_token()
  if token is None:
    logging.error("Not authenticated with GitHub CLI or GH_TOKEN env var is not set.")
    sys.exit(1)

  gh_client = github_client.GithubClient(repo=repo, token=token)

  if args.url:
    start, end, workflow_file_name, job_name = _extract_from_url(
      args.url, gh_client, start
    )

  if not start:
    parser.error("the following arguments are required: -s/--start")
  if not end:
    parser.error("the following arguments are required: -e/--end")
  if not workflow_file_name:
    parser.error("the following arguments are required: -w/--workflow")

  logging.info("Initializing culprit finder for %s", repo)
  logging.info("Start commit: %s", start)
  logging.info("End commit: %s", end)
  logging.info("Workflow: %s", workflow_file_name)
  logging.info("Job: %s", job_name)

  state_persister = culprit_finder_state.StatePersister(
    repo=repo, workflow=workflow_file_name, job=job_name
  )

  if args.clear_cache and state_persister.exists():
    state_persister.delete()

  state = _initialize_state(state_persister, repo, workflow_file_name, start, end)

  has_culprit_finder_workflow = any(
    wf.path == ".github/workflows/culprit_finder.yml"
    for wf in gh_client.get_workflows()
  )

  logging.info("Using culprit finder workflow: %s", has_culprit_finder_workflow)

  cross_repo_gh_client: github_client.GithubClient | None = None
  if args.cross_repo_dep:
    cross_repo_gh_client = github_client.GithubClient(
      repo=args.cross_repo_dep, token=token
    )

  finder = culprit_finder.CulpritFinder(
    repo=repo,
    start_sha=start,
    end_sha=end,
    workflow_file=workflow_file_name,
    has_culprit_finder_workflow=has_culprit_finder_workflow,
    gh_client=gh_client,
    state=state,
    state_persister=state_persister,
    job=job_name,
    cross_repo_gh_client=cross_repo_gh_client,
    dep_pin_file=args.dep_pin_file,
  )

  try:
    culprit_commit, culprit_repo = finder.run_bisection()
    print(culprit_repo, repo)
    _print_culprit_finder_results(culprit_commit, culprit_repo, repo)

    state_persister.delete()
  except KeyboardInterrupt:
    logging.info("Bisection interrupted by user (CTRL+C). Saving current state...")
    state_persister.save(state)
    logging.info("State saved.")


if __name__ == "__main__":
  main()
