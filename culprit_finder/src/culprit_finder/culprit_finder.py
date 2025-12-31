"""
Core logic for detecting regression commits using binary search.

This module defines the `CulpritFinder` class, which orchestrates the bisection process.
"""

import time
import logging
import uuid

from github.Commit import Commit
from github.WorkflowRun import WorkflowRun
from github.WorkflowJob import WorkflowJob

from culprit_finder import github_client
from culprit_finder import culprit_finder_state


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
    gh_client: github_client.GithubClient,
    state: culprit_finder_state.CulpritFinderState,
    state_persister: culprit_finder_state.StatePersister,
    job: str | None = None,
    cross_repo_gh_client: github_client.GithubClient | None = None,
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
        gh_client: The GithubClient instance used to interact with GitHub.
        state: The CulpritFinderState object containing the current bisection state.
        state_persister: The StatePersister object used to save the bisection state.
        job: The specific job name within the workflow to monitor for pass/fail.
        cross_repo_gh_client: Optional GithubClient instance for cross-repo operations.
        cross_repo_dep: Optional cross-repo dependency (owner/repo).
        dep_pin_file: Optional file path containing the dependency pin.
    """
    self._repo = repo
    self._start_sha = start_sha
    self._end_sha = end_sha
    self._culprit_finder_workflow_file = CULPRIT_FINDER_WORKFLOW_NAME
    self._workflow_file = workflow_file
    self._has_culprit_finder_workflow = has_culprit_finder_workflow
    self._gh_client = gh_client
    self._state = state
    self._state_persister = state_persister
    self._job = job
    self._cross_repo_gh_client = cross_repo_gh_client
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
  ) -> WorkflowRun | None:
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
        A WorkflowRun object containing workflow run details if successful and completed, or None if failed.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
      latest_run = self._gh_client.get_latest_run(
        workflow_file, branch_name, event="workflow_dispatch"
      )

      if not latest_run:
        logging.info(
          "No workflow runs found yet for branch %s, waiting...",
          branch_name,
        )
        time.sleep(poll_interval)
        continue

      if previous_run_id and latest_run.id == previous_run_id:
        logging.info(
          "Waiting for new workflow run to appear...",
        )
        time.sleep(poll_interval)
        continue

      if latest_run.status == "completed":
        return latest_run

      logging.info(
        "Run for %s on branch %s is still in progress (%s)...",
        commit_sha,
        branch_name,
        latest_run.status,
      )

      time.sleep(poll_interval)
    raise TimeoutError("Timed out waiting for workflow to complete")

  def _get_target_job(self, jobs: list[WorkflowJob]) -> WorkflowJob:
    """
    Finds a specific job in the list, handling nested caller/called names.

    Args:
        jobs: A list of Job objects from a workflow run.

    Returns:
        The Job object that matches the target job name.

    Raises:
        ValueError: If the specified job is not found in the workflow run.
    """

    def get_job_name(name: str) -> str:
      if self._has_culprit_finder_workflow:
        # when calling a workflow from another workflow, the job name is
        # in the format "Caller Job Name / Called Job Name"
        return name.split("/", 1)[-1].strip()
      return name

    target_job = next(
      (job for job in jobs if get_job_name(job.name) == self._job), None
    )
    if target_job:
      return target_job

    logging.error(
      "Job %s not found, jobs in workflow %s",
      self._job,
      self._workflow_file,
    )
    raise ValueError(f"Job {self._job} not found in workflow {self._workflow_file}")

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
    previous_run = self._gh_client.get_latest_run(
      workflow_to_trigger, branch_name, event="workflow_dispatch"
    )
    previous_run_id = previous_run.id if previous_run else None

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

    if run.conclusion == "skipped" and self._has_culprit_finder_workflow:
      raise ValueError(
        f"Bisection stopped: The culprit finder workflow was skipped while testing '{self._workflow_file}'.\n"
        f"Please ensure that 'culprit_finder.yml' is configured to trigger on '{self._workflow_file}' "
        f"and that all required permissions are set."
      )

    if self._job:
      jobs = self._gh_client.get_run_jobs(run.id)
      target_job = self._get_target_job(jobs)
      return target_job.conclusion == "success"

    return run.conclusion == "success"

  def _bisect(
    self,
    commits: list[Commit],
    dep_pin_commit: str | None = None,
    fixed_branch_commit: str | None = None,
  ) -> Commit | None:
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

      current_commit_sha = commits[mid_idx].sha
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

      if current_commit_sha in self._state["cache"]:
        logging.info("Using cached result for commit %s", current_commit_sha)
        is_good = self._state["cache"][current_commit_sha] == "PASS"

        if is_good:
          good_idx = mid_idx
          logging.info("Commit %s is good", current_commit_sha)
        else:
          bad_idx = mid_idx
          logging.info("Commit %s is bad", current_commit_sha)

        continue

      # Ensure the branch does not exist from a previous run
      if not self._gh_client.check_branch_exists(branch_name):
        self._gh_client.create_branch(branch_name, branch_source)
        logging.info("Created branch %s", branch_name)
        self._gh_client.wait_for_branch_creation(branch_name, timeout=180)

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
        self._state["current_good"] = current_commit_sha
        self._state["cache"][current_commit_sha] = "PASS"
        logging.info("Commit %s is good", current_commit_sha)
      else:
        bad_idx = mid_idx
        self._state["current_bad"] = current_commit_sha
        self._state["cache"][current_commit_sha] = "FAIL"
        logging.info("Commit %s is bad", current_commit_sha)

      self._state_persister.save(self._state)

    if bad_idx == len(commits):
      return None

    return commits[bad_idx]

  def _bisect_unified(
    self,
    merged_commits: list[tuple[Commit, str]],
    main_start_sha: str,
    cross_start_sha: str,
    cross_repo_name: str,
  ) -> tuple[Commit | None, str]:
    """
    Performs binary search on a merged timeline of commits from two repositories.

    Args:
        merged_commits: Merged and sorted list of commits from both repos.
                        Each commit dict must have a 'source' key ('main' or 'cross').
        main_start_sha: The known good SHA for the main repository.
        cross_start_sha: The known good SHA for the cross repository.
        cross_repo_name: The name of the cross repository.

    Returns:
        A tuple containing the culprit commit (or None) and the repository name.
    """
    good_idx = -1
    bad_idx = len(merged_commits)

    while bad_idx - good_idx > 1:
      mid_idx = (good_idx + bad_idx) // 2
      candidate, candidate_repo = merged_commits[mid_idx]

      current_main_sha = main_start_sha
      current_cross_sha = cross_start_sha

      found_main = False
      found_cross = False

      # Find the state of both repositories at this point in the timeline
      for i in range(mid_idx, -1, -1):
        commit, repo = merged_commits[i]
        if not found_main and repo == self._repo:
          current_main_sha = commit.sha
          found_main = True
        if not found_cross and repo == cross_repo_name:
          current_cross_sha = commit.sha
          found_cross = True
        if found_main and found_cross:
          break

      # branch_source is always main repo sha (where we run the workflow).
      # dep_pin_commit is the cross repo sha (input to the workflow).
      branch_name = f"culprit-finder/test-unified-{mid_idx}_{uuid.uuid4()}"

      if not self._gh_client.check_branch_exists(branch_name):
        self._gh_client.create_branch(branch_name, current_main_sha)
        logging.info("Created branch %s on %s", branch_name, current_main_sha)
        self._gh_client.wait_for_branch_creation(branch_name, timeout=180)

      try:
        is_good = self._test_commit(
          candidate.sha, branch_name, dep_pin_commit=current_cross_sha
        )
      finally:
        if self._gh_client.check_branch_exists(branch_name):
          logging.info("Deleting branch %s", branch_name)
          self._gh_client.delete_branch(branch_name)

      if is_good:
        good_idx = mid_idx
        self._state["current_good"] = candidate.sha
        self._state["cache"][candidate.sha] = "PASS"
        logging.info(
          "Commit '%s' with sha: %s (%s) is good",
          candidate.commit.message,
          candidate.sha,
          candidate_repo,
        )
      else:
        bad_idx = mid_idx
        self._state["current_bad"] = candidate.sha
        self._state["cache"][candidate.sha] = "FAIL"
        logging.info(
          "Commit '%s' with sha: %s (%s) is bad",
          candidate.commit.message,
          candidate.sha,
          candidate_repo,
        )

      self._state_persister.save(self._state)

    if bad_idx == len(merged_commits):
      return None, self._repo

    culprit, culprit_repo = merged_commits[bad_idx]
    return culprit, culprit_repo

  def _get_cross_repo_commits_from_dep_pin_file(
    self, dep_pin_file: str
  ) -> tuple[str, str]:
    """
    Extracts the start and end commit SHAs for the cross-repo dependency.

    Args:
        dep_pin_file (str): The path to the dependency pin file.

    Returns:
        A tuple containing (start_commit_sha, end_commit_sha) for the dependency.
    """
    start = self._gh_client.get_file_content(dep_pin_file, self._start_sha)
    end = self._gh_client.get_file_content(dep_pin_file, self._end_sha)

    # TODO: file extraction logic hardcoded for now
    return start.split("=")[1].strip(), end.split("=")[1].strip()

  def _get_cross_repo_commits_from_date(
    self, cross_repo_gh_client: github_client.GithubClient
  ) -> tuple[str, str]:
    start_commit = self._gh_client.get_commit(self._start_sha)
    end_commit = self._gh_client.get_commit(self._end_sha)

    start_date = start_commit.commit.committer.date
    end_date = end_commit.commit.committer.date

    cross_start_commit = cross_repo_gh_client.get_commit_at_date(start_date)
    cross_end_commit = cross_repo_gh_client.get_commit_at_date(end_date)

    return cross_start_commit.sha, cross_end_commit.sha

  def _run_cross_repo_bisection_with_pinned_deps(
    self,
    main_commits: list[Commit],
    cross_repo_gh_client: github_client.GithubClient,
    dep_pin_file: str,
  ) -> tuple[Commit | None, str]:
    """
    Runs bisection logic to find the culprit commit for a cross-repo dependency.

    Args:
        main_commits (list[Commit]): List of commits to perform bisection on.
        cross_repo_gh_client (github_client.GithubClient): GitHub client for the cross-repo dependency.
        dep_pin_file (str): Path to the dependency pin file.

    Returns:
        tuple[Commit | None, str]: A tuple containing the culprit commit and the repository name.
    """
    cross_repo_start, cross_repo_end = self._get_cross_repo_commits_from_dep_pin_file(
      dep_pin_file
    )
    culprit_commit = self._bisect(main_commits, dep_pin_commit=cross_repo_start)
    if culprit_commit:
      return culprit_commit, self._repo

    logging.info(
      "All commits in target repo are good, proceeding with cross-repo dependency"
    )
    cross_repo_commits = cross_repo_gh_client.compare_commits(
      cross_repo_start, cross_repo_end
    )
    return self._bisect(
      cross_repo_commits, fixed_branch_commit=self._start_sha
    ), cross_repo_gh_client.repo_name

  def _run_cross_repo_bisection_with_floating_deps(
    self,
    main_repo_commits: list[Commit],
    cross_repo_gh_client: github_client.GithubClient,
  ) -> tuple[Commit | None, str]:
    cross_repo_start, cross_repo_end = self._get_cross_repo_commits_from_date(
      cross_repo_gh_client
    )
    cross_repo_commits = cross_repo_gh_client.compare_commits(
      cross_repo_start, cross_repo_end
    )

    merged_commits: list[tuple[Commit, str]] = [
      (c, self._repo) for c in main_repo_commits
    ] + [(c, cross_repo_gh_client.repo_name) for c in cross_repo_commits]
    merged_commits.sort(key=lambda c: c[0].commit.committer.date)

    return self._bisect_unified(
      merged_commits,
      self._start_sha,
      cross_repo_start,
      cross_repo_gh_client.repo_name,
    )

  def run_bisection(self) -> tuple[Commit | None, str]:
    """
    Runs bisection logic (binary search) to find the culprit commit for a GitHub workflow.

    This method iteratively:
    1. Picks a midpoint commit between the known good and bad states.
    2. Creates a temporary branch for that commit.
    3. Triggers the workflow.
    4. Narrows down the range of commits based on the workflow result (success/failure).

    Returns:
      tuple[Commit | None, str]: A tuple containing:
              - The culprit commit (or None if not found).
              - The name of the repository where the culprit commit resides.

    """
    commits = self._gh_client.compare_commits(self._start_sha, self._end_sha)
    if not commits:
      logging.info("No commits found between %s and %s", self._start_sha, self._end_sha)
      return None, self._repo

    if not self._cross_repo_gh_client:
      return self._bisect(commits), self._repo

    if self._dep_pin_file:
      return self._run_cross_repo_bisection_with_pinned_deps(
        commits, self._cross_repo_gh_client, self._dep_pin_file
      )

    return self._run_cross_repo_bisection_with_floating_deps(
      commits, self._cross_repo_gh_client
    )
