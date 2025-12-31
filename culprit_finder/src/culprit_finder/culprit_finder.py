"""
Core logic for detecting regression commits using binary search.

This module defines the `CulpritFinder` class, which orchestrates the bisection process.
"""

import time
import logging
import uuid
from culprit_finder import github


CULPRIT_FINDER_WORKFLOW_NAME = "culprit_finder.yml"


class CulpritFinder:
  """Culprit finder class to find the culprit commit for a GitHub workflow."""

  def __init__(
    self,
    repo: str,
    start_sha: str,
    end_sha: str,
    workflow_file: str,
    has_culprit_finder_workflow: bool,
    github_client: github.GithubClient,
    cross_repo_dep: str | None = None,
    dep_pin_file: str | None = None,
  ):
    """
    Initializes the CulpritFinder instance.

    Args:
        repo: The GitHub repository in 'owner/repo' format.
        start_sha: The SHA of the last known good commit.
        end_sha: The SHA of the first known bad commit.
        workflow_file: The name of the workflow file to test (e.g., 'build.yml').
        has_culprit_finder_workflow: Whether the repo being tested has a Culprit Finder workflow.
        github_client: The GitHub client instance.
        cross_repo_dep: Optional cross-repo dependency (owner/repo).
        dep_pin_file: Optional file path containing the dependency pin.
    """
    self._repo = repo
    self._start_sha = start_sha
    self._end_sha = end_sha
    self._culprit_finder_workflow_file = CULPRIT_FINDER_WORKFLOW_NAME
    self._workflow_file = workflow_file
    self._has_culprit_finder_workflow = has_culprit_finder_workflow
    self._gh_client = github_client
    self._cross_repo_dep = cross_repo_dep
    self._dep_pin_file = dep_pin_file

  def _wait_for_workflow_completion(
    self,
    workflow_file: str,
    branch_name: str,
    commit_sha: str,
    previous_run_id: int | None,
    poll_interval=30,
    timeout=7200,  # 2 hours
  ) -> github.Run | None:
    """
    Polls for the completion of the most recent workflow_dispatch run on the branch.

    Args:
        workflow_file: The filename of the workflow to poll.
        branch_name: The name of the branch where the workflow is running.
        previous_run_id: The ID of the latest run before triggering (to distinguish the new run).
        commit_sha: The commit SHA associated with the workflow run.
        poll_interval: Time to wait between polling attempts (in seconds).
        timeout: Maximum time to wait for the workflow to complete (in seconds).

    Returns:
        A dictionary containing workflow run details if successful and completed
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
      latest_run = self._gh_client.get_latest_run(workflow_file, branch_name)

      if not latest_run:
        logging.info(
          "No workflow runs found yet for branch %s, waiting...",
          branch_name,
        )
        time.sleep(poll_interval)
        continue

      if previous_run_id and latest_run["databaseId"] == previous_run_id:
        logging.info(
          "Waiting for new workflow run to appear...",
        )
        time.sleep(poll_interval)
        continue

      if latest_run["status"] == "completed":
        return latest_run

      logging.info(
        "Run for %s on branch %s is still in progress (%s)...",
        commit_sha,
        branch_name,
        latest_run["status"],
      )

      time.sleep(poll_interval)
    raise TimeoutError("Timed out waiting for workflow to complete")

  def _test_commit(
    self, commit_sha: str, branch_name: str, dep_pin_commit: str | None = None
  ) -> bool:
    """
    Tests a commit by triggering a GitHub workflow on it.

    Args:
        commit_sha: The SHA of the commit to test.
        branch_name: The name of the temporary branch created for testing.
        dep_pin_commit: Optional commit SHA to pin for cross-repo dependency.

    Returns:
        True if the workflow completes successfully, False otherwise.
    """
    logging.info("Testing commit %s on branch %s", commit_sha, branch_name)

    if self._has_culprit_finder_workflow:
      workflow_to_trigger = self._culprit_finder_workflow_file
      inputs = {"workflow-to-debug": self._workflow_file}
    else:
      workflow_to_trigger = self._workflow_file
      inputs = {}

    if dep_pin_commit:
      logging.info("Cross-repo dependency detected, pinning commit %s", dep_pin_commit)
      inputs["cross_repo_commit_sha"] = dep_pin_commit

    logging.info(
      "Triggering workflow %s on %s",
      workflow_to_trigger,
      branch_name,
    )

    # Get the ID of the previous run (if any) to distinguish it from the new one we are about to trigger
    previous_run = self._gh_client.get_latest_run(workflow_to_trigger, branch_name)
    previous_run_id = previous_run["databaseId"] if previous_run else None

    self._gh_client.trigger_workflow(
      workflow_to_trigger,
      branch_name,
      inputs,
    )

    run = self._wait_for_workflow_completion(
      workflow_to_trigger,
      branch_name,
      commit_sha,
      previous_run_id,
    )
    if not run:
      logging.error("Workflow failed to complete")
      return False

    return run["conclusion"] == "success"

  def _bisect(
    self,
    commits: list[github.Commit],
    dep_pin_commit: str | None = None,
    fixed_branch_commit: str | None = None,
  ) -> github.Commit | None:
    """
    Performs binary search on a list of commits to find the culprit.

    Args:
        commits: List of commits to search through.
        dep_pin_commit: Optional commit SHA to pin for cross-repo dependency
                        when testing commits in the primary repo.
        fixed_branch_commit: If set, this commit is used as the base for the
                             test branch, and the commits in `commits` are treated
                             as dependency pins (cross-repo search).

    Returns:
        The culprit commit if found, otherwise None.
    """
    # Initially, start_sha is good, which is before commits[0], so -1
    good_idx = -1
    bad_idx = len(commits)

    while bad_idx - good_idx > 1:
      mid_idx = (good_idx + bad_idx) // 2

      current_commit_sha = commits[mid_idx]["sha"]
      current_dep_pin: str | None

      if fixed_branch_commit:
        branch_name = f"culprit-finder/test_{fixed_branch_commit}_cross_repo_{current_commit_sha}_{uuid.uuid4()}"
        branch_source = fixed_branch_commit
        test_commit = fixed_branch_commit
        current_dep_pin = current_commit_sha
      else:
        branch_name = f"culprit-finder/test-{current_commit_sha}_{uuid.uuid4()}"
        branch_source = current_commit_sha
        test_commit = current_commit_sha
        current_dep_pin = dep_pin_commit

      # Ensure the branch does not exist from a previous run
      if not self._gh_client.check_branch_exists(branch_name):
        self._gh_client.create_branch(branch_name, branch_source)
        logging.info("Created branch %s", branch_name)

      try:
        is_good = self._test_commit(
          test_commit, branch_name, dep_pin_commit=current_dep_pin
        )
      finally:
        if self._gh_client.check_branch_exists(branch_name):
          logging.info("Deleting branch %s", branch_name)
          self._gh_client.delete_branch(branch_name)

      if is_good:
        good_idx = mid_idx
        logging.info("Commit %s is good", current_commit_sha)
      else:
        bad_idx = mid_idx
        logging.info("Commit %s is bad", current_commit_sha)

    if bad_idx == len(commits):
      return None

    return commits[bad_idx]

  def _get_cross_repo_commits(self) -> tuple[str, str]:
    """
    Extracts the start and end commit SHAs for the cross-repo dependency.

    Returns:
        A tuple containing (start_commit_sha, end_commit_sha) for the dependency.
    """
    if not self._dep_pin_file:
      raise ValueError("Dependency pin file is not specified")

    start = self._gh_client.get_file_content(self._dep_pin_file, self._start_sha)
    end = self._gh_client.get_file_content(self._dep_pin_file, self._end_sha)

    # TODO: file extraction logic hardcoded for now
    return start.split("=")[1].strip(), end.split("=")[1].strip()

  def run_bisection(self) -> tuple[github.Commit | None, str]:
    """
    Runs bisection logic (binary search) to find the culprit commit for a GitHub workflow.

    This method iteratively:
    1. Picks a midpoint commit between the known good and bad states.
    2. Creates a temporary branch for that commit.
    3. Triggers the workflow.
    4. Narrows down the range of commits based on the workflow result (success/failure).

    Returns:
      tuple[github.Commit | None, str]: A tuple containing:
              - The culprit commit (or None if not found).
              - The name of the repository where the culprit commit resides.

    """
    commits = self._gh_client.compare_commits(self._start_sha, self._end_sha)
    if not commits:
      logging.info("No commits found between %s and %s", self._start_sha, self._end_sha)
      return None, self._repo

    if not self._cross_repo_dep:
      return self._bisect(commits), self._repo

    cross_repo_start, cross_repo_end = self._get_cross_repo_commits()
    culprit_commit = self._bisect(commits, dep_pin_commit=cross_repo_start)
    if culprit_commit:
      return culprit_commit, self._repo

    logging.info(
      "All commits in target repo are good, proceeding with cross-repo dependency"
    )
    cross_repo_gh_client = github.GithubClient(self._cross_repo_dep)
    cross_repo_commits = cross_repo_gh_client.compare_commits(
      cross_repo_start, cross_repo_end
    )
    return self._bisect(
      cross_repo_commits, fixed_branch_commit=self._start_sha
    ), self._cross_repo_dep
